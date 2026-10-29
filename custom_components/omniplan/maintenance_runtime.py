"""Home Assistant side of the maintenance subsystem.

* Feeds sensor states into :class:`~.maintenance_manager.MaintenanceManager`
  (through the coordinator's single state listener).
* Completes (or binds) tasks when an NFC tag is scanned (``tag_scanned``).
* Sends notifications when a task becomes Due Soon / Overdue / Triggered or a
  warranty is about to expire, through the notify service configured on the
  maintenance plan. Objects with a ``zone.*`` (an off-site garage, a cabin)
  hold their notifications until a household member is inside that zone.
* Runs the admin-configured on-complete device action (e.g. ``button.press``
  on an appliance's filter-reset entity) as the user who completed the task.
* Stores uploaded documents content-addressed by SHA-256 in
  ``<config>/.storage/omniplan_docs`` (so identical files are kept once).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time as time_module
from typing import TYPE_CHECKING, Any

from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar

from .const import DOCS_DIRNAME, EVENT_MAINT_DUE
from .maintenance_manager import MaintenanceManager
from .planner import Actor

if TYPE_CHECKING:
    from .coordinator import OmniPlanCoordinator

_LOGGER = logging.getLogger(__name__)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
STATUS_WORDS = {"due_soon": "is due soon", "overdue": "is overdue", "triggered": "was triggered", "warranty": "warranty expires"}
PENDING_META = "maint_pending_notifications"


class MaintenanceRuntime:
    def __init__(self, hass: HomeAssistant, coordinator: OmniPlanCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.docs_dir = hass.config.path(".storage", DOCS_DIRNAME)
        self.manager = MaintenanceManager(coordinator.db, coordinator.planner, state_reader=self._read, area_names=self.area_names)
        self.manager.run_action = self.run_action
        self._pending: list[dict[str, Any]] = []
        self._unsubs: list[Any] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        await self.manager.async_load()
        self._pending = list(await self.coordinator.db.get_meta(PENDING_META, []) or [])
        self._unsubs.append(self.hass.bus.async_listen("tag_scanned", self._on_tag))
        referenced = await self.coordinator.db.all_document_hashes()
        await self.hass.async_add_executor_job(self._cleanup_orphans, referenced)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.manager.async_flush()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @callback
    def _read(self, entity_id: str) -> tuple[str | None, dict[str, Any]] | None:
        state = self.hass.states.get(entity_id)
        return (state.state, dict(state.attributes)) if state else None

    def area_names(self) -> dict[str, str]:
        return {area.id: area.name for area in ar.async_get(self.hass).async_list_areas()}

    def watched_entities(self) -> set[str]:
        return self.manager.watched_entities() | {p["zone_entity_id"] for p in self._pending if p.get("zone_entity_id")}

    @callback
    def handle_state(self, entity_id: str, old: Any, new: Any) -> None:
        old_state = old.state if old else None
        new_state = new.state if new else None
        signals = self.manager.handle_state(
            entity_id, old_state, new_state, dict(old.attributes) if old else {}, dict(new.attributes) if new else {}
        )
        if signals:
            self.coordinator.spawn(self.manager.process_signals(signals), "omniplan_maintenance_signals")
        if any(p.get("zone_entity_id") == entity_id for p in self._pending) and self._someone_in_zone(entity_id):
            self.coordinator.spawn(self._deliver_zone(entity_id), "omniplan_maintenance_zone")

    async def tick(self) -> None:
        for transition in await self.manager.tick():
            await self.notify(transition)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _someone_in_zone(self, zone_entity_id: str) -> bool:
        state = self.hass.states.get(zone_entity_id)
        if state is None:
            return False
        persons = set(state.attributes.get("persons") or [])
        household = set(self.coordinator.household())
        return bool(persons & household) if household else bool(persons)

    async def notify(self, transition: dict[str, Any]) -> None:
        plan = await self.coordinator.db.get_plan(transition["plan_id"])
        if plan is None:
            return
        self.coordinator.planner.fire(
            EVENT_MAINT_DUE,
            self.coordinator.planner.event_payload(
                plan,
                {k: transition.get(k) for k in ("task_id", "object_id", "plan_id", "code", "name", "object_name", "status", "countdown", "assigned_to")},
                "name",
                "object_name",
            ),
        )
        service = (plan.get("settings") or {}).get("notify_service")
        if not service:
            return
        zone = transition.get("zone_entity_id")
        if zone and not self._someone_in_zone(zone):
            self._pending.append({**transition, "service": service})
            self._pending = self._pending[-200:]
            await self.coordinator.db.set_meta(PENDING_META, self._pending)
            await self.coordinator.async_refresh_listeners()
            return
        await self._send(service, transition)

    async def _send(self, service: str, transition: dict[str, Any]) -> None:
        domain, _, name = service.partition(".")
        if transition.get("private"):
            message = "A private maintenance task needs attention."
        elif transition.get("kind") == "warranty":
            message = f"{transition['object_name']}: warranty expires in {transition['countdown'].replace('Due today', 'today')}."
        else:
            message = f"{transition['object_name']}: {transition['name']} {STATUS_WORDS.get(transition['status'], transition['status'])} ({transition['countdown']})."
        link = f"/omniplan?maint_task={transition['task_id']}" if transition.get("task_id") else f"/omniplan?maint_object={transition.get('object_id')}"
        try:
            await self.hass.services.async_call(
                domain,
                name,
                {"title": "OmniPlan maintenance", "message": message, "data": {"url": link, "clickAction": link, "tag": f"omniplan-{transition.get('task_id') or transition.get('object_id')}"}},
                blocking=False,
            )
        except Exception as err:  # noqa: BLE001 - a missing notify service must not break the tick
            _LOGGER.warning("Could not send the maintenance notification through %s: %s", service, err)

    async def _deliver_zone(self, zone_entity_id: str) -> None:
        due = [p for p in self._pending if p.get("zone_entity_id") == zone_entity_id]
        if not due:
            return
        self._pending = [p for p in self._pending if p.get("zone_entity_id") != zone_entity_id]
        await self.coordinator.db.set_meta(PENDING_META, self._pending)
        for transition in due:
            await self._send(transition["service"], transition)
        await self.coordinator.async_refresh_listeners()

    # ------------------------------------------------------------------
    # Device actions & NFC
    # ------------------------------------------------------------------
    async def run_action(self, action: dict[str, Any], actor: Actor) -> None:
        domain, _, service = str(action["service"]).partition(".")
        target = {"entity_id": list(action.get("entity_id") or [])} if action.get("entity_id") else None
        await self.hass.services.async_call(
            domain, service, dict(action.get("data") or {}), target=target, blocking=False, context=Context(user_id=actor.user_id)
        )

    @callback
    def _on_tag(self, event: Event) -> None:
        tag_id = event.data.get("tag_id")
        if tag_id:
            self.coordinator.spawn(self._handle_tag(str(tag_id), event.context.user_id), "omniplan_maintenance_tag")

    async def _handle_tag(self, tag_id: str, user_id: str | None) -> None:
        actor = await self.coordinator.actor_for_user_id(user_id)
        result = await self.manager.handle_tag(tag_id, actor)
        if result["result"] in ("bound", "completed"):
            _LOGGER.debug("NFC tag %s: %s", tag_id, result["result"])

    # ------------------------------------------------------------------
    # Document files
    # ------------------------------------------------------------------
    def file_path(self, sha256: str) -> str:
        if not SHA_RE.match(sha256 or ""):
            raise ValueError("bad document hash")
        return os.path.join(self.docs_dir, sha256)

    async def store_file(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        await self.hass.async_add_executor_job(self._write_file, sha, data)
        return sha

    def _write_file(self, sha: str, data: bytes) -> None:
        os.makedirs(self.docs_dir, exist_ok=True)
        path = self.file_path(sha)
        if os.path.exists(path):
            return  # deduplicated
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)

    def read_file(self, sha: str) -> bytes:
        with open(self.file_path(sha), "rb") as handle:
            return handle.read()

    def _cleanup_orphans(self, referenced: set[str]) -> None:
        """Remove files no document references any more (kept a day so undo can restore them)."""
        if not os.path.isdir(self.docs_dir):
            return
        cutoff = time_module.time() - 86400
        for name in os.listdir(self.docs_dir):
            path = os.path.join(self.docs_dir, name)
            if name.endswith(".tmp") or (SHA_RE.match(name) and name not in referenced):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    continue
