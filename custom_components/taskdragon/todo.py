"""Native Home Assistant to-do entities: one ``todo.*`` entity per TaskDragon plan.

These keep TaskDragon visible to Assist, automations and the stock to-do panel.
Rich metadata (assignees, subtasks, snoozes…) stays in the custom WebSocket API;
here tasks are flattened to the fields ``TodoItem`` supports. Snoozed items and
subtasks are hidden from the native view. Pantry plans map "in stock" to a
completed item and "low/out" to an item that needs action. Maintenance plans
list "Object: Task" items that need action while Due Soon, Overdue or
Triggered; checking one off completes that maintenance task. Packing items show
their trip as a ``[Trip name]`` prefix; items of archived trips are hidden.

Entity state and ``todo.get_items`` are readable by every Home Assistant user,
so private plans never get an entity, and every write through an entity is
limited to that entity's own plan.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ARCHETYPE_MAINTENANCE,
    ARCHETYPE_PACKING,
    ARCHETYPE_PANTRY,
    DOMAIN,
    SIGNAL_PLAN_ADDED,
    SIGNAL_PLAN_REMOVED,
    SIGNAL_UPDATED,
    STATUS_COMPLETED,
    STATUS_NEEDS_ACTION,
    STATUS_SNOOZED,
    STOCK_IN,
    STOCK_LOW,
    TRIP_ARCHIVED,
    VERSION,
)
from .coordinator import TaskDragonCoordinator
from .planner import PlannerError
from .timeutil import parse_date, parse_datetime

_LOGGER = logging.getLogger(__name__)

SUPPORTED = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.MOVE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)
MAINTENANCE_SUPPORTED = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)
PANTRY_SUPPORTED = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: TaskDragonCoordinator = entry.runtime_data
    entities: dict[str, TaskDragonTodoList] = {}

    def build(plan: dict[str, Any]) -> TaskDragonTodoList:
        entity = TaskDragonTodoList(coordinator, plan)
        entities[plan["id"]] = entity
        return entity

    plans = [plan for plan in await coordinator.db.list_plans() if not plan["is_private"]]
    async_add_entities([build(plan) for plan in plans], update_before_add=True)

    @callback
    def plan_added(plan: dict[str, Any]) -> None:
        if plan["id"] not in entities and not plan.get("archived") and not plan.get("is_private"):
            async_add_entities([build(plan)], update_before_add=True)

    @callback
    def plan_removed(plan_id: str) -> None:
        entity = entities.pop(plan_id, None)
        if entity is None:
            return
        registry = er.async_get(hass)
        if entity.entity_id and registry.async_get(entity.entity_id):
            registry.async_remove(entity.entity_id)
        else:
            hass.async_create_task(entity.async_remove())

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_PLAN_ADDED, plan_added))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_PLAN_REMOVED, plan_removed))


class TaskDragonTodoList(TodoListEntity):
    """A plan exposed as a native to-do list."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: TaskDragonCoordinator, plan: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self._plan_id = plan["id"]
        self._archetype = plan["archetype"]
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{plan['id']}"
        self._attr_name = plan["name"]
        self._attr_icon = plan.get("icon")
        self._attr_supported_features = (
            PANTRY_SUPPORTED
            if plan["archetype"] == ARCHETYPE_PANTRY
            else MAINTENANCE_SUPPORTED
            if plan["archetype"] == ARCHETYPE_MAINTENANCE
            else SUPPORTED
        )
        self._maint: dict[str, dict[str, Any]] = {}
        self._attr_todo_items = []
        self._trip_names: dict[str, str] = {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="TaskDragon",
            manufacturer="TaskDragon",
            model="Household planner",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_extra_state_attributes = {"plan_id": plan["id"], "archetype": plan["archetype"]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._handle_update))

    @callback
    def _handle_update(self, plan_ids: set[str]) -> None:
        if self._plan_id in plan_ids and not self.coordinator.closing:
            self.async_schedule_update_ha_state(True)

    async def _async_refresh(self) -> None:
        """Write our own state right away after a change made through this entity."""
        await self.async_update()
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    async def async_update(self) -> None:
        if self.coordinator.closing:
            return
        try:
            plan = await self.coordinator.db.get_plan(self._plan_id)
            if plan is None or plan["is_private"]:
                self._attr_todo_items = []
                return
            self._attr_name = plan["name"]
            self._attr_icon = plan.get("icon")
            if self._archetype == ARCHETYPE_PANTRY:
                items = await self.coordinator.db.list_pantry(self._plan_id)
                self._attr_todo_items = [self._pantry_item(item) for item in items]
                low = sum(1 for item in items if item["stock_state"] != STOCK_IN)
                self._attr_extra_state_attributes = {"plan_id": self._plan_id, "archetype": self._archetype, "low_or_out": low}
                return
            if self._archetype == ARCHETYPE_MAINTENANCE:
                items = await self.coordinator.maintenance.manager.todo_items(self._plan_id)
                self._maint = {item["id"]: item for item in items}
                self._attr_todo_items = [
                    TodoItem(
                        summary=item["summary"],
                        uid=item["id"],
                        status=TodoItemStatus.COMPLETED if item["status"] == "ok" else TodoItemStatus.NEEDS_ACTION,
                        due=parse_date(item["due"]),
                        description=f"{item['status'].replace('_', ' ').title()} · {item['countdown']}",
                    )
                    for item in items
                ]
                self._attr_extra_state_attributes = {
                    "plan_id": self._plan_id,
                    "archetype": self._archetype,
                    **{status: sum(1 for i in items if i["status"] == status) for status in ("overdue", "due_soon", "triggered")},
                }
                return
            tasks = await self.coordinator.db.list_tasks(self._plan_id, top_level_only=True)
            if self._archetype == ARCHETYPE_PACKING:
                trips = await self.coordinator.db.list_trips([self._plan_id])
                archived = {trip["id"] for trip in trips if trip["status"] == TRIP_ARCHIVED}
                self._trip_names = {trip["id"]: trip["name"] for trip in trips}
                tasks = [task for task in tasks if task["trip_id"] not in archived]
            visible = [task for task in tasks if task["status"] != STATUS_SNOOZED]
            self._attr_todo_items = [self._task_item(task) for task in visible]
            today = self.coordinator.planner.today().isoformat()
            self._attr_extra_state_attributes = {
                "plan_id": self._plan_id,
                "archetype": self._archetype,
                "is_template": plan["is_template"],
                "overdue": sum(
                    1 for t in visible if t["status"] != STATUS_COMPLETED and t["due_date"] and t["due_date"] < today
                ),
                "due_today": sum(1 for t in visible if t["status"] != STATUS_COMPLETED and t["due_date"] == today),
                "snoozed": len(tasks) - len(visible),
            }
        except (ValueError, RuntimeError, sqlite3.Error) as err:
            _LOGGER.debug("Could not refresh %s: %s", self.entity_id, err)

    def _display_summary(self, task: dict[str, Any]) -> str:
        summary = task["summary"]
        if task.get("quantity"):
            summary = f"{summary} ({task['quantity']})"
        trip = self._trip_names.get(task.get("trip_id") or "")
        return f"[{trip}] {summary}" if trip else summary

    def _task_item(self, task: dict[str, Any]) -> TodoItem:
        due: date | datetime | None = None
        if task["due_datetime"]:
            moment = parse_datetime(task["due_datetime"])
            due = moment.astimezone(self.coordinator.tz) if moment else None
        elif task["due_date"]:
            due = parse_date(task["due_date"])
        return TodoItem(
            summary=self._display_summary(task),
            uid=task["uid"],
            status=TodoItemStatus.COMPLETED if task["status"] == STATUS_COMPLETED else TodoItemStatus.NEEDS_ACTION,
            due=due,
            description=task["description"] or None,
        )

    @staticmethod
    def _pantry_item(item: dict[str, Any]) -> TodoItem:
        state = {"in_stock": "In stock", "low": "Low", "out": "Out"}.get(item["stock_state"], item["stock_state"])
        return TodoItem(
            summary=item["name"],
            uid=item["id"],
            status=TodoItemStatus.COMPLETED if item["stock_state"] == STOCK_IN else TodoItemStatus.NEEDS_ACTION,
            description=f"{state}" + (f" — {item['notes']}" if item.get("notes") else ""),
        )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    async def _actor(self):
        context = getattr(self, "_context", None)
        return await self.coordinator.actor_for_user_id(context.user_id if context else None)

    async def _own_task(self, uid: str | None) -> dict[str, Any]:
        """A task of *this* plan; uids from other plans are treated as unknown."""
        task = await self.coordinator.db.get_task(uid or "")
        if task is None or task["plan_id"] != self._plan_id:
            raise ServiceValidationError(f"Item {uid} is not on this list")
        return task

    async def _own_maintenance_task(self, task_id: str | None) -> dict[str, Any]:
        from .database import MTASKS

        task = await self.coordinator.db.row(MTASKS, task_id or "")
        if task is None or task["plan_id"] != self._plan_id:
            raise ServiceValidationError(f"Item {task_id} is not on this list")
        return task

    async def _update_maintenance(self, item: TodoItem, actor) -> None:
        task = await self._own_maintenance_task(item.uid)
        manager = self.coordinator.maintenance.manager
        current = self._maint.get(task["id"]) or {}
        name = item.summary or ""
        prefix = current.get("summary", "").split(": ", 1)[0] + ": " if ": " in current.get("summary", "") else ""
        if prefix and name.startswith(prefix):
            name = name[len(prefix) :]
        changes: dict[str, Any] = {}
        if name and name != task["name"]:
            changes["name"] = name
        due = item.due.date() if isinstance(item.due, datetime) else item.due
        if due is not None and due.isoformat() != task["next_due"]:
            changes["next_due"] = due.isoformat()
        status_text = f"{current.get('status', '').replace('_', ' ').title()} · {current.get('countdown', '')}"
        if item.description and item.description != status_text and item.description != task["notes"]:
            changes["notes"] = item.description
        try:
            if changes:
                await manager.save_task({"id": task["id"], **changes}, actor)
            if item.status == TodoItemStatus.COMPLETED and current.get("status", "ok") != "ok":
                await manager.complete(task["id"], actor, method="todo")
        except PlannerError as err:
            raise ServiceValidationError(err.message) from err

    async def _own_pantry_item(self, item_id: str | None) -> dict[str, Any]:
        item = await self.coordinator.db.get_pantry_item(item_id or "")
        if item is None or item["plan_id"] != self._plan_id:
            raise ServiceValidationError(f"Item {item_id} is not on this list")
        return item

    async def async_create_todo_item(self, item: TodoItem) -> None:
        actor = await self._actor()
        if self._archetype == ARCHETYPE_PANTRY:
            await self.coordinator.planner.save_pantry_item(
                {
                    "plan_id": self._plan_id,
                    "name": item.summary,
                    "stock_state": STOCK_IN if item.status == TodoItemStatus.COMPLETED else STOCK_LOW,
                }
            )
            await self._async_refresh()
            return
        data: dict[str, Any] = {"plan_id": self._plan_id, "summary": item.summary, "description": item.description}
        if self._archetype == ARCHETYPE_PACKING:
            data["trip_id"] = await self.coordinator.planner.default_trip_id(self._plan_id)
        data.update(_due_fields(item.due))
        if item.status == TodoItemStatus.COMPLETED:
            data["status"] = STATUS_COMPLETED
        try:
            await self.coordinator.planner.save_task(data, actor)
        except PlannerError as err:
            raise ValueError(err.message) from err
        await self._async_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        actor = await self._actor()
        if self._archetype == ARCHETYPE_MAINTENANCE:
            await self._update_maintenance(item, actor)
            await self._async_refresh()
            return
        if self._archetype == ARCHETYPE_PANTRY:
            existing = await self._own_pantry_item(item.uid)
            if item.summary and item.summary != existing["name"]:
                await self.coordinator.planner.save_pantry_item({"id": existing["id"], "name": item.summary})
            wanted = STOCK_IN if item.status == TodoItemStatus.COMPLETED else (
                existing["stock_state"] if existing["stock_state"] != STOCK_IN else STOCK_LOW
            )
            if wanted != existing["stock_state"]:
                await self.coordinator.planner.set_pantry_state(existing["id"], wanted, actor=actor)
            await self._async_refresh()
            return
        task = await self._own_task(item.uid)
        data: dict[str, Any] = {"uid": task["uid"]}
        summary = item.summary or task["summary"]
        trip = self._trip_names.get(task.get("trip_id") or "")
        if trip and summary.startswith(f"[{trip}] "):
            summary = summary[len(trip) + 3 :]
        if task.get("quantity") and summary == f"{task['summary']} ({task['quantity']})":
            summary = task["summary"]
        if summary != task["summary"]:
            data["summary"] = summary
        if (item.description or None) != (task["description"] or None):
            data["description"] = item.description
        current_due = self._task_item(task).due
        if item.due != current_due:
            data.update(_due_fields(item.due))
        completed = item.status == TodoItemStatus.COMPLETED
        if completed != (task["status"] == STATUS_COMPLETED):
            data["status"] = STATUS_COMPLETED if completed else STATUS_NEEDS_ACTION
        if len(data) > 1:
            try:
                await self.coordinator.planner.save_task(data, actor)
            except PlannerError as err:
                raise ValueError(err.message) from err
        await self._async_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        if self._archetype == ARCHETYPE_MAINTENANCE:
            manager = self.coordinator.maintenance.manager
            for uid in uids:
                await self._own_maintenance_task(uid)
            for uid in uids:
                await manager.delete_task(uid)
            await self._async_refresh()
            return
        if self._archetype == ARCHETYPE_PANTRY:
            for uid in uids:
                await self._own_pantry_item(uid)
            for uid in uids:
                await self.coordinator.planner.delete_pantry_item(uid)
        else:
            for uid in uids:
                await self._own_task(uid)
            await self.coordinator.planner.delete_tasks(uids)
        await self._async_refresh()

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        await self._own_task(uid)
        if previous_uid:
            await self._own_task(previous_uid)
        try:
            await self.coordinator.planner.move_task(uid, previous_uid)
        except PlannerError as err:
            raise ValueError(err.message) from err
        await self._async_refresh()


def _due_fields(due: date | datetime | None) -> dict[str, Any]:
    if due is None:
        return {"due_date": None, "due_datetime": None}
    if isinstance(due, datetime):
        return {"due_datetime": due.isoformat()}
    return {"due_date": due.isoformat(), "due_datetime": None}
