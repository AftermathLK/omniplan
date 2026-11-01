"""Maintenance subsystem on top of the database: objects (assets), their tasks,
spare parts, documents, activity history and the water-softener salt model.

Like :mod:`.planner` this module imports nothing from Home Assistant. Sensor
states arrive through :meth:`MaintenanceManager.handle_state`, current values
are read through an injected ``state_reader`` and time comes from the
planner's clock, so the whole lifecycle runs in plain unit tests. The Home
Assistant runtime (:mod:`.maintenance_runtime`) wires listeners, NFC tags,
notifications and on-complete service calls to it.

Trigger and softener counters change often (a water meter may report every
few seconds), so they live in memory and are flushed to SQLite once a minute,
on shutdown, and immediately whenever something meaningful happens (a trigger
latches, a count increments, the salt model learns).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from . import maintenance as m
from .const import (
    ARCHETYPE_MAINTENANCE,
    COST_MODE_CONSUMPTION,
    COST_MODE_PURCHASE,
    COST_MODES,
    DOC_CATEGORIES,
    EVENT_KIND_COMPLETED,
    EVENT_KIND_MISSED,
    EVENT_KIND_PURCHASE,
    EVENT_KIND_REGEN,
    EVENT_KIND_SALT,
    EVENT_KIND_SKIPPED,
    EVENT_MAINT_COMPLETED,
    FEEDBACK_CHOICES,
    INTERVAL_UNITS,
    LINK_SCHEMES,
    MAINT_CATEGORIES,
    MAINT_OK,
    MAINT_OVERDUE,
    MAINT_SEVERITY,
    MAINT_TRIGGERED,
    MAX_NAME,
    MAX_SUMMARY,
    MAX_TEXT,
    OBJECT_FLEET,
    OBJECT_GENERIC,
    OBJECT_SOFTENER,
    OBJECT_TYPES,
    PRIORITIES,
    RESTOCK_ACTIONS,
    RESTOCK_NONE,
    RESTOCK_SHOPPING,
    RESTOCK_TASK,
    SALT_OBS_EMPTY,
    SALT_OBS_FILL,
    SALT_OBS_KINDS,
    SALT_OBS_LEVEL,
    SALT_SCALES,
    SCHEDULE_MODES,
    SCHEDULE_NONE,
    SCHEDULE_ONCE,
    SCHEDULE_PLANNED,
    SOURCE_MAINT_PART,
    STATUS_COMPLETED,
    TRIGGER_LOGIC,
    TRIGGER_PROBLEM,
    TRIGGER_SALT,
    TRIGGER_THRESHOLD,
    TRIGGER_TYPES,
)
from .database import MDOCS, MEVENTS, MOBJECTS, MPARTS, MTASKS, SALTOBS, TaskDragonDB, new_id
from .pdf import Column, render_table_document
from .planner import SYSTEM, Actor, Planner, PlannerError

_LOGGER = logging.getLogger(__name__)

StateReader = Callable[[str], "tuple[str | None, Mapping[str, Any]] | None"]
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
NFC_LISTEN_SECONDS = 60


def _not_found(what: str = "That") -> PlannerError:
    return PlannerError("not_found", f"{what} was not found (or isn't shared with you)")


def _text(value: Any, limit: int, *, required: str | None = None) -> str | None:
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    if not text:
        if required:
            raise PlannerError("invalid", required)
        return None
    if len(text) > limit:
        raise PlannerError("invalid", f"That text is too long (max {limit} characters)")
    return text


def _long_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise PlannerError("invalid", f"Notes can be at most {limit} characters")
    return text or None


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None, name: str = "Value") -> float | None:
    if value in (None, ""):
        return None
    result = m.num(value)
    if result is None:
        raise PlannerError("invalid", f"{name} must be a number")
    if (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        raise PlannerError("invalid", f"{name} is out of range")
    return result


def _date_text(value: Any, name: str) -> str | None:
    if value in (None, ""):
        return None
    parsed = m.to_date(value)
    if parsed is None:
        raise PlannerError("invalid", f"{name} must look like 2026-11-14")
    return parsed.isoformat()


def _entity(value: Any, *, domains: Sequence[str] | None = None, name: str = "Entity") -> str | None:
    if value in (None, ""):
        return None
    entity_id = str(value).strip()
    if not ENTITY_RE.match(entity_id) or (domains and entity_id.split(".", 1)[0] not in domains):
        raise PlannerError("invalid", f"{name} must be an entity id like {(domains or ['sensor'])[0]}.example")
    return entity_id


def _link(value: Any) -> str | None:
    if value in (None, ""):
        return None
    url = str(value).strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in LINK_SCHEMES or not parts.netloc or len(url) > 2048:
        raise PlannerError("invalid", "Links must start with http:// or https://")
    return url


def _code(obj: Mapping[str, Any] | None, task: Mapping[str, Any] | None = None, event_number: int | None = None) -> str:
    code = f"#{obj['number']}" if obj else "#?"
    if task is not None:
        code += f".{task['number']}"
    if event_number:
        code += f"-{event_number}"
    return code


ACTION_CONFIGURED_BY = "configured_by"
ACTION_BY_SYSTEM = "system"
ACTION_BY_UPGRADE = "upgrade"
ACTIONS_STAMPED_META = "maint_actions_stamped"


def _same_action(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    def bare(x: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return {k: v for k, v in x.items() if k != ACTION_CONFIGURED_BY} if x else None

    return bare(a) == bare(b)


class MaintenanceManager:
    """Assets → tasks → parts & documents, with sensor triggers and learning."""

    def __init__(
        self,
        db: TaskDragonDB,
        planner: Planner,
        *,
        state_reader: StateReader | None = None,
        area_names: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self.db = db
        self.planner = planner
        # Whether whoever configured an on-complete action may still run it (set by the HA runtime).
        self.action_trusted: Callable[[Mapping[str, Any]], Awaitable[bool]] | None = None
        self.tz = planner.tz
        self._state_reader = state_reader
        self._area_names = area_names
        self._trigger_state: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty_tasks: set[str] = set()
        self._softener: dict[str, dict[str, Any]] = {}
        self._dirty_objects: set[str] = set()
        self._watch: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        self._learn_cache: dict[tuple[str, int], dict[str, Any] | None] = {}
        self._nfc_listen: dict[str, Any] | None = None
        # Set by the Home Assistant runtime.
        self.run_action: Callable[[dict[str, Any], Actor], Any] | None = None
        planner.completion_hooks.append(self._on_planner_task_completed)
        planner.undo_handlers.update(
            {
                "maint_completion": self._undo_completion,
                "maint_object": self._undo_object,
                "maint_task": self._undo_task,
                "maint_part": self._undo_part,
                "maint_document": self._undo_document,
            }
        )
        planner.maintenance = self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self.planner.now()

    def today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def _read(self, entity_id: str | None) -> tuple[Any, Mapping[str, Any]]:
        if not entity_id or self._state_reader is None:
            return None, {}
        result = self._state_reader(entity_id)
        if result is None:
            return None, {}
        return result[0], result[1] or {}

    def areas(self) -> Mapping[str, str]:
        return dict(self._area_names() if self._area_names else {})

    async def _plan(self, plan_id: str | None) -> dict[str, Any]:
        plan = await self.db.get_plan(plan_id) if plan_id else None
        if plan is None or plan["archetype"] != ARCHETYPE_MAINTENANCE:
            raise _not_found("That maintenance plan")
        return plan

    async def _object(self, object_id: str | None) -> dict[str, Any]:
        obj = await self.db.row(MOBJECTS, object_id) if object_id else None
        if obj is None:
            raise _not_found("That object")
        return obj

    async def _task(self, task_id: str | None) -> dict[str, Any]:
        task = await self.db.row(MTASKS, task_id) if task_id else None
        if task is None:
            raise _not_found("That maintenance task")
        return task

    async def _part(self, part_id: str | None) -> dict[str, Any]:
        part = await self.db.row(MPARTS, part_id) if part_id else None
        if part is None:
            raise _not_found("That part")
        return part

    def _settings(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        settings = dict(plan.get("settings") or {})
        return {
            "cost_mode": settings.get("cost_mode") if settings.get("cost_mode") in COST_MODES else COST_MODE_CONSUMPTION,
            "currency": settings.get("currency") or "$",
            "notify_service": settings.get("notify_service"),
            "default_warning_days": int(settings.get("default_warning_days") or 7),
            "shopping_plan_id": settings.get("shopping_plan_id"),
            "chore_plan_id": settings.get("chore_plan_id"),
        }

    # ------------------------------------------------------------------
    # Load, flush and the entity watch list
    # ------------------------------------------------------------------
    async def is_action_trusted(self, action: Mapping[str, Any]) -> bool:
        if not action.get(ACTION_CONFIGURED_BY):
            return False
        if self.action_trusted is None:
            return True
        return await self.action_trusted(action)

    async def trust_existing_actions(self) -> int:
        """Upgrade step (once): actions saved before 1.4.1 went through the admin check, so they stay trusted."""
        if await self.db.get_meta(ACTIONS_STAMPED_META):
            return 0
        stamped = 0
        for task in await self.db.rows(MTASKS):
            action = task.get("on_complete_action")
            if action and not action.get(ACTION_CONFIGURED_BY):
                await self.db.update_row(MTASKS, task["id"], {"on_complete_action": {**action, ACTION_CONFIGURED_BY: ACTION_BY_UPGRADE}}, touch=False)
                stamped += 1
        await self.db.set_meta(ACTIONS_STAMPED_META, True)
        return stamped

    async def pause_restored_actions(self) -> int:
        """After a restore: on-complete actions come from a file, not from an administrator, so they wait for one."""
        paused = 0
        for task in await self.db.rows(MTASKS):
            action = task.get("on_complete_action")
            if action and action.get(ACTION_CONFIGURED_BY):
                await self.db.update_row(MTASKS, task["id"], {"on_complete_action": {k: v for k, v in action.items() if k != ACTION_CONFIGURED_BY}}, touch=False)
                paused += 1
            elif action:
                paused += 1
        await self.db.set_meta(ACTIONS_STAMPED_META, True)
        return paused

    async def async_load(self) -> None:
        now = self.now()
        for task in await self.db.rows(MTASKS):
            states: dict[str, dict[str, Any]] = {}
            for cfg in task["triggers"]:
                current, attrs = self._read(cfg.get("entity_id"))
                states[cfg["id"]] = m.trigger_startup(cfg, task["trigger_state"].get(cfg["id"]), current=current, attrs=attrs, now=now)
            self._trigger_state[task["id"]] = states
        for obj in await self.db.rows(MOBJECTS, {"object_type": OBJECT_SOFTENER}):
            self._softener[obj["id"]] = dict(obj["state"] or {})
            self._seed_meter(obj)
            await self._relearn_salt(obj)
        await self.rebuild_watch()

    async def async_flush(self) -> None:
        for task_id in list(self._dirty_tasks):
            self._dirty_tasks.discard(task_id)
            if await self.db.row(MTASKS, task_id) is not None:
                await self.db.update_row(MTASKS, task_id, {"trigger_state": self._trigger_state.get(task_id, {})}, touch=False)
        for object_id in list(self._dirty_objects):
            self._dirty_objects.discard(object_id)
            if await self.db.row(MOBJECTS, object_id) is not None:
                await self.db.update_row(MOBJECTS, object_id, {"state": self._softener.get(object_id, {})}, touch=False)

    async def rebuild_watch(self) -> None:
        watch: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for task in await self.db.rows(MTASKS, {"archived": 0}):
            for cfg in task["triggers"]:
                if cfg.get("entity_id"):
                    watch.setdefault(cfg["entity_id"], []).append(("trigger", task["id"], cfg))
        for obj in await self.db.rows(MOBJECTS, {"object_type": OBJECT_SOFTENER}):
            cfg = obj["config"] or {}
            for key, kind in (("volume_entity", "volume"), ("flow_entity", "flow"), ("level_entity", "level")):
                if cfg.get(key):
                    watch.setdefault(cfg[key], []).append((kind, obj["id"], cfg))
        self._watch = watch

    def watched_entities(self) -> set[str]:
        return set(self._watch)

    # ------------------------------------------------------------------
    # Objects
    # ------------------------------------------------------------------
    def _clean_softener_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "volume_entity": _entity(raw.get("volume_entity"), domains=("sensor",), name="Water volume sensor"),
            "flow_entity": _entity(raw.get("flow_entity"), domains=("sensor",), name="Flow rate sensor"),
            "level_entity": _entity(raw.get("level_entity"), domains=("sensor",), name="Salt level sensor"),
            "level_empty": _number(raw.get("level_empty"), name="Sensor reading when empty"),
            "level_full": _number(raw.get("level_full"), name="Sensor reading when full"),
            "scale": raw.get("scale") if raw.get("scale") in SALT_SCALES else "levels",
            "max_level": _number(raw.get("max_level"), minimum=1, maximum=100, name="Top level") or 8,
            "min_level": _number(raw.get("min_level"), minimum=0, maximum=99, name="Bottom level") or 0,
            "capacity_bags": _number(raw.get("capacity_bags"), minimum=0.5, maximum=50, name="Tank capacity") or 4,
            "bag_lb": _number(raw.get("bag_lb"), minimum=1, maximum=200, name="Bag size") or 40,
            "warning_level": _number(raw.get("warning_level"), minimum=0, name="Warning level"),
            "unit": raw.get("unit") if raw.get("unit") in ("gal", "L") else "gal",
            "regen_start": m.to_time(raw.get("regen_start")).strftime("%H:%M") if m.to_time(raw.get("regen_start")) else "02:00",
            "regen_end": m.to_time(raw.get("regen_end")).strftime("%H:%M") if m.to_time(raw.get("regen_end")) else "04:00",
            "regen_min_flow": _number(raw.get("regen_min_flow"), minimum=0, name="Regeneration flow") or 0.5,
            "regen_min_minutes": _number(raw.get("regen_min_minutes"), minimum=1, name="Regeneration minutes") or 8,
            "initial_tank_volume": _number(raw.get("initial_tank_volume"), minimum=1, name="Initial estimate"),
        }
        if cfg["max_level"] <= cfg["min_level"]:
            raise PlannerError("invalid", "The top level must be above the bottom level")
        return {k: v for k, v in cfg.items() if v is not None}

    async def save_object(self, data: Mapping[str, Any], actor: Actor = SYSTEM) -> dict[str, Any]:
        existing = await self._object(data["id"]) if data.get("id") else None
        plan = await self._plan(existing["plan_id"] if existing else data.get("plan_id"))
        fields: dict[str, Any] = {}
        if existing is None or "name" in data:
            fields["name"] = _text(data.get("name"), MAX_NAME, required="An object needs a name")
        for key, limit in (("manufacturer_model", MAX_NAME), ("area_id", 64)):
            if key in data:
                fields[key] = _text(data[key], limit)
        if "icon" in data:
            icon = _text(data["icon"], 64)
            if icon and not re.match(r"^mdi:[a-z0-9]+(?:-[a-z0-9]+)*$", icon):
                raise PlannerError("invalid", "Icons look like mdi:water-boiler")
            fields["icon"] = icon
        if "object_type" in data or existing is None:
            kind = data.get("object_type") or OBJECT_GENERIC
            if kind not in OBJECT_TYPES:
                raise PlannerError("invalid", "Unknown object type")
            fields["object_type"] = kind
        if "zone_entity_id" in data:
            fields["zone_entity_id"] = _entity(data["zone_entity_id"], domains=("zone",), name="Zone")
        for key, name in (("installed_date", "Installed date"), ("warranty_expiry_date", "Warranty date")):
            if key in data:
                fields[key] = _date_text(data[key], name)
        if "warranty_reminder_days" in data:
            fields["warranty_reminder_days"] = int(_number(data["warranty_reminder_days"], minimum=0, maximum=365, name="Reminder days") or 30)
        if "notes" in data:
            fields["notes"] = _long_text(data["notes"])
        if "archived" in data:
            fields["archived"] = bool(data["archived"])
        if "parent_object_id" in data:
            parent_id = data["parent_object_id"] or None
            if parent_id:
                parent = await self._object(parent_id)
                if parent["plan_id"] != plan["id"]:
                    raise PlannerError("invalid", "The parent object must be in the same plan")
                if existing is not None:
                    walk, seen = parent, set()
                    while walk is not None and walk["id"] not in seen:
                        if walk["id"] == existing["id"]:
                            raise PlannerError("invalid", "An object can't be inside itself")
                        seen.add(walk["id"])
                        walk = await self.db.row(MOBJECTS, walk["parent_object_id"]) if walk["parent_object_id"] else None
            fields["parent_object_id"] = parent_id
        kind = fields.get("object_type") or (existing or {}).get("object_type")
        if "config" in data and kind == OBJECT_SOFTENER:
            fields["config"] = self._clean_softener_config(data.get("config") or {})
        elif "config" in data:
            fields["config"] = {}
        if existing is None:
            fields.update(plan_id=plan["id"], number=await self.db.next_number(MOBJECTS, {"plan_id": plan["id"]}))
            obj = await self.db.insert_row(MOBJECTS, fields)
            if kind == OBJECT_SOFTENER:
                await self._ensure_salt_task(obj, actor)
        else:
            obj = await self.db.update_row(MOBJECTS, existing["id"], fields)
            assert obj is not None
        if obj["object_type"] == OBJECT_SOFTENER:
            self._softener.setdefault(obj["id"], dict(obj["state"] or {}))
            self._seed_meter(obj)
            await self._relearn_salt(obj)
        await self.rebuild_watch()
        self.planner.notify([plan["id"]])
        return obj

    def _seed_meter(self, obj: Mapping[str, Any]) -> None:
        """Start counting from the meter's current reading (and again if the sensor changes)."""
        entity_id = (obj.get("config") or {}).get("volume_entity")
        live = self._softener.setdefault(obj["id"], {})
        if not entity_id:
            return
        if live.get("volume_entity") != entity_id:
            live["volume_entity"] = entity_id
            live["last_raw"] = None
        if live.get("last_raw") is None:
            reading = m.num(self._read(entity_id)[0])
            if reading is not None:
                live["last_raw"] = reading
                live.setdefault("total", 0.0)
                self._dirty_objects.add(obj["id"])

    async def _ensure_salt_task(self, obj: Mapping[str, Any], actor: Actor) -> None:
        for task in await self.db.rows(MTASKS, {"object_id": obj["id"]}):
            if any(cfg.get("type") == TRIGGER_SALT for cfg in task["triggers"]):
                return
        await self.save_task(
            {
                "object_id": obj["id"],
                "name": "Refill Water Softener Salt",
                "category": "Refill",
                "schedule_mode": SCHEDULE_NONE,
                "warning_days": 5,
                "triggers": [{"type": TRIGGER_SALT}],
            },
            actor,
        )

    async def delete_object(self, object_id: str) -> dict[str, Any]:
        obj = await self._object(object_id)
        tasks = await self.db.rows(MTASKS, {"object_id": object_id})
        payload = {
            "object": obj,
            "tasks": tasks,
            "task_parts": await self.db.task_parts([t["id"] for t in tasks]),
            "events": await self.db.rows(MEVENTS, {"object_id": object_id}),
            "documents": await self.db.rows(MDOCS, {"object_id": object_id}),
            "observations": await self.db.rows(SALTOBS, {"object_id": object_id}),
            "parts": [p["id"] for p in await self.db.rows(MPARTS, {"object_id": object_id})],
            "children": [c["id"] for c in await self.db.rows(MOBJECTS, {"parent_object_id": object_id})],
        }
        await self.db.delete_row(MOBJECTS, object_id)
        for task in tasks:
            self._trigger_state.pop(task["id"], None)
        self._softener.pop(object_id, None)
        await self.rebuild_watch()
        self.planner.notify([obj["plan_id"]])
        return payload

    async def _undo_object(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        obj = payload["object"]
        await self._require_access(obj["plan_id"], actor)
        await self.db.restore_rows(MOBJECTS, [obj])
        await self.db.restore_rows(MTASKS, payload["tasks"])
        for task in payload["tasks"]:
            self._trigger_state[task["id"]] = dict(task["trigger_state"])
        by_task: dict[str, list[tuple[str, float]]] = {}
        for link in payload["task_parts"]:
            if await self.db.row(MPARTS, link["part_id"]) is not None:
                by_task.setdefault(link["task_id"], []).append((link["part_id"], link["quantity"]))
        for task_id, parts in by_task.items():
            await self.db.set_task_parts(task_id, parts)
        await self.db.restore_rows(MEVENTS, payload["events"])
        await self.db.restore_rows(MDOCS, payload["documents"])
        await self.db.restore_rows(SALTOBS, payload["observations"])
        for part_id in payload["parts"]:
            if await self.db.row(MPARTS, part_id) is not None:
                await self.db.update_row(MPARTS, part_id, {"object_id": obj["id"]})
        for child_id in payload["children"]:
            if await self.db.row(MOBJECTS, child_id) is not None:
                await self.db.update_row(MOBJECTS, child_id, {"parent_object_id": obj["id"]})
        if obj["object_type"] == OBJECT_SOFTENER:
            self._softener[obj["id"]] = dict(obj["state"] or {})
            await self._relearn_salt(obj)
        await self.rebuild_watch()
        self.planner.notify([obj["plan_id"]])
        return {"restored": [obj["id"]]}

    async def _require_access(self, plan_id: str, actor: Actor) -> None:
        if not self.planner.can_access(await self.db.get_plan(plan_id), actor):
            raise _not_found()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def _clean_trigger(self, raw: Mapping[str, Any], existing_ids: set[str]) -> dict[str, Any]:
        kind = raw.get("type")
        if kind not in TRIGGER_TYPES:
            raise PlannerError("invalid", "Unknown trigger type")
        trigger_id = str(raw.get("id") or "")
        if not re.match(r"^[a-z0-9]{1,32}$", trigger_id) or trigger_id in existing_ids:
            trigger_id = new_id()[:12]
        cfg: dict[str, Any] = {"id": trigger_id, "type": kind}
        if kind != TRIGGER_SALT:
            cfg["entity_id"] = _entity(raw.get("entity_id"), name="Trigger entity")
        if kind == TRIGGER_THRESHOLD:
            cfg["above"] = _number(raw.get("above"), name="Above")
            cfg["below"] = _number(raw.get("below"), name="Below")
            if cfg["above"] is None and cfg["below"] is None:
                raise PlannerError("invalid", "A threshold trigger needs an above or below value")
            cfg["for_minutes"] = _number(raw.get("for_minutes"), minimum=0, maximum=100000, name="Minutes") or 0
        elif kind == "counter":
            cfg["delta"] = _number(raw.get("delta"), minimum=0.000001, name="Increase") or 0
            if not cfg["delta"]:
                raise PlannerError("invalid", "A counter trigger needs the increase to wait for")
            cfg["unit"] = _text(raw.get("unit"), 20)
        elif kind == "runtime":
            cfg["target_state"] = _text(raw.get("target_state"), 100) or "on"
            cfg["attribute"] = _text(raw.get("attribute"), 64)
            cfg["target_hours"] = _number(raw.get("target_hours"), minimum=0.01, maximum=1000000, name="Hours") or 0
            if not cfg["target_hours"]:
                raise PlannerError("invalid", "A runtime trigger needs the number of hours")
        elif kind == "state_count":
            cfg["from_state"] = _text(raw.get("from_state"), 100)
            cfg["to_state"] = _text(raw.get("to_state"), 100) or "on"
            cfg["count"] = int(_number(raw.get("count"), minimum=1, maximum=1000000, name="Count") or 1)
            cfg["debounce_seconds"] = _number(raw.get("debounce_seconds"), minimum=0, maximum=86400, name="Debounce") or 0
        elif kind == TRIGGER_PROBLEM:
            cfg["auto_complete"] = bool(raw.get("auto_complete"))
        return {k: v for k, v in cfg.items() if v is not None}

    def _clean_action(self, raw: Any) -> dict[str, Any] | None:
        if not raw:
            return None
        if not isinstance(raw, Mapping):
            raise PlannerError("invalid", "The on-complete action must be an object")
        service = str(raw.get("service") or "").strip()
        if not SERVICE_RE.match(service):
            raise PlannerError("invalid", "Services look like button.press")
        targets = raw.get("entity_id") or []
        targets = [targets] if isinstance(targets, str) else list(targets)
        clean_targets = [_entity(t, name="Action target") for t in targets if t][:20]
        data = raw.get("data") or {}
        if not isinstance(data, Mapping) or len(data) > 10:
            raise PlannerError("invalid", "Action data must be a small object")
        clean_data = {}
        for key, value in data.items():
            if not re.match(r"^[a-z0-9_]{1,64}$", str(key)) or not isinstance(value, (str, int, float, bool)):
                raise PlannerError("invalid", "Action data values must be plain text or numbers")
            clean_data[str(key)] = value
        return {"service": service, "entity_id": clean_targets, "data": clean_data}

    async def save_task(self, data: Mapping[str, Any], actor: Actor = SYSTEM) -> dict[str, Any]:
        existing = await self._task(data["id"]) if data.get("id") else None
        obj = await self._object(data.get("object_id") or (existing or {}).get("object_id"))
        if existing is not None and obj["plan_id"] != existing["plan_id"]:
            raise PlannerError("invalid", "Tasks can only move between objects of the same plan")
        plan = await self._plan(obj["plan_id"])
        settings = self._settings(plan)
        fields: dict[str, Any] = {}
        if existing is None or "name" in data:
            fields["name"] = _text(data.get("name"), MAX_NAME, required="A task needs a name")
        if "category" in data or existing is None:
            category = data.get("category") or "Service"
            if category not in MAINT_CATEGORIES:
                raise PlannerError("invalid", "Unknown task type")
            fields["category"] = category
        if "priority" in data:
            fields["priority"] = data["priority"] if data["priority"] in PRIORITIES else "none"
        if "notes" in data:
            fields["notes"] = _long_text(data["notes"])
        if "spec_note" in data:
            fields["spec_note"] = _text(data["spec_note"], MAX_SUMMARY)
        if "schedule_mode" in data or existing is None:
            mode = data.get("schedule_mode") or "rolling"
            if mode not in SCHEDULE_MODES:
                raise PlannerError("invalid", "Unknown schedule")
            fields["schedule_mode"] = mode
        mode = fields.get("schedule_mode", (existing or {}).get("schedule_mode"))
        if "interval_value" in data:
            value = _number(data["interval_value"], minimum=1, maximum=1000, name="Interval")
            fields["interval_value"] = int(value) if value else None
        if "interval_unit" in data:
            if data["interval_unit"] not in INTERVAL_UNITS:
                raise PlannerError("invalid", "Unknown interval unit")
            fields["interval_unit"] = data["interval_unit"]
        value = fields.get("interval_value", (existing or {}).get("interval_value"))
        unit = fields.get("interval_unit", (existing or {}).get("interval_unit") or "months")
        if mode in ("rolling", SCHEDULE_PLANNED) and not value:
            raise PlannerError("invalid", "Rolling and planned schedules need an interval")
        if "warning_days" in data or existing is None:
            fields["warning_days"] = int(_number(data.get("warning_days"), minimum=0, maximum=365, name="Warning days") or (settings["default_warning_days"] if data.get("warning_days") in (None, "") else 0))
        if "warning_percent" in data:
            fields["warning_percent"] = int(_number(data["warning_percent"], minimum=0, maximum=90, name="Warning percent") or 0)
        if "schedule_time" in data:
            parsed_time = m.to_time(data["schedule_time"])
            if data["schedule_time"] and parsed_time is None:
                raise PlannerError("invalid", "Times look like 08:30")
            fields["schedule_time"] = parsed_time.strftime("%H:%M") if parsed_time else None
        if "active_months" in data:
            months = sorted({int(x) for x in data["active_months"] or [] if str(x).isdigit() and 1 <= int(x) <= 12})
            fields["active_months"] = [] if len(months) == 12 else months
        if "triggers" in data:
            seen: set[str] = set()
            triggers = []
            for raw in list(data["triggers"] or [])[:10]:
                cfg = self._clean_trigger(raw, seen)
                seen.add(cfg["id"])
                triggers.append(cfg)
            fields["triggers"] = triggers
        if "trigger_logic" in data:
            fields["trigger_logic"] = data["trigger_logic"] if data["trigger_logic"] in TRIGGER_LOGIC else "or"
        if "assigned_to" in data:
            fields["assigned_to"] = _entity(data["assigned_to"], domains=("person",), name="Assignee")
        if "rotation" in data:
            fields["rotation"] = list(dict.fromkeys(_entity(p, domains=("person",), name="Rotation") for p in data["rotation"] or [] if p))[:20]
        if "nfc_tag_id" in data:
            fields["nfc_tag_id"] = _text(data["nfc_tag_id"], 128)
        if "require_nfc" in data:
            fields["require_nfc"] = bool(data["require_nfc"])
        if "on_complete_action" in data:
            action = self._clean_action(data["on_complete_action"])
            previous = (existing or {}).get("on_complete_action")
            privileged = actor.user_id is None or actor.is_admin
            if action is not None:
                if privileged:
                    # Stamped with who configured it; run time checks they are still an administrator.
                    action[ACTION_CONFIGURED_BY] = actor.user_id or ACTION_BY_SYSTEM
                elif _same_action(action, previous):
                    action[ACTION_CONFIGURED_BY] = (previous or {}).get(ACTION_CONFIGURED_BY)
                else:
                    raise PlannerError("unauthorized", "Only administrators can set an on-complete device action")
            elif previous is not None and not privileged:
                raise PlannerError("unauthorized", "Only administrators can set an on-complete device action")
            fields["on_complete_action"] = action
        if "manual_doc_id" in data:
            doc_id = data["manual_doc_id"] or None
            if doc_id:
                doc = await self.db.row(MDOCS, doc_id)
                if doc is None or doc["object_id"] != obj["id"]:
                    raise PlannerError("invalid", "The manual must be a document of this object")
            fields["manual_doc_id"] = doc_id
        if "manual_page" in data:
            page = _number(data["manual_page"], minimum=1, maximum=100000, name="Page")
            fields["manual_page"] = int(page) if page else None
        if "archived" in data:
            fields["archived"] = bool(data["archived"])
        for key, name in (("next_due", "Next due"), ("anchor_date", "Anchor date")):
            if key in data:
                fields[key] = _date_text(data[key], name)
        if "last_done" in data and data["last_done"]:
            last = m.from_iso(data["last_done"]) or (datetime.combine(m.to_date(data["last_done"]), datetime.min.time(), self.tz) if m.to_date(data["last_done"]) else None)
            if last is None:
                raise PlannerError("invalid", "Last performed must be a date")
            fields["last_done"] = m.to_iso(last if last.tzinfo else last.replace(tzinfo=self.tz))

        if mode in ("rolling", SCHEDULE_PLANNED) and not fields.get("next_due", (existing or {}).get("next_due")):
            last = m.from_iso(fields.get("last_done", (existing or {}).get("last_done")))
            start = last.astimezone(self.tz).date() if last else self.today()
            fields["next_due"] = m.first_due(mode=mode, value=value, unit=unit, start=start, active_months=fields.get("active_months", (existing or {}).get("active_months"))).isoformat()
        if mode == SCHEDULE_PLANNED and not fields.get("anchor_date", (existing or {}).get("anchor_date")):
            fields["anchor_date"] = fields.get("next_due", (existing or {}).get("next_due"))
        if mode == SCHEDULE_NONE:
            fields["next_due"] = None

        parts = None
        if "parts" in data:
            parts = []
            for entry in list(data["parts"] or [])[:20]:
                part = await self._part(entry.get("part_id"))
                if part["plan_id"] != plan["id"]:
                    raise PlannerError("invalid", "Parts must belong to the same plan")
                parts.append((part["id"], _number(entry.get("quantity"), minimum=0.001, maximum=100000, name="Quantity") or 1))

        if existing is None:
            fields.update(plan_id=plan["id"], object_id=obj["id"], number=await self.db.next_number(MTASKS, {"object_id": obj["id"]}))
            task = await self.db.insert_row(MTASKS, fields)
        else:
            if obj["id"] != existing["object_id"]:
                fields.update(object_id=obj["id"], number=await self.db.next_number(MTASKS, {"object_id": obj["id"]}))
            task = await self.db.update_row(MTASKS, existing["id"], fields)
            assert task is not None
        if parts is not None:
            await self.db.set_task_parts(task["id"], parts)
        # Keep the progress of triggers whose definition didn't change; start fresh ones from now.
        previous = {cfg["id"]: cfg for cfg in (existing or {}).get("triggers") or []}
        states = dict(self._trigger_state.get(task["id"], {}))
        fresh: dict[str, dict[str, Any]] = {}
        now = self.now()
        for cfg in task["triggers"]:
            if cfg["id"] in states and previous.get(cfg["id"]) == cfg:
                fresh[cfg["id"]] = states[cfg["id"]]
            else:
                current, attrs = self._read(cfg.get("entity_id"))
                fresh[cfg["id"]] = m.trigger_reset(cfg, current=current, attrs=attrs, now=now)
        self._trigger_state[task["id"]] = fresh
        await self.db.update_row(MTASKS, task["id"], {"trigger_state": fresh}, touch=False)
        await self.rebuild_watch()
        self.planner.notify([plan["id"]])
        saved = await self._task(task["id"])
        action = saved.get("on_complete_action")
        # Who configured the action stays on the server.
        return {**saved, "on_complete_action": {k: v for k, v in action.items() if k != ACTION_CONFIGURED_BY} if action else None, "action_paused": bool(action) and not action.get(ACTION_CONFIGURED_BY)}

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        task = await self._task(task_id)
        payload = {
            "task": task,
            "task_parts": await self.db.task_parts([task_id]),
            "events": await self.db.rows(MEVENTS, {"task_id": task_id}),
        }
        await self.db.delete_row(MTASKS, task_id)
        self._trigger_state.pop(task_id, None)
        await self.rebuild_watch()
        self.planner.notify([task["plan_id"]])
        return payload

    async def _undo_task(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        task = payload["task"]
        await self._require_access(task["plan_id"], actor)
        if await self.db.row(MOBJECTS, task["object_id"]) is None:
            raise _not_found("The task's object")
        await self.db.restore_rows(MTASKS, [task])
        await self.db.set_task_parts(task["id"], [(p["part_id"], p["quantity"]) for p in payload["task_parts"] if await self.db.row(MPARTS, p["part_id"])])
        await self.db.restore_rows(MEVENTS, payload["events"])
        self._trigger_state[task["id"]] = dict(task["trigger_state"])
        await self.rebuild_watch()
        self.planner.notify([task["plan_id"]])
        return {"restored": [task["id"]]}

    async def create_from_task_fields(self, plan: Mapping[str, Any], fields: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        """A plain task (brain dump, todo entity, promotion) landing in a maintenance plan."""
        objects = await self.db.rows(MOBJECTS, {"plan_id": plan["id"], "name": "General"})
        obj = objects[0] if objects else await self.save_object({"plan_id": plan["id"], "name": "General", "icon": "mdi:toolbox-outline"}, actor)
        due = m.to_date(fields.get("due_date")) or (m.from_iso(fields.get("due_datetime")).astimezone(self.tz).date() if fields.get("due_datetime") else None)
        rolling = fields.get("recurrence_mode") == "rolling" and fields.get("recurrence_interval_days")
        calendar_rule = m.interval_from_rrule(fields.get("recurrence_rule")) if fields.get("recurrence_mode") == "calendar" else None
        payload: dict[str, Any] = {
            "object_id": obj["id"],
            "name": str(fields.get("summary") or "")[:MAX_NAME],
            "category": m.guess_category(str(fields.get("summary") or "")),
            "notes": fields.get("description"),
            "priority": fields.get("priority") or "none",
            "assigned_to": (fields.get("assigned_to") or [None])[0] if isinstance(fields.get("assigned_to"), list) else fields.get("assigned_to"),
        }
        if rolling:
            value, unit = m.days_to_interval(float(fields["recurrence_interval_days"]))
            payload.update(schedule_mode="rolling", interval_value=value, interval_unit=unit, next_due=due.isoformat() if due else None)
        elif calendar_rule:
            payload.update(schedule_mode=SCHEDULE_PLANNED, interval_value=calendar_rule[0], interval_unit=calendar_rule[1], next_due=due.isoformat() if due else None)
        else:
            payload.update(schedule_mode=SCHEDULE_ONCE, next_due=(due or self.today() + timedelta(days=7)).isoformat())
        task = await self.save_task(payload, actor)
        return {"uid": task["id"], "plan_id": task["plan_id"], "summary": task["name"], "maintenance": True}

    # ------------------------------------------------------------------
    # Parts
    # ------------------------------------------------------------------
    async def save_part(self, data: Mapping[str, Any], actor: Actor = SYSTEM) -> dict[str, Any]:
        existing = await self._part(data["id"]) if data.get("id") else None
        plan = await self._plan(existing["plan_id"] if existing else data.get("plan_id"))
        fields: dict[str, Any] = {}
        if existing is None or "part_name" in data:
            fields["part_name"] = _text(data.get("part_name"), MAX_NAME, required="A part needs a name")
        for key, limit in (("mpn_gtin", 64), ("storage_location", MAX_NAME)):
            if key in data:
                fields[key] = _text(data[key], limit)
        if "notes" in data:
            fields["notes"] = _long_text(data["notes"], 2000)
        if "object_id" in data:
            object_id = data["object_id"] or None
            if object_id and (await self._object(object_id))["plan_id"] != plan["id"]:
                raise PlannerError("invalid", "The object must be in the same plan")
            fields["object_id"] = object_id
        for key, name in (("current_stock", "Stock"), ("reorder_threshold", "Reorder threshold"), ("unit_cost", "Unit cost")):
            if key in data:
                fields[key] = _number(data[key], minimum=0, maximum=1e9, name=name)
                if key != "unit_cost" and fields[key] is None:
                    fields[key] = 0.0
        if "restock_quantity" in data:
            fields["restock_quantity"] = _number(data["restock_quantity"], minimum=0.001, maximum=1e6, name="Restock quantity") or 1
        if "reorder_url" in data:
            fields["reorder_url"] = _link(data["reorder_url"])
        if "restock_action" in data:
            if data["restock_action"] not in RESTOCK_ACTIONS:
                raise PlannerError("invalid", "Unknown restock action")
            fields["restock_action"] = data["restock_action"]
        if existing is None:
            fields["plan_id"] = plan["id"]
            part = await self.db.insert_row(MPARTS, fields)
        else:
            part = await self.db.update_row(MPARTS, existing["id"], fields)
            assert part is not None
            if {"current_stock", "reorder_threshold", "restock_action"} & set(fields):
                await self._check_restock(part, actor)
        self.planner.notify([plan["id"]])
        return await self._part(part["id"])

    async def adjust_stock(self, part_id: str, delta: float, actor: Actor = SYSTEM) -> dict[str, Any]:
        part = await self._part(part_id)
        stock = max(0.0, float(part["current_stock"] or 0) + float(delta))
        part = await self.db.update_row(MPARTS, part_id, {"current_stock": stock})
        assert part is not None
        if delta < 0:
            await self._check_restock(part, actor)
        self.planner.notify([part["plan_id"]])
        return part

    async def delete_part(self, part_id: str) -> dict[str, Any]:
        part = await self._part(part_id)
        links = [link for link in await self.db.task_parts([t["id"] for t in await self.db.rows(MTASKS, {"plan_id": part["plan_id"]})]) if link["part_id"] == part_id]
        await self.db.delete_row(MPARTS, part_id)
        self.planner.notify([part["plan_id"]])
        return {"part": part, "links": links}

    async def _undo_part(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        part = payload["part"]
        await self._require_access(part["plan_id"], actor)
        if part.get("object_id") and await self.db.row(MOBJECTS, part["object_id"]) is None:
            part = {**part, "object_id": None}
        await self.db.restore_rows(MPARTS, [part])
        for link in payload["links"]:
            if await self.db.row(MTASKS, link["task_id"]) is not None:
                current = [(p["part_id"], p["quantity"]) for p in await self.db.task_parts([link["task_id"]])]
                await self.db.set_task_parts(link["task_id"], [*current, (part["id"], link["quantity"])])
        self.planner.notify([part["plan_id"]])
        return {"restored": [part["id"]]}

    async def _check_restock(self, part: Mapping[str, Any], actor: Actor) -> None:
        """Low stock: put the part on the shopping list (or create a "Buy …" task), once."""
        if part["restock_action"] == RESTOCK_NONE or float(part["current_stock"] or 0) > float(part["reorder_threshold"] or 0):
            return
        if part.get("pending_ref"):
            pending = await self.db.get_task(part["pending_ref"])
            if pending is not None and pending["status"] != STATUS_COMPLETED:
                return
        plan = await self.db.get_plan(part["plan_id"])
        settings = self._settings(plan or {})
        quantity = float(part["restock_quantity"] or 1)
        ref = f"part:{part['id']}"
        try:
            if part["restock_action"] == RESTOCK_SHOPPING:
                target = await self.db.get_plan(settings["shopping_plan_id"]) if settings["shopping_plan_id"] else None
                target = target or await self.planner.hardware_plan()
                if target is None:
                    return
                result = await self.planner.add_shopping_item(
                    target["id"], part["part_name"], actor, quantity=m.fmt_number(quantity, 2), source=SOURCE_MAINT_PART, source_ref=ref
                )
                created = result["task"]
            elif part["restock_action"] == RESTOCK_TASK:
                target = await self.db.get_plan(settings["chore_plan_id"]) if settings["chore_plan_id"] else None
                target = target or await self.planner.chores_plan()
                if target is None:
                    return
                created = await self.planner.save_task(
                    {
                        "plan_id": target["id"],
                        "summary": f"Buy {part['part_name']}",
                        "description": f"Restock {m.fmt_number(quantity, 2)} for maintenance (on hand: {m.fmt_number(float(part['current_stock'] or 0), 2)}).",
                        "link_url": part.get("reorder_url"),
                        "source": SOURCE_MAINT_PART,
                        "source_ref": ref,
                        "context_tags": ["Errand"],
                        "dedupe": False,
                    },
                    actor,
                )
            else:
                return
        except PlannerError as err:
            _LOGGER.warning("Could not create a restock item for %s: %s", part["part_name"], err.message)
            return
        await self.db.update_row(MPARTS, part["id"], {"pending_ref": created["uid"]})

    async def _on_planner_task_completed(self, task: Mapping[str, Any], actor: Actor) -> None:
        """A restock shopping item or "Buy …" task was checked off: add the stock back."""
        if task.get("source") != SOURCE_MAINT_PART or not str(task.get("source_ref") or "").startswith("part:"):
            return
        part = await self.db.row(MPARTS, str(task["source_ref"]).removeprefix("part:"))
        if part is None or part.get("pending_ref") != task["uid"]:
            return
        quantity = float(part["restock_quantity"] or 1)
        await self.db.update_row(MPARTS, part["id"], {"current_stock": float(part["current_stock"] or 0) + quantity, "pending_ref": None})
        await self.db.insert_row(
            MEVENTS,
            {
                "plan_id": part["plan_id"],
                "object_id": part.get("object_id"),
                "kind": EVENT_KIND_PURCHASE,
                "method": "restock",
                "occurred_at": m.to_iso(self.now()),
                "user_id": actor.user_id,
                "user_name": actor.user_name,
                "person_entity_id": actor.person,
                "cost": round(quantity * float(part["unit_cost"] or 0), 2) if part.get("unit_cost") else None,
                "data": {"part_id": part["id"], "part_name": part["part_name"], "quantity": quantity},
            },
        )
        self.planner.notify([part["plan_id"]])

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------
    async def add_document(self, data: Mapping[str, Any], actor: Actor = SYSTEM) -> dict[str, Any]:
        obj = await self._object(data.get("object_id"))
        category = data.get("category") if data.get("category") in DOC_CATEGORIES else "other"
        task_id = data.get("task_id") or None
        if task_id and (await self._task(task_id))["object_id"] != obj["id"]:
            raise PlannerError("invalid", "The task must belong to this object")
        event_id = data.get("event_id") or None
        if event_id:
            event = await self.db.row(MEVENTS, event_id)
            if event is None or event["object_id"] != obj["id"]:
                raise PlannerError("invalid", "The service record must belong to this object")
        record: dict[str, Any] = {
            "plan_id": obj["plan_id"],
            "object_id": obj["id"],
            "task_id": task_id,
            "event_id": event_id,
            "category": category,
            "created_by": actor.label,
        }
        if data.get("kind") == "link":
            record.update(kind="link", url=_link(data.get("url")), title=_text(data.get("title"), MAX_NAME) or urlsplit(str(data.get("url"))).netloc)
            if not record["url"]:
                raise PlannerError("invalid", "A link needs a URL")
        else:
            sha = str(data.get("sha256") or "")
            if not re.match(r"^[0-9a-f]{64}$", sha):
                raise PlannerError("invalid", "Unknown file")
            record.update(
                kind="file",
                sha256=sha,
                filename=_text(data.get("filename"), 200) or "file",
                content_type=str(data.get("content_type") or "application/octet-stream"),
                size=int(data.get("size") or 0),
                title=_text(data.get("title"), MAX_NAME) or _text(data.get("filename"), MAX_NAME) or "File",
            )
        doc = await self.db.insert_row(MDOCS, record)
        self.planner.notify([obj["plan_id"]])
        return doc

    async def delete_document(self, doc_id: str) -> dict[str, Any]:
        doc = await self.db.row(MDOCS, doc_id)
        if doc is None:
            raise _not_found("That document")
        await self.db.delete_row(MDOCS, doc_id)
        for task in await self.db.rows(MTASKS, {"manual_doc_id": doc_id}):
            await self.db.update_row(MTASKS, task["id"], {"manual_doc_id": None, "manual_page": None})
        self.planner.notify([doc["plan_id"]])
        return {"document": doc}

    async def _undo_document(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        doc = payload["document"]
        await self._require_access(doc["plan_id"], actor)
        if await self.db.row(MOBJECTS, doc["object_id"]) is None:
            raise _not_found("The document's object")
        await self.db.restore_rows(MDOCS, [doc])
        self.planner.notify([doc["plan_id"]])
        return {"restored": [doc["id"]]}

    # ------------------------------------------------------------------
    # Completion & skipping
    # ------------------------------------------------------------------
    def _rotate(self, task: Mapping[str, Any]) -> str | None:
        rotation = list(task.get("rotation") or [])
        if not rotation:
            return task.get("assigned_to")
        current = task.get("assigned_to")
        if current in rotation:
            return rotation[(rotation.index(current) + 1) % len(rotation)]
        return rotation[0]

    def _status_of(self, task: Mapping[str, Any], obj: Mapping[str, Any]) -> m.TaskStatus:
        return m.compute_status(task, self._trigger_results(task, obj), now=self.now(), tz=self.tz, extra_countdown=self._once_label(task))

    @staticmethod
    def _once_label(task: Mapping[str, Any]) -> str | None:
        if task.get("schedule_mode") == SCHEDULE_ONCE and not task.get("next_due") and task.get("last_done"):
            return "Done"
        return None

    def _fresh_trigger_states(self, task: Mapping[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
        states = {}
        for cfg in task["triggers"]:
            current, attrs = self._read(cfg.get("entity_id"))
            states[cfg["id"]] = m.trigger_reset(cfg, current=current, attrs=attrs, now=now)
        return states

    async def complete(
        self,
        task_id: str,
        actor: Actor = SYSTEM,
        *,
        method: str = "manual",
        cost: Any = None,
        duration_minutes: Any = None,
        notes: Any = None,
        feedback: str | None = None,
        performed_by: str | None = None,
        photo_doc_id: str | None = None,
        parts: Sequence[Mapping[str, Any]] | None = None,
        completed_at: Any = None,
        salt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = await self._task(task_id)
        obj = await self._object(task["object_id"])
        if task["require_nfc"] and method not in ("nfc", "auto_recovery"):
            raise PlannerError("nfc_required", "This task can only be completed by scanning its NFC tag at the appliance")
        if feedback is not None and feedback not in FEEDBACK_CHOICES:
            raise PlannerError("invalid", "Unknown feedback")
        now = self.now()
        moment = now
        if completed_at:
            moment = m.from_iso(completed_at) or now
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=self.tz)
            moment = min(moment, now)
        local_day = moment.astimezone(self.tz).date()
        status_before = self._status_of(task, obj).status
        previous_done = m.from_iso(task["last_done"])
        interval = round((moment - previous_done).total_seconds() / 86400, 2) if previous_done and moment > previous_done else None

        consumed: list[dict[str, Any]] = []
        stocks_before: dict[str, float] = {}
        parts_cost = 0.0
        usage = parts if parts is not None else [{"part_id": p["part_id"], "quantity": p["quantity"]} for p in await self.db.task_parts([task_id])]
        for entry in usage:
            part = await self.db.row(MPARTS, entry.get("part_id") or "")
            quantity = float(_number(entry.get("quantity"), minimum=0, maximum=100000, name="Quantity") or 0)
            if part is None or part["plan_id"] != task["plan_id"] or quantity <= 0:
                continue
            stocks_before[part["id"]] = float(part["current_stock"] or 0)
            await self.db.update_row(MPARTS, part["id"], {"current_stock": max(0.0, stocks_before[part["id"]] - quantity)})
            line_cost = quantity * float(part["unit_cost"] or 0)
            parts_cost += line_cost
            consumed.append({"part_id": part["id"], "part_name": part["part_name"], "quantity": quantity, "cost": round(line_cost, 2)})

        next_due = m.next_due_after(
            mode=task["schedule_mode"],
            value=task["interval_value"],
            unit=task["interval_unit"],
            anchor=m.to_date(task["anchor_date"]),
            previous_due=m.to_date(task["next_due"]),
            completed_on=local_day,
            active_months=task["active_months"],
        )
        states_before = dict(self._trigger_state.get(task_id, {}))
        self._trigger_state[task_id] = self._fresh_trigger_states(task, now)

        salt_obs = None
        if obj["object_type"] == OBJECT_SOFTENER and (salt is not None or any(cfg.get("type") == TRIGGER_SALT for cfg in task["triggers"])):
            salt = dict(salt or {})
            salt_obs = await self.salt_log(
                obj["id"],
                SALT_OBS_FILL,
                level=salt.get("level"),
                bags=salt.get("bags"),
                actor=actor,
                record_event=False,
            )

        person = _entity(performed_by, domains=("person",), name="Performed by") if performed_by else actor.person
        number = int(task["event_seq"] or 0) + 1
        event = await self.db.insert_row(
            MEVENTS,
            {
                "plan_id": task["plan_id"],
                "object_id": obj["id"],
                "task_id": task_id,
                "number": number,
                "kind": EVENT_KIND_COMPLETED,
                "method": method if method in ("manual", "nfc", "auto_recovery", "todo", "qr", "service", "migrated") else "manual",
                "occurred_at": m.to_iso(moment),
                "user_id": actor.user_id,
                "user_name": actor.user_name,
                "person_entity_id": person,
                "cost": _number(cost, minimum=0, maximum=1e9, name="Cost"),
                "parts_cost": round(parts_cost, 2) if consumed else None,
                "duration_minutes": int(_number(duration_minutes, minimum=0, maximum=100000, name="Duration") or 0) or None,
                "notes": _long_text(notes, 4000),
                "feedback": feedback,
                "interval_days": interval,
                "photo_doc_id": photo_doc_id or None,
                "data": {"parts": consumed, "status_before": status_before, "due_before": task["next_due"], "next_due": next_due.isoformat() if next_due else None, "salt_observation": salt_obs},
            },
        )
        changes = {
            "last_done": m.to_iso(moment),
            "next_due": next_due.isoformat() if next_due else None,
            "event_seq": number,
            "assigned_to": self._rotate(task),
            "trigger_state": self._trigger_state[task_id],
            "last_status": MAINT_OK,
        }
        await self.db.update_row(MTASKS, task_id, changes)
        for part_id in stocks_before:
            part = await self.db.row(MPARTS, part_id)
            if part is not None:
                await self._check_restock(part, actor)
        plan = await self.db.get_plan(task["plan_id"])
        payload = {"uid": task_id, "name": task["name"], "object": obj["name"], "code": _code(obj, task, number), "method": method, "person": person, "user_name": actor.user_name, "next_due": changes["next_due"], "plan_id": task["plan_id"]}
        self.planner.fire(EVENT_MAINT_COMPLETED, self.planner.event_payload(plan, payload, "name", "object"))
        action_result = None
        action = task.get("on_complete_action")
        if action and self.run_action is not None:
            if not await self.is_action_trusted(action):
                _LOGGER.warning("The on-complete action for %s is paused until an administrator saves the task again", _code(obj, task))
            else:
                try:
                    action_result = self.run_action({k: v for k, v in action.items() if k != ACTION_CONFIGURED_BY}, actor)
                    if hasattr(action_result, "__await__"):
                        action_result = await action_result
                except Exception as err:  # noqa: BLE001 - a failing device reset must not undo the completion
                    _LOGGER.warning("On-complete action for %s failed: %s", _code(obj, task), err)
        self.planner.notify([task["plan_id"]])
        updated = await self._task(task_id)
        undo = {"task": task, "stocks": stocks_before, "event_id": event["id"], "trigger_state": states_before, "salt_observation": salt_obs, "pending": None}
        return {"event": self.event_view(event, {obj["id"]: obj}, {task_id: updated}), "task_id": task_id, "next_due": changes["next_due"], "undo": undo}

    async def _undo_completion(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        task = payload["task"]
        await self._require_access(task["plan_id"], actor)
        if await self.db.row(MTASKS, task["id"]) is None:
            raise _not_found("That maintenance task")
        await self.db.update_row(
            MTASKS,
            task["id"],
            {k: task[k] for k in ("last_done", "next_due", "event_seq", "assigned_to", "last_status")} | {"trigger_state": payload["trigger_state"]},
        )
        self._trigger_state[task["id"]] = dict(payload["trigger_state"])
        for part_id, stock in payload["stocks"].items():
            if await self.db.row(MPARTS, part_id) is not None:
                await self.db.update_row(MPARTS, part_id, {"current_stock": stock})
        await self.db.delete_row(MEVENTS, payload["event_id"])
        if payload.get("salt_observation"):
            await self.db.delete_row(SALTOBS, payload["salt_observation"])
            obj = await self.db.row(MOBJECTS, task["object_id"])
            if obj is not None:
                await self._relearn_salt(obj)
        self.planner.notify([task["plan_id"]])
        return {"restored": [task["id"]]}

    async def skip(self, task_id: str, actor: Actor = SYSTEM, notes: Any = None) -> dict[str, Any]:
        """Move on to the next cycle without doing it: *Skipped* if early, honestly *Missed* if overdue."""
        task = await self._task(task_id)
        obj = await self._object(task["object_id"])
        status = self._status_of(task, obj).status
        kind = EVENT_KIND_MISSED if status in (MAINT_OVERDUE, MAINT_TRIGGERED) else EVENT_KIND_SKIPPED
        today = self.today()
        previous_due = m.to_date(task["next_due"])
        if task["schedule_mode"] == "rolling" and task["interval_value"]:
            next_due = m.season_shift(m.add_interval(max(previous_due or today, today), task["interval_value"], task["interval_unit"]), task["active_months"])
        else:
            next_due = m.next_due_after(
                mode=task["schedule_mode"],
                value=task["interval_value"],
                unit=task["interval_unit"],
                anchor=m.to_date(task["anchor_date"]),
                previous_due=previous_due,
                completed_on=today,
                active_months=task["active_months"],
            )
        now = self.now()
        self._trigger_state[task_id] = self._fresh_trigger_states(task, now)
        number = int(task["event_seq"] or 0) + 1
        event = await self.db.insert_row(
            MEVENTS,
            {
                "plan_id": task["plan_id"],
                "object_id": obj["id"],
                "task_id": task_id,
                "number": number,
                "kind": kind,
                "method": "manual",
                "occurred_at": m.to_iso(now),
                "user_id": actor.user_id,
                "user_name": actor.user_name,
                "person_entity_id": actor.person,
                "notes": _long_text(notes, 4000),
                "data": {"status_before": status, "due_before": task["next_due"], "next_due": next_due.isoformat() if next_due else None},
            },
        )
        await self.db.update_row(
            MTASKS,
            task_id,
            {"next_due": next_due.isoformat() if next_due else task["next_due"] if task["schedule_mode"] == SCHEDULE_ONCE else None, "event_seq": number, "trigger_state": self._trigger_state[task_id], "last_status": MAINT_OK},
        )
        self.planner.notify([task["plan_id"]])
        return {"event": self.event_view(event, {obj["id"]: obj}, {task_id: task}), "kind": kind}

    # ------------------------------------------------------------------
    # Sensors
    # ------------------------------------------------------------------
    def handle_state(
        self,
        entity_id: str,
        old: Any,
        new: Any,
        old_attrs: Mapping[str, Any] | None,
        new_attrs: Mapping[str, Any] | None,
    ) -> list[tuple[str, str, Any]]:
        """Fold a state change into triggers and softener meters (synchronous, in memory).

        Returns follow-up work for :meth:`process_signals`:
        ``("auto_complete", task_id, None)``, ``("regeneration", object_id, event)``,
        ``("salt_level", object_id, (kind, level))``, ``("latched", task_id, None)``.
        """
        now = self.now()
        signals: list[tuple[str, str, Any]] = []
        for kind, owner, cfg in self._watch.get(entity_id, []):
            if kind == "trigger":
                states = self._trigger_state.setdefault(owner, {})
                before = states.get(cfg["id"], {})
                after, extra = m.trigger_update(cfg, before, old=old, new=new, old_attrs=old_attrs, new_attrs=new_attrs, now=now)
                states[cfg["id"]] = after
                self._dirty_tasks.add(owner)
                for signal in extra:
                    signals.append((signal, owner, None))
                if (after.get("latched") and not before.get("latched")) or after.get("count") != before.get("count"):
                    signals.append(("persist", owner, None))
            elif kind == "volume":
                live = self._softener.setdefault(owner, {})
                raw = m.num(new)
                if raw is not None:
                    m.feed_volume(live, raw, now.astimezone(self.tz).date())
                    self._dirty_objects.add(owner)
            elif kind == "flow":
                live = self._softener.setdefault(owner, {})
                regen, event = m.regen_feed(cfg, live.get("regen"), now_local=now.astimezone(self.tz), flow=m.num(new), total=float(live.get("total") or 0.0))
                live["regen"] = regen
                self._dirty_objects.add(owner)
                if event:
                    live["regen_total"] = float(live.get("regen_total") or 0.0) + event["volume"]
                    live["last_regen"] = event
                    signals.append(("regeneration", owner, event))
            elif kind == "level":
                observed = self._level_sensor_observation(owner, cfg, m.num(new))
                if observed:
                    signals.append(("salt_level", owner, observed))
        return signals

    def _level_sensor_observation(self, object_id: str, cfg: Mapping[str, Any], reading: float | None) -> tuple[str, float | None] | None:
        """A distance/level sensor crossed one of the tank's markers (or the tank was refilled)."""
        empty, full = m.num(cfg.get("level_empty")), m.num(cfg.get("level_full"))
        if reading is None or empty is None or full is None or empty == full:
            return None
        fraction = min(1.0, max(0.0, (reading - empty) / (full - empty)))
        scale = m.scale_from_config(cfg)
        live = self._softener.setdefault(object_id, {})
        last = live.get("sensor_marker")
        self._dirty_objects.add(object_id)
        if last is not None and fraction >= float(last) + 0.25:
            live["sensor_marker"] = 1.0
            return (SALT_OBS_FILL, None)
        crossed = [marker for marker in scale.markers() if scale.to_fraction(marker) >= fraction - 1e-9 and (last is None or scale.to_fraction(marker) < float(last) - 1e-9)]
        if not crossed:
            if last is None:
                live["sensor_marker"] = fraction
            return None
        marker = min(crossed, key=scale.to_fraction)
        live["sensor_marker"] = scale.to_fraction(marker)
        if last is None:
            return None
        return (SALT_OBS_EMPTY if scale.to_fraction(marker) <= 0 else SALT_OBS_LEVEL, marker)

    async def process_signals(self, signals: Iterable[tuple[str, str, Any]]) -> None:
        persist = False
        for kind, owner, payload in signals:
            try:
                if kind == "auto_complete":
                    await self.complete(owner, SYSTEM, method="auto_recovery", notes="Sensor recovered")
                elif kind == "regeneration":
                    obj = await self.db.row(MOBJECTS, owner)
                    if obj is not None:
                        await self.db.insert_row(
                            MEVENTS,
                            {"plan_id": obj["plan_id"], "object_id": owner, "kind": EVENT_KIND_REGEN, "method": "flow", "occurred_at": payload["ended_at"], "data": payload},
                        )
                        persist = True
                        self.planner.notify([obj["plan_id"]])
                elif kind == "salt_level":
                    obs_kind, level = payload
                    await self.salt_log(owner, obs_kind, level=level, actor=SYSTEM, source="sensor")
                elif kind == "persist":
                    persist = True
                    task = await self.db.row(MTASKS, owner)
                    if task is not None:
                        self.planner.notify([task["plan_id"]])
            except PlannerError as err:
                _LOGGER.debug("Maintenance signal %s for %s ignored: %s", kind, owner, err.message)
        if persist:
            await self.async_flush()

    async def tick(self) -> list[dict[str, Any]]:
        """Minute upkeep: trigger timers, regeneration windows, status transitions and warranty reminders.

        Returns the transitions worth notifying about (a task got worse, or a
        warranty is about to expire), each at most once.
        """
        now = self.now()
        local = now.astimezone(self.tz)
        for task_id, states in self._trigger_state.items():
            task_cfgs = {kind_cfg["id"]: kind_cfg for _, owner, kind_cfg in (w for ws in self._watch.values() for w in ws) if owner == task_id and "id" in kind_cfg}
            for trigger_id, cfg in task_cfgs.items():
                current, attrs = self._read(cfg.get("entity_id"))
                before = states.get(trigger_id, {})
                after = m.trigger_tick(cfg, before, current=current, attrs=attrs, now=now)
                if after != before:
                    states[trigger_id] = after
                    self._dirty_tasks.add(task_id)
        for object_id, live in self._softener.items():
            if live.get("regen", {}).get("active_start"):
                obj_cfg = next((cfg for ws in self._watch.values() for kind, owner, cfg in ws if kind == "flow" and owner == object_id), None)
                if obj_cfg is not None:
                    flow, _ = self._read(obj_cfg.get("flow_entity"))
                    regen, event = m.regen_feed(obj_cfg, live.get("regen"), now_local=local, flow=m.num(flow), total=float(live.get("total") or 0.0))
                    live["regen"] = regen
                    if event:
                        live["regen_total"] = float(live.get("regen_total") or 0.0) + event["volume"]
                        live["last_regen"] = event
                        await self.process_signals([("regeneration", object_id, event)])
        transitions: list[dict[str, Any]] = []
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS)}
        plans = {p["id"]: p for p in await self.db.list_plans()}
        for task in await self.db.rows(MTASKS, {"archived": 0}):
            obj = objects.get(task["object_id"])
            plan = plans.get(task["plan_id"])
            if obj is None or plan is None:
                continue
            status = self._status_of(task, obj)
            previous = task["last_status"] or MAINT_OK
            if status.status != previous:
                await self.db.update_row(MTASKS, task["id"], {"last_status": status.status}, touch=False)
                if MAINT_SEVERITY[status.status] > MAINT_SEVERITY.get(previous, 0):
                    transitions.append(
                        {
                            "kind": "task",
                            "task_id": task["id"],
                            "plan_id": task["plan_id"],
                            "code": _code(obj, task),
                            "name": task["name"],
                            "object_name": obj["name"],
                            "status": status.status,
                            "countdown": status.countdown,
                            "assigned_to": task["assigned_to"],
                            "zone_entity_id": obj.get("zone_entity_id"),
                            "private": bool(plan.get("is_private")),
                        }
                    )
                self.planner.notify([task["plan_id"]])
        for obj in objects.values():
            expiry = m.to_date(obj.get("warranty_expiry_date"))
            state = dict(obj.get("state") or {}) if obj["object_type"] != OBJECT_SOFTENER else self._softener.setdefault(obj["id"], {})
            if expiry is None or state.get("warranty_notified") == expiry.isoformat() or obj["plan_id"] not in plans:
                continue
            days = (expiry - local.date()).days
            if 0 <= days <= int(obj.get("warranty_reminder_days") or 30):
                state["warranty_notified"] = expiry.isoformat()
                if obj["object_type"] == OBJECT_SOFTENER:
                    self._dirty_objects.add(obj["id"])
                else:
                    await self.db.update_row(MOBJECTS, obj["id"], {"state": state}, touch=False)
                transitions.append(
                    {
                        "kind": "warranty",
                        "object_id": obj["id"],
                        "plan_id": obj["plan_id"],
                        "code": _code(obj),
                        "name": "Warranty expires",
                        "object_name": obj["name"],
                        "status": "warranty",
                        "countdown": m.countdown(days),
                        "zone_entity_id": obj.get("zone_entity_id"),
                        "private": bool(plans[obj["plan_id"]].get("is_private")),
                    }
                )
        await self.async_flush()
        return transitions

    # ------------------------------------------------------------------
    # Salt
    # ------------------------------------------------------------------
    async def _relearn_salt(self, obj: Mapping[str, Any]) -> None:
        cfg = obj.get("config") or {}
        scale = m.scale_from_config(cfg)
        observations = await self.db.rows(SALTOBS, {"object_id": obj["id"]}, order=("observed_at",))
        model = m.learn_salt(observations, scale, m.default_tank_volume(cfg, scale))
        live = self._softener.setdefault(obj["id"], {})
        live["model"] = model
        live["last_obs"] = observations[-1] if observations else None
        fills = [o for o in observations if o["kind"] == SALT_OBS_FILL]
        live["last_fill_volume"] = fills[-1]["volume"] if fills else None
        self._dirty_objects.add(obj["id"])

    def effective_volume(self, object_id: str) -> float:
        live = self._softener.get(object_id, {})
        return float(live.get("total") or 0.0) - float(live.get("regen_total") or 0.0)

    def salt_summary(self, obj: Mapping[str, Any]) -> dict[str, Any]:
        cfg = obj.get("config") or {}
        scale = m.scale_from_config(cfg)
        live = self._softener.get(obj["id"], {})
        warning_level = m.num(cfg.get("warning_level"))
        warning_fraction = scale.to_fraction(warning_level) if warning_level is not None else 0.2
        summary = m.salt_summary(
            live.get("model") or {},
            scale,
            last_observation=live.get("last_obs"),
            last_fill_volume=live.get("last_fill_volume"),
            effective_volume=self.effective_volume(obj["id"]),
            daily_use=m.daily_average(live.get("daily") or {}, self.today()),
            warning_fraction=warning_fraction,
            unit=cfg.get("unit") or "gal",
        )
        summary.update(
            {
                "scale": scale.kind,
                "markers": scale.markers(),
                "max_level": scale.max_level,
                "min_level": scale.min_level,
                "capacity_bags": scale.capacity_bags,
                "bag_lb": scale.bag_lb,
                "regenerations": live.get("last_regen"),
                "volume_entity": cfg.get("volume_entity"),
                "flow_entity": cfg.get("flow_entity"),
                "model_segments": (live.get("model") or {}).get("segments", [])[-8:],
            }
        )
        return summary

    async def salt_log(
        self,
        object_id: str,
        kind: str,
        *,
        level: Any = None,
        bags: Any = None,
        actor: Actor = SYSTEM,
        source: str = "manual",
        record_event: bool = True,
    ) -> str:
        obj = await self._object(object_id)
        if obj["object_type"] != OBJECT_SOFTENER:
            raise PlannerError("invalid", "Salt levels belong to water softener objects")
        if kind not in SALT_OBS_KINDS:
            raise PlannerError("invalid", "Unknown salt observation")
        scale = m.scale_from_config(obj.get("config") or {})
        value = _number(level, minimum=0, maximum=1000, name="Level")
        bag_count = _number(bags, minimum=0.1, maximum=50, name="Bags")
        fraction: float | None
        if kind == SALT_OBS_EMPTY:
            fraction = 0.0
        elif kind == SALT_OBS_LEVEL:
            if value is None:
                raise PlannerError("invalid", "Which level is the salt at?")
            fraction = scale.to_fraction(value)
        else:
            fraction = None if bag_count else (scale.to_fraction(value) if value is not None else 1.0)
        obs = await self.db.insert_row(
            SALTOBS,
            {
                "object_id": object_id,
                "kind": kind,
                "level": value,
                "fraction": fraction,
                "bags": bag_count,
                "volume": round(self.effective_volume(object_id), 3),
                "source": source,
                "user_name": actor.label,
                "observed_at": m.to_iso(self.now()),
            },
        )
        await self._relearn_salt(obj)
        if record_event:
            await self.db.insert_row(
                MEVENTS,
                {
                    "plan_id": obj["plan_id"],
                    "object_id": object_id,
                    "kind": EVENT_KIND_SALT,
                    "method": source,
                    "occurred_at": obs["observed_at"],
                    "user_id": actor.user_id,
                    "user_name": actor.user_name,
                    "person_entity_id": actor.person,
                    "data": {"kind": kind, "level": value, "bags": bag_count, "label": scale.label(fraction) if fraction is not None else f"+{m.fmt_number(bag_count, 1)} bags"},
                },
            )
        await self.async_flush()
        self.planner.notify([obj["plan_id"]])
        return obs["id"]

    async def salt_fill(self, object_id: str, actor: Actor, *, level: Any = None, bags: Any = None) -> dict[str, Any]:
        """"Filled salt": completes the refill task (which logs the fill) or just logs it."""
        for task in await self.db.rows(MTASKS, {"object_id": object_id, "archived": 0}):
            if any(cfg.get("type") == TRIGGER_SALT for cfg in task["triggers"]):
                return await self.complete(task["id"], actor, method="manual", salt={"level": level, "bags": bags})
        return {"observation": await self.salt_log(object_id, SALT_OBS_FILL, level=level, bags=bags, actor=actor)}

    # ------------------------------------------------------------------
    # NFC
    # ------------------------------------------------------------------
    def start_nfc_binding(self, task_id: str, actor: Actor) -> datetime:
        expires = self.now() + timedelta(seconds=NFC_LISTEN_SECONDS)
        self._nfc_listen = {"task_id": task_id, "expires": expires, "user_id": actor.user_id}
        return expires

    async def handle_tag(self, tag_id: str, actor: Actor) -> dict[str, Any]:
        """A tag was scanned: bind it (if binding mode is on) or complete the tasks it belongs to."""
        tag = _text(tag_id, 128)
        if not tag:
            return {"result": "ignored"}
        listen = self._nfc_listen
        if listen and listen["expires"] > self.now():
            self._nfc_listen = None
            task = await self.db.row(MTASKS, listen["task_id"])
            if task is not None:
                await self.db.update_row(MTASKS, task["id"], {"nfc_tag_id": tag})
                self.planner.notify([task["plan_id"]])
                return {"result": "bound", "task_id": task["id"]}
        tasks = await self.db.rows(MTASKS, {"nfc_tag_id": tag, "archived": 0})
        if not tasks:
            return {"result": "unknown"}
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"id": [t["object_id"] for t in tasks]})}
        if len(tasks) > 1:
            tasks = [t for t in tasks if self._status_of(t, objects[t["object_id"]]).status != MAINT_OK] or []
        completed = []
        for task in tasks:
            result = await self.complete(task["id"], actor, method="nfc", notes="Completed via NFC tag")
            completed.append({"task_id": task["id"], "code": result["event"]["code"], "plan_id": task["plan_id"]})
        return {"result": "completed" if completed else "nothing_due", "completed": completed}

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    async def recommendation(self, task: Mapping[str, Any], events: Sequence[Mapping[str, Any]] | None = None, *, refresh: bool = False) -> dict[str, Any] | None:
        key = (task["id"], int(task["event_seq"] or 0))
        if not refresh and key in self._learn_cache:
            return self._learn_cache[key]
        if not task.get("interval_value") or task["schedule_mode"] in (SCHEDULE_NONE, SCHEDULE_ONCE):
            self._learn_cache[key] = None
            return None
        if events is None:
            events = await self.db.rows(MEVENTS, {"task_id": task["id"], "kind": EVENT_KIND_COMPLETED}, order=("occurred_at",))
        samples = [
            {"interval_days": e["interval_days"], "feedback": e["feedback"], "completed_on": (m.from_iso(e["occurred_at"]) or self.now()).astimezone(self.tz).date().isoformat()}
            for e in events
            if e["kind"] == EVENT_KIND_COMPLETED and e.get("interval_days")
        ]
        next_due = m.to_date(task.get("next_due"))
        rec = m.recommend_interval(samples, m.interval_days(task["interval_value"], task["interval_unit"]), next_due_month=next_due.month if next_due else None)
        if rec:
            value, unit = m.days_to_interval(rec["interval_days"])
            rec.update(value=value, unit=unit, label=m.describe_interval(value, unit), analyzed_at=m.to_iso(self.now()))
        self._learn_cache[key] = rec
        return rec

    async def apply_recommendation(self, task_id: str, actor: Actor = SYSTEM) -> dict[str, Any]:
        task = await self._task(task_id)
        rec = await self.recommendation(task, refresh=True)
        if not rec:
            raise PlannerError("invalid", "There isn't enough history to recommend an interval yet")
        last = m.from_iso(task["last_done"])
        start = last.astimezone(self.tz).date() if last else self.today()
        changes: dict[str, Any] = {"interval_value": rec["value"], "interval_unit": rec["unit"]}
        if task["schedule_mode"] == "rolling":
            changes["next_due"] = m.season_shift(m.add_interval(start, rec["value"], rec["unit"]), task["active_months"]).isoformat()
        else:
            anchor = m.to_date(task["next_due"]) or start
            changes["anchor_date"] = anchor.isoformat()
        return await self.save_task({"id": task_id, **changes}, actor)

    # ------------------------------------------------------------------
    # Templates & battery fleet
    # ------------------------------------------------------------------
    async def _find_or_create_part(self, plan_id: str, object_id: str | None, spec: Mapping[str, Any]) -> dict[str, Any]:
        for part in await self.db.rows(MPARTS, {"plan_id": plan_id}):
            if part["part_name"].lower() == str(spec["part_name"]).lower():
                return part
        return await self.save_part(
            {
                "plan_id": plan_id,
                "object_id": object_id,
                "part_name": spec["part_name"],
                "reorder_threshold": spec.get("reorder_threshold", 0),
                "restock_quantity": spec.get("restock_quantity", 1),
                "current_stock": spec.get("current_stock", 0),
                "restock_action": spec.get("restock_action", RESTOCK_SHOPPING),
            }
        )

    async def apply_template(self, plan_id: str, template_id: str, actor: Actor = SYSTEM, *, parent_object_id: str | None = None, name: str | None = None) -> dict[str, Any]:
        template = m.get_template(template_id)
        if template is None:
            raise _not_found("That template")
        obj = await self.save_object({**template["object"], "plan_id": plan_id, "parent_object_id": parent_object_id, "name": name or template["object"]["name"]}, actor)
        existing_salt = obj["object_type"] == OBJECT_SOFTENER
        for spec in template["tasks"]:
            if existing_salt and any(t.get("type") == TRIGGER_SALT for t in spec.get("triggers") or []):
                task = next(t for t in await self.db.rows(MTASKS, {"object_id": obj["id"]}) if any(c.get("type") == TRIGGER_SALT for c in t["triggers"]))
            else:
                fields = {k: v for k, v in spec.items() if k != "parts"}
                fields["triggers"] = [t for t in spec.get("triggers") or [] if t.get("type") == TRIGGER_SALT or t.get("entity_id")]
                pending = [t for t in spec.get("triggers") or [] if t.get("type") != TRIGGER_SALT and not t.get("entity_id")]
                if pending:
                    hint = "; ".join(f"{t['type']} trigger" for t in pending)
                    fields["notes"] = f"Suggested: add a {hint} once you pick the sensor."
                if fields["schedule_mode"] in (SCHEDULE_NONE,) and not fields["triggers"]:
                    fields["schedule_mode"] = SCHEDULE_ONCE
                task = await self.save_task({**fields, "object_id": obj["id"]}, actor)
            parts = []
            for part_spec in spec.get("parts") or []:
                part = await self._find_or_create_part(plan_id, obj["id"], part_spec)
                parts.append({"part_id": part["id"], "quantity": part_spec.get("quantity", 1)})
            if parts:
                await self.save_task({"id": task["id"], "parts": parts}, actor)
        return obj

    async def battery_fleet(self, plan_id: str, states: Iterable[tuple[str, Any, Mapping[str, Any]]], actor: Actor = SYSTEM) -> dict[str, Any]:
        await self._plan(plan_id)
        fleet = next(iter(await self.db.rows(MOBJECTS, {"plan_id": plan_id, "object_type": OBJECT_FLEET})), None)
        if fleet is None:
            fleet = await self.save_object({"plan_id": plan_id, "name": "Battery Fleet", "object_type": OBJECT_FLEET, "icon": "mdi:battery-sync-outline", "notes": "Every battery-powered device Home Assistant knows about."}, actor)
        tracked = {cfg.get("entity_id") for t in await self.db.rows(MTASKS, {"plan_id": plan_id}) for cfg in t["triggers"]}
        created = []
        for entry in m.plan_battery_fleet(states, {e for e in tracked if e}):
            parts = []
            if entry["battery_type"]:
                part = await self._find_or_create_part(plan_id, fleet["id"], {"part_name": f"{entry['battery_type']} Battery", "reorder_threshold": 2, "restock_quantity": 4})
                parts.append({"part_id": part["id"], "quantity": 1})
            task = await self.save_task(
                {
                    "object_id": fleet["id"],
                    "name": f"Replace Battery — {entry['device']}",
                    "category": "Replacement",
                    "schedule_mode": SCHEDULE_NONE,
                    "triggers": [entry["trigger"]],
                    "spec_note": f"{entry['battery_type']} battery" if entry["battery_type"] else None,
                    "parts": parts,
                },
                actor,
            )
            created.append(task["id"])
        return {"object_id": fleet["id"], "created": len(created)}

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    def _trigger_results(self, task: Mapping[str, Any], obj: Mapping[str, Any]) -> list[m.TriggerResult]:
        results = []
        now = self.now()
        states = self._trigger_state.get(task["id"], {})
        for cfg in task["triggers"]:
            if cfg.get("type") == TRIGGER_SALT:
                if obj.get("object_type") == OBJECT_SOFTENER:
                    results.append(m.salt_trigger_result(self.salt_summary(obj), int(task.get("warning_days") or 0)))
                continue
            current, attrs = self._read(cfg.get("entity_id"))
            results.append(m.trigger_result(cfg, states.get(cfg["id"]), current=current, attrs=attrs, now=now))
        return results

    def event_view(self, event: Mapping[str, Any], objects: Mapping[str, Mapping[str, Any]], tasks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        obj = objects.get(event.get("object_id") or "")
        task = tasks.get(event.get("task_id") or "")
        return {
            **{k: event.get(k) for k in ("id", "object_id", "task_id", "number", "kind", "method", "occurred_at", "user_name", "person_entity_id", "cost", "parts_cost", "duration_minutes", "notes", "feedback", "interval_days", "photo_doc_id")},
            "data": event.get("data") or {},
            "code": _code(obj, task, event.get("number")) if task and obj else (_code(obj) if obj else ""),
            "task_name": task["name"] if task else None,
            "object_name": obj["name"] if obj else None,
            "total_cost": round(float(event.get("cost") or 0) + float(event.get("parts_cost") or 0), 2),
        }

    def task_view(
        self,
        task: Mapping[str, Any],
        obj: Mapping[str, Any],
        *,
        parts: Sequence[Mapping[str, Any]] = (),
        events: Sequence[Mapping[str, Any]] = (),
        recommendation: Mapping[str, Any] | None = None,
        area_names: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        status = self._status_of(task, obj)
        results = self._trigger_results(task, obj)
        triggers = []
        for cfg, result in zip([c for c in task["triggers"] if c.get("type") != TRIGGER_SALT or obj.get("object_type") == OBJECT_SOFTENER], results):
            triggers.append({**cfg, "met": result.met, "progress": round(min(result.progress, 9.99), 3), "label": result.label, "configured": result.configured})
        completed = [e for e in events if e["kind"] == EVENT_KIND_COMPLETED]
        costs = [float(e.get("cost") or 0) + float(e.get("parts_cost") or 0) for e in completed]
        durations = [int(e["duration_minutes"]) for e in completed if e.get("duration_minutes")]
        return {
            **{k: task.get(k) for k in (
                "id", "plan_id", "object_id", "number", "name", "category", "priority", "notes", "spec_note", "schedule_mode",
                "interval_value", "interval_unit", "anchor_date", "next_due", "last_done", "warning_days", "warning_percent",
                "schedule_time", "active_months", "trigger_logic", "assigned_to", "rotation", "nfc_tag_id", "require_nfc",
                "manual_doc_id", "manual_page", "archived", "event_seq",
            )},
            "on_complete_action": {k: v for k, v in task["on_complete_action"].items() if k != ACTION_CONFIGURED_BY} if task.get("on_complete_action") else None,
            "action_paused": bool(task.get("on_complete_action")) and not (task["on_complete_action"] or {}).get(ACTION_CONFIGURED_BY),
            "code": _code(obj, task),
            "object_name": obj["name"],
            "object_number": obj["number"],
            "area_id": obj.get("area_id"),
            "area_name": (area_names or {}).get(obj.get("area_id") or ""),
            "interval_label": m.describe_interval(task.get("interval_value"), task.get("interval_unit") or "months"),
            "triggers": triggers,
            "status": status.status,
            "days_left": status.days_left,
            "progress": round(status.progress, 3),
            "countdown": status.countdown,
            "in_season": status.in_season,
            "reasons": status.reasons,
            "due_at": m.to_iso(status.due_at) if status.due_at else None,
            "parts": list(parts),
            "stats": {
                "completions": len(completed),
                "avg_cost": round(sum(costs) / len(costs), 2) if costs else None,
                "avg_duration": round(sum(durations) / len(durations)) if durations else None,
            },
            "recommendation": recommendation,
        }

    def object_view(self, obj: Mapping[str, Any], task_views: Sequence[Mapping[str, Any]], area_names: Mapping[str, str]) -> dict[str, Any]:
        mine = [t for t in task_views if t["object_id"] == obj["id"]]
        warranty = m.to_date(obj.get("warranty_expiry_date"))
        worst = max((t["status"] for t in mine), key=lambda s: MAINT_SEVERITY[s], default=MAINT_OK)
        view = {
            **{k: obj.get(k) for k in (
                "id", "plan_id", "number", "name", "manufacturer_model", "object_type", "icon", "area_id", "zone_entity_id",
                "parent_object_id", "installed_date", "warranty_expiry_date", "warranty_reminder_days", "notes", "config", "archived",
            )},
            "code": _code(obj),
            "area_name": area_names.get(obj.get("area_id") or ""),
            "task_count": len(mine),
            "status": worst,
            "warranty_days_left": (warranty - self.today()).days if warranty else None,
        }
        if obj["object_type"] == OBJECT_SOFTENER:
            view["salt"] = self.salt_summary(obj)
        return view

    async def board(self, plan_id: str, actor: Actor = SYSTEM) -> dict[str, Any]:
        plan = await self._plan(plan_id)
        if not self.planner.can_access(plan, actor):
            raise _not_found("That maintenance plan")
        areas = self.areas()
        objects = await self.db.rows(MOBJECTS, {"plan_id": plan_id}, order=("number",))
        tasks = await self.db.rows(MTASKS, {"plan_id": plan_id}, order=("object_id", "number"))
        by_object = {o["id"]: o for o in objects}
        events = await self.db.rows(MEVENTS, {"plan_id": plan_id}, order=("occurred_at DESC",))
        events_by_task: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("task_id"):
                events_by_task.setdefault(event["task_id"], []).append(event)
        links = await self.db.task_parts([t["id"] for t in tasks])
        parts_by_task: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            parts_by_task.setdefault(link["task_id"], []).append({"part_id": link["part_id"], "quantity": link["quantity"]})
        task_views = []
        for task in tasks:
            obj = by_object.get(task["object_id"])
            if obj is None:
                continue
            history = list(reversed(events_by_task.get(task["id"], [])))
            rec = await self.recommendation(task, history)
            task_views.append(self.task_view(task, obj, parts=parts_by_task.get(task["id"], []), events=history, recommendation=rec, area_names=areas))
        object_views = [self.object_view(obj, task_views, areas) for obj in objects]
        parts = await self.db.rows(MPARTS, {"plan_id": plan_id}, order=("part_name",))
        part_views = []
        for part in parts:
            bound = [link["task_id"] for link in links if link["part_id"] == part["id"]]
            stock = float(part["current_stock"] or 0)
            part_views.append(
                {
                    **part,
                    "low": stock <= float(part["reorder_threshold"] or 0),
                    "inventory_value": round(stock * float(part["unit_cost"] or 0), 2) if part.get("unit_cost") else None,
                    "bound_tasks": bound,
                    "pending": bool(part.get("pending_ref")),
                }
            )
        tasks_by_id = {t["id"]: t for t in tasks}
        documents = await self.db.rows(MDOCS, {"plan_id": plan_id}, order=("created_at DESC",))
        visible = [t for t in task_views if not t["archived"]]
        settings = self._settings(plan)
        return {
            "plan": {"id": plan["id"], "name": plan["name"], "is_private": plan["is_private"], "settings": settings},
            "summary": {
                "objects": len([o for o in objects if not o["archived"]]),
                "tasks": len(visible),
                "overdue": sum(1 for t in visible if t["status"] == MAINT_OVERDUE),
                "due_soon": sum(1 for t in visible if t["status"] == "due_soon"),
                "triggered": sum(1 for t in visible if t["status"] == MAINT_TRIGGERED),
                "ok": sum(1 for t in visible if t["status"] == MAINT_OK),
            },
            "objects": object_views,
            "tasks": task_views,
            "parts": part_views,
            "documents": [{k: d.get(k) for k in ("id", "object_id", "task_id", "event_id", "category", "title", "kind", "url", "filename", "content_type", "size", "created_by", "created_at")} for d in documents],
            "events": [self.event_view(e, by_object, tasks_by_id) for e in events[:150]],
            "areas": [{"area_id": k, "name": v} for k, v in sorted(areas.items(), key=lambda kv: kv[1].lower())],
            "templates": m.template_catalog(),
            "spend": m.spend_report(events, by_object, areas, cost_mode=settings["cost_mode"], year=self.today().year, tz=self.tz),
            "nfc_listening": self._nfc_listen["task_id"] if self._nfc_listen and self._nfc_listen["expires"] > self.now() else None,
            "now": m.to_iso(self.now()),
            "today": self.today().isoformat(),
        }

    async def summary(self, plan_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Compact per-plan status for the main board (tab badges and My Day)."""
        out: dict[str, dict[str, Any]] = {}
        if not plan_ids:
            return out
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"plan_id": list(plan_ids)})}
        for task in await self.db.rows(MTASKS, {"plan_id": list(plan_ids), "archived": 0}):
            obj = objects.get(task["object_id"])
            if obj is None:
                continue
            status = self._status_of(task, obj)
            entry = out.setdefault(task["plan_id"], {"tasks": 0, "overdue": 0, "due_soon": 0, "triggered": 0, "attention": []})
            entry["tasks"] += 1
            if status.status != MAINT_OK:
                entry[status.status] += 1
                entry["attention"].append(
                    {
                        "id": task["id"],
                        "code": _code(obj, task),
                        "name": task["name"],
                        "object_name": obj["name"],
                        "object_id": obj["id"],
                        "plan_id": task["plan_id"],
                        "status": status.status,
                        "countdown": status.countdown,
                        "assigned_to": task["assigned_to"],
                        "due": status.due_date.isoformat() if status.due_date else None,
                        "require_nfc": task["require_nfc"],
                    }
                )
        for entry in out.values():
            entry["attention"].sort(key=lambda t: (-MAINT_SEVERITY[t["status"]], t["due"] or "9999"))
        return out

    async def todo_items(self, plan_id: str) -> list[dict[str, Any]]:
        """Light-weight rows for the native todo entity."""
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"plan_id": plan_id})}
        items = []
        for task in await self.db.rows(MTASKS, {"plan_id": plan_id, "archived": 0}, order=("object_id", "number")):
            obj = objects.get(task["object_id"])
            if obj is None:
                continue
            status = self._status_of(task, obj)
            items.append(
                {
                    "id": task["id"],
                    "summary": f"{obj['name']}: {task['name']}",
                    "status": status.status,
                    "countdown": status.countdown,
                    "due": task["next_due"],
                }
            )
        return items

    async def history(
        self,
        *,
        object_id: str | None = None,
        task_id: str | None = None,
        start: Any = None,
        end: Any = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {}
        if task_id:
            filters["task_id"] = task_id
        elif object_id:
            filters["object_id"] = object_id
        else:
            raise PlannerError("invalid", "Pick an object or a task")
        events = await self.db.rows(MEVENTS, filters, order=("occurred_at DESC",))
        start_day, end_day = m.to_date(start), m.to_date(end)
        if start_day or end_day:
            kept = []
            for event in events:
                moment = m.from_iso(event["occurred_at"])
                day = moment.astimezone(self.tz).date() if moment else None
                if day and (start_day is None or day >= start_day) and (end_day is None or day <= end_day):
                    kept.append(event)
            events = kept
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"id": list({e["object_id"] for e in events if e.get("object_id")})})} if events else {}
        tasks = {t["id"]: t for t in await self.db.rows(MTASKS, {"id": list({e["task_id"] for e in events if e.get("task_id")})})} if events else {}
        return [self.event_view(e, objects, tasks) for e in events[: max(1, min(int(limit), 2000))]]

    async def service_record_pdf(
        self, object_id: str, *, start: Any = None, end: Any = None, task_id: str | None = None, people: Mapping[str, str] | None = None
    ) -> bytes:
        obj = await self._object(object_id)
        plan = await self.db.get_plan(obj["plan_id"])
        settings = self._settings(plan or {})
        currency = settings["currency"]
        events = await self.history(object_id=object_id, task_id=task_id, start=start, end=end, limit=2000)
        events = [e for e in events if e["kind"] in (EVENT_KIND_COMPLETED, EVENT_KIND_SKIPPED, EVENT_KIND_MISSED, EVENT_KIND_PURCHASE)]
        events.reverse()

        def us_date(value: Any) -> str:
            moment = m.from_iso(value)
            return moment.astimezone(self.tz).strftime("%m/%d/%Y") if moment else ""

        rows = []
        total = 0.0
        for event in events:
            who = (people or {}).get(event.get("person_entity_id") or "") or event.get("user_name") or ""
            # Parts count once: when used (consumption) or when bought (purchase), per the plan's setting.
            cost = round(m.event_amount(event, settings["cost_mode"]), 2)
            total += cost
            parts = ", ".join(f"{p['quantity']:g}× {p['part_name']}" for p in (event.get("data") or {}).get("parts") or [])
            notes = "; ".join(x for x in (event.get("notes"), f"Parts: {parts}" if parts else None) if x)
            result = {"completed": "Completed", "skipped": "Skipped", "missed": "Missed", "purchase": "Purchased"}.get(event["kind"], event["kind"])
            if event.get("method") == "nfc":
                result += " (NFC)"
            rows.append(
                [
                    us_date(event["occurred_at"]),
                    f"{event.get('task_name') or (event.get('data') or {}).get('part_name') or ''} {event['code']}".strip(),
                    result,
                    who,
                    notes,
                    f"{cost:,.2f} {currency}" if cost else "",
                    f"{event['duration_minutes']} min" if event.get("duration_minutes") else "",
                ]
            )
        area = self.areas().get(obj.get("area_id") or "")
        subtitle = [" · ".join(x for x in (obj.get("manufacturer_model"), f"Area: {area}" if area else None, f"Installed: {us_date(obj['installed_date'] + 'T12:00:00+00:00')}" if obj.get("installed_date") else None) if x)]
        start_day, end_day = m.to_date(start), m.to_date(end)
        subtitle.append(
            "Period: " + (start_day.strftime("%m/%d/%Y") if start_day else "beginning") + " – " + (end_day.strftime("%m/%d/%Y") if end_day else "today")
        )
        return render_table_document(
            title=f"Service record — {obj['name']} ({_code(obj)})",
            subtitle_lines=[line for line in subtitle if line],
            columns=[Column("Date", 62), Column("Task", 108), Column("Result", 56), Column("By", 66), Column("Notes", 140), Column("Cost", 44, "right"), Column("Time", 40, "right")],
            rows=rows,
            footer_line=f"{len(rows)} entries · Total cost: {total:,.2f} {currency}",
            generated_line=f"Generated by TaskDragon on {self.now().astimezone(self.tz).strftime('%m/%d/%Y %I:%M %p')}",
        )

    async def spend(self, plan_id: str, year: int) -> dict[str, Any]:
        plan = await self._plan(plan_id)
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"plan_id": plan_id})}
        events = await self.db.rows(MEVENTS, {"plan_id": plan_id})
        return m.spend_report(events, objects, self.areas(), cost_mode=self._settings(plan)["cost_mode"], year=year, tz=self.tz)


# Re-exported for the WebSocket layer.
__all__ = ["COST_MODE_PURCHASE", "MaintenanceManager", "NFC_LISTEN_SECONDS"]
