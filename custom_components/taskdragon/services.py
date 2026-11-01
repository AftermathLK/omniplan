"""``taskdragon.*`` services for automations, scripts and voice pipelines.

A service call made by a signed-in user (from a dashboard button, a script they
ran or Assist) acts as that user, so it only sees household plans and that
user's own private lists. Calls without a user (automations) act as TaskDragon.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    BUCKETS,
    DOMAIN,
    LOW_ACTIONS,
    PRIORITIES,
    SNOOZE_RULE_TYPES,
    STATUS_COMPLETED,
    STOCK_STATES,
)
from .backup_manager import BackupError
from .coordinator import TaskDragonCoordinator
from .database import MOBJECTS, MTASKS
from .mealie_client import MealieError
from .planner import Actor, PlannerError
from .websocket_api import run_brain_dump

SERVICE_PARSE_BRAIN_DUMP = "parse_brain_dump"
SERVICE_ADD_TASK = "add_task"
SERVICE_COMPLETE_TASK = "complete_task"
SERVICE_SNOOZE_TASK = "snooze_task"
SERVICE_ADD_SHOPPING_ITEM = "add_shopping_item"
SERVICE_TRIGGER_PANTRY_LOW = "trigger_pantry_low"
SERVICE_RUN_SHIFT_BALANCE = "run_shift_balance"
SERVICE_SYNC_MEALIE = "sync_mealie"
SERVICE_CLONE_PLAN = "clone_plan"
SERVICE_RESET_PLAN = "reset_plan"
SERVICE_CREATE_TRIP = "create_trip"
SERVICE_COMPLETE_MAINTENANCE = "complete_maintenance"
SERVICE_LOG_SALT = "log_salt_level"
SERVICE_CREATE_BACKUP = "create_backup"

PARSE_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("agent_id"): cv.string,
        vol.Optional("dry_run", default=False): cv.boolean,
    }
)
ADD_TASK_SCHEMA = vol.Schema(
    {
        vol.Required("plan"): cv.string,
        vol.Required("summary"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("due_date"): cv.date,
        vol.Optional("due_datetime"): cv.datetime,
        vol.Optional("priority"): vol.In(PRIORITIES),
        vol.Optional("time_of_day_bucket"): vol.In(BUCKETS),
        vol.Optional("assigned_to"): cv.entity_ids,
        vol.Optional("context_tags"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("estimated_minutes"): vol.Coerce(int),
        vol.Optional("recurrence_rule"): cv.string,
        vol.Optional("recurrence_interval_days"): vol.Coerce(int),
        vol.Optional("shift_aware"): cv.boolean,
    }
)
COMPLETE_SCHEMA = vol.Schema(
    {
        vol.Exclusive("uid", "target"): cv.string,
        vol.Exclusive("summary", "target"): cv.string,
        vol.Optional("plan"): cv.string,
        vol.Optional("completed", default=True): cv.boolean,
    }
)
SNOOZE_SCHEMA = vol.Schema(
    {
        vol.Required("uid"): cv.string,
        vol.Required("rule_type"): vol.In(SNOOZE_RULE_TYPES),
        vol.Optional("until"): cv.datetime,
        vol.Optional("person"): cv.entity_id,
        vol.Optional("entity_id"): cv.entity_id,
        vol.Optional("state"): cv.string,
        vol.Optional("above"): vol.Coerce(float),
        vol.Optional("below"): vol.Coerce(float),
    }
)
SHOPPING_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional("plan"): cv.string,
        vol.Optional("category"): cv.string,
    }
)
PANTRY_SCHEMA = vol.Schema(
    {
        vol.Required("item"): cv.string,
        vol.Required("state"): vol.In(STOCK_STATES),
        vol.Optional("action"): vol.In(LOW_ACTIONS),
    }
)
CLONE_SCHEMA = vol.Schema({vol.Required("plan"): cv.string, vol.Optional("name"): cv.string, vol.Optional("start_date"): cv.date})
PLAN_SCHEMA = vol.Schema({vol.Required("plan"): cv.string})
MAINT_COMPLETE_SCHEMA = vol.Schema(
    {
        vol.Required("task"): cv.string,
        vol.Optional("cost"): vol.Coerce(float),
        vol.Optional("duration_minutes"): vol.Coerce(int),
        vol.Optional("notes"): cv.string,
    }
)
SALT_SCHEMA = vol.Schema(
    {
        vol.Required("object"): cv.string,
        vol.Required("kind"): vol.In(("fill", "level", "empty")),
        vol.Optional("level"): vol.Coerce(float),
        vol.Optional("bags"): vol.Coerce(float),
    }
)
TRIP_SCHEMA = vol.Schema(
    {
        vol.Required("plan"): cv.string,
        vol.Required("name"): cv.string,
        vol.Optional("start_date"): cv.date,
        vol.Optional("end_date"): cv.date,
    }
)


def _coordinator(hass: HomeAssistant) -> TaskDragonCoordinator:
    coordinator = hass.data.get(DOMAIN)
    if coordinator is None:
        raise HomeAssistantError("TaskDragon is not set up")
    return coordinator


async def _resolve_plan(coordinator: TaskDragonCoordinator, ref: str, actor: Actor) -> dict[str, Any]:
    """Find a plan the actor may use by id, exact name, or the plan's ``todo.*`` entity."""
    plans = await coordinator.planner.accessible_plans(actor, include_archived=True)
    needle = ref.strip()
    for plan in plans:
        if plan["id"] == needle or plan["name"].lower() == needle.lower():
            return plan
    if needle.startswith("todo."):
        entry = er.async_get(coordinator.hass).async_get(needle)
        if entry and entry.unique_id:
            plan_id = entry.unique_id.split("_", 1)[-1]
            for plan in plans:
                if plan["id"] == plan_id:
                    return plan
    raise ServiceValidationError(f"No TaskDragon plan matches '{ref}'")


@callback
def async_register_services(hass: HomeAssistant) -> None:
    async def parse_brain_dump(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        agent = call.data.get("agent_id")
        if agent and agent != coordinator.conversation_agent() and actor.user_id is not None and not actor.is_admin:
            raise Unauthorized(context=call.context)
        return await _run(run_brain_dump(
            coordinator, call.data["text"], actor, dry_run=call.data["dry_run"], agent_id=agent
        ))

    async def add_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        plan = await _resolve_plan(coordinator, call.data["plan"], actor)
        data: dict[str, Any] = {"plan_id": plan["id"], "summary": call.data["summary"]}
        for key in ("description", "priority", "time_of_day_bucket", "context_tags", "estimated_minutes", "shift_aware"):
            if key in call.data:
                data[key] = call.data[key]
        if "assigned_to" in call.data:
            data["assigned_to"] = list(call.data["assigned_to"])
        if "due_datetime" in call.data:
            data["due_datetime"] = call.data["due_datetime"].isoformat()
        elif "due_date" in call.data:
            data["due_date"] = call.data["due_date"].isoformat()
        if call.data.get("recurrence_rule"):
            data.update({"recurrence_mode": "calendar", "recurrence_rule": call.data["recurrence_rule"]})
        elif call.data.get("recurrence_interval_days"):
            data.update({"recurrence_mode": "rolling", "recurrence_interval_days": call.data["recurrence_interval_days"]})
        task = await _run(coordinator.planner.save_task(data, actor))
        return {"uid": task["uid"], "plan_id": task["plan_id"], "summary": task["summary"]}

    async def complete_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        uid = call.data.get("uid")
        if not uid:
            if "summary" not in call.data:
                raise ServiceValidationError("Provide a task uid or summary")
            if call.data.get("plan"):
                plan_ids = [(await _resolve_plan(coordinator, call.data["plan"], actor))["id"]]
            else:
                plan_ids = [plan["id"] for plan in await coordinator.planner.accessible_plans(actor)]
            wanted = call.data["summary"].strip().lower()
            tasks = await coordinator.db.list_tasks(plan_ids=plan_ids)
            matches = [t for t in tasks if t["summary"].lower() == wanted and (t["status"] != STATUS_COMPLETED) == call.data["completed"]]
            if not matches:
                raise ServiceValidationError(f"No task named '{call.data['summary']}'")
            uid = matches[0]["uid"]
        await _run(coordinator.planner.authorize(actor, tasks=[uid]))
        result = await _run(coordinator.planner.set_completed(uid, call.data["completed"], actor))
        return {"uid": uid, "status": result["task"]["status"], "next_due": result["next_due"], "unblocked": result["unblocked"]}

    async def snooze_task(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        config = {key: call.data[key] for key in ("person", "entity_id", "state", "above", "below") if key in call.data}
        if "until" in call.data:
            config["until"] = call.data["until"].isoformat()
        await _run(coordinator.planner.authorize(actor, tasks=[call.data["uid"]]))
        await _run(coordinator.planner.snooze_context(call.data["uid"], call.data["rule_type"], config, actor))

    async def add_shopping_item(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        plan = (
            await _resolve_plan(coordinator, call.data["plan"], actor)
            if call.data.get("plan")
            else await coordinator.planner.grocery_plan()
        )
        if plan is None:
            raise ServiceValidationError("No shopping plan exists")
        result = await _run(
            coordinator.planner.add_shopping_item(plan["id"], call.data["text"], actor, category=call.data.get("category"))
        )
        return {"uid": result["task"]["uid"], "merged": result["merged"], "quantity": result["task"]["quantity"]}

    async def trigger_pantry_low(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        ref = call.data["item"].strip().lower()
        visible = {plan["id"] for plan in await coordinator.planner.accessible_plans(actor)}
        items = [i for i in await coordinator.db.list_pantry() if i["plan_id"] in visible]
        item = next((i for i in items if i["id"] == call.data["item"] or i["name"].lower() == ref), None)
        if item is None:
            raise ServiceValidationError(f"No pantry item matches '{call.data['item']}'")
        result = await _run(coordinator.planner.set_pantry_state(item["id"], call.data["state"], call.data.get("action"), actor))
        return {"action_needed": result["action_needed"], "action": result["action"], "created": result["created"]}

    async def run_shift_balance(call: ServiceCall) -> ServiceResponse:
        await _require_admin(hass, call)
        return await _coordinator(hass).async_run_shift_balance()

    async def sync_mealie(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        if coordinator.mealie is None:
            raise ServiceValidationError("Mealie is not configured")
        return dict(await coordinator.async_sync_mealie())

    async def clone_plan(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        plan = await _resolve_plan(coordinator, call.data["plan"], actor)
        start = call.data.get("start_date")
        created = await _run(
            coordinator.planner.clone_plan(plan["id"], call.data.get("name"), start.isoformat() if start else None, actor)
        )
        return {"plan_id": created["id"], "name": created["name"]}

    async def reset_plan(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        plan = await _resolve_plan(coordinator, call.data["plan"], actor)
        return {"reset": await _run(coordinator.planner.reset_plan(plan["id"]))}

    async def create_trip(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        plan = await _resolve_plan(coordinator, call.data["plan"], actor)
        start, end = call.data.get("start_date"), call.data.get("end_date")
        result = await _run(
            coordinator.planner.create_trip(
                plan["id"],
                call.data["name"],
                start.isoformat() if start else None,
                end.isoformat() if end else None,
                actor,
            )
        )
        return {"trip_id": result["trip"]["id"], "status": result["trip"]["status"], "added": result["added"]}

    hass.services.async_register(DOMAIN, SERVICE_PARSE_BRAIN_DUMP, parse_brain_dump, PARSE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_ADD_TASK, add_task, ADD_TASK_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_COMPLETE_TASK, complete_task, COMPLETE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_SNOOZE_TASK, snooze_task, SNOOZE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ADD_SHOPPING_ITEM, add_shopping_item, SHOPPING_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_TRIGGER_PANTRY_LOW, trigger_pantry_low, PANTRY_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RUN_SHIFT_BALANCE, run_shift_balance, vol.Schema({}), SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_MEALIE, sync_mealie, vol.Schema({}), SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CLONE_PLAN, clone_plan, CLONE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_RESET_PLAN, reset_plan, PLAN_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CREATE_TRIP, create_trip, TRIP_SCHEMA, SupportsResponse.OPTIONAL)

    async def complete_maintenance(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        task = await _resolve_maintenance_task(coordinator, call.data["task"], actor)
        result = await _run(
            coordinator.maintenance.manager.complete(
                task["id"], actor, method="service", cost=call.data.get("cost"), duration_minutes=call.data.get("duration_minutes"), notes=call.data.get("notes")
            )
        )
        return {"task_id": task["id"], "event": result["event"]["code"], "next_due": result["next_due"]}

    async def log_salt_level(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass)
        actor = await coordinator.actor_for_user_id(call.context.user_id)
        obj = await _resolve_maintenance_object(coordinator, call.data["object"], actor)
        manager = coordinator.maintenance.manager
        if call.data["kind"] == "fill":
            await _run(manager.salt_fill(obj["id"], actor, level=call.data.get("level"), bags=call.data.get("bags")))
        else:
            await _run(manager.salt_log(obj["id"], call.data["kind"], level=call.data.get("level"), actor=actor, source="service"))
        current = await coordinator.db.row(MOBJECTS, obj["id"])
        return manager.salt_summary(current)

    hass.services.async_register(DOMAIN, SERVICE_COMPLETE_MAINTENANCE, complete_maintenance, MAINT_COMPLETE_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_LOG_SALT, log_salt_level, SALT_SCHEMA, SupportsResponse.OPTIONAL)

    async def create_backup(call: ServiceCall) -> ServiceResponse:
        """A portable snapshot into <config>/backups/taskdragon (e.g. before an update); old ones are pruned.

        Like an admin service: users must be administrators, automations (no user) may call it.
        (``async_register_admin_service`` cannot return a response on every supported core.)
        """
        await _require_admin(hass, call)
        coordinator = _coordinator(hass)
        try:
            return await coordinator.backups.create_snapshot("snapshot")
        except BackupError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, SERVICE_CREATE_BACKUP, create_backup, vol.Schema({}), SupportsResponse.OPTIONAL)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Administrators only; calls without a user (automations, scripts run by the system) are allowed."""
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None:
            raise UnknownUser(context=call.context)
        if not user.is_admin:
            raise Unauthorized(context=call.context)


async def _maintenance_rows(coordinator: TaskDragonCoordinator, actor: Actor) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plans = [p["id"] for p in await coordinator.planner.accessible_plans(actor) if p["archetype"] == "home_maintenance"]
    objects = await coordinator.db.rows(MOBJECTS, {"plan_id": plans})
    tasks = await coordinator.db.rows(MTASKS, {"plan_id": plans})
    return objects, tasks


async def _resolve_maintenance_task(coordinator: TaskDragonCoordinator, ref: str, actor: Actor) -> dict[str, Any]:
    """A maintenance task by id, "#3.1" code, "Furnace / Replace Filter" or a unique task name."""
    needle = ref.strip()
    objects, tasks = await _maintenance_rows(coordinator, actor)
    by_object = {o["id"]: o for o in objects}
    lowered = needle.lower()
    for task in tasks:
        obj = by_object.get(task["object_id"])
        if obj is None:
            continue
        code = f"#{obj['number']}.{task['number']}"
        if needle in (task["id"], code) or lowered == f"{obj['name']} / {task['name']}".lower():
            return task
    matches = [t for t in tasks if t["name"].lower() == lowered]
    if len(matches) == 1:
        return matches[0]
    raise ServiceValidationError(f"No single maintenance task matches '{ref}'")


async def _resolve_maintenance_object(coordinator: TaskDragonCoordinator, ref: str, actor: Actor) -> dict[str, Any]:
    needle = ref.strip()
    objects, _ = await _maintenance_rows(coordinator, actor)
    matches = [o for o in objects if needle in (o["id"], f"#{o['number']}") or o["name"].lower() == needle.lower()]
    if len(matches) == 1:
        return matches[0]
    raise ServiceValidationError(f"No single maintenance object matches '{ref}'")


async def _run(coro):
    """Translate planner errors into Home Assistant service errors."""
    try:
        return await coro
    except PlannerError as err:
        raise ServiceValidationError(err.message) from err
    except MealieError as err:
        raise HomeAssistantError(str(err)) from err
