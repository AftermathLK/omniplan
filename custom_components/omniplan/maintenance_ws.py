"""WebSocket commands for the maintenance subsystem (``omniplan/maint_*``).

Every command goes through :func:`.websocket_api._command`, so it is
authenticated, every object/task/part/document id is authorized against the
signed-in user before the handler runs, and errors come back as codes. Undo
payloads stay on the server behind tokens.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    COST_MODES,
    DOC_CATEGORIES,
    FEEDBACK_CHOICES,
    INTERVAL_UNITS,
    MAINT_CATEGORIES,
    MAX_SUMMARY,
    MAX_TEXT,
    OBJECT_TYPES,
    PRIORITIES,
    RESTOCK_ACTIONS,
    SALT_OBS_KINDS,
    SCHEDULE_MODES,
    TRIGGER_LOGIC,
)
from .planner import PlannerError
from .qr import encode_rows
from .websocket_api import DATE, ID, IDS, NAME, OPT_ID, OPT_NAME, _command, _text

NUMBER = vol.Any(None, "", vol.Coerce(float))

OBJECT_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): ID,
        vol.Optional("plan_id"): ID,
        vol.Optional("name"): NAME,
        vol.Optional("manufacturer_model"): OPT_NAME,
        vol.Optional("object_type"): vol.In(OBJECT_TYPES),
        vol.Optional("icon"): vol.Any(None, _text(64)),
        vol.Optional("area_id"): vol.Any(None, _text(64)),
        vol.Optional("zone_entity_id"): vol.Any(None, _text(255)),
        vol.Optional("parent_object_id"): OPT_ID,
        vol.Optional("installed_date"): DATE,
        vol.Optional("warranty_expiry_date"): DATE,
        vol.Optional("warranty_reminder_days"): NUMBER,
        vol.Optional("notes"): vol.Any(None, _text(MAX_TEXT)),
        vol.Optional("archived"): bool,
        vol.Optional("config"): vol.Schema({vol.Optional(str): vol.Any(None, str, int, float, bool)}, extra=vol.REMOVE_EXTRA),
    }
)
TRIGGER_SCHEMA = vol.Schema(
    {
        vol.Required("type"): _text(20),
        vol.Optional("id"): vol.Any(None, _text(40)),
        vol.Optional("entity_id"): vol.Any(None, _text(255)),
        vol.Optional("above"): NUMBER,
        vol.Optional("below"): NUMBER,
        vol.Optional("for_minutes"): NUMBER,
        vol.Optional("delta"): NUMBER,
        vol.Optional("unit"): vol.Any(None, _text(20)),
        vol.Optional("target_state"): vol.Any(None, _text(100)),
        vol.Optional("attribute"): vol.Any(None, _text(64)),
        vol.Optional("target_hours"): NUMBER,
        vol.Optional("from_state"): vol.Any(None, _text(100)),
        vol.Optional("to_state"): vol.Any(None, _text(100)),
        vol.Optional("count"): NUMBER,
        vol.Optional("debounce_seconds"): NUMBER,
        vol.Optional("auto_complete"): bool,
    },
    extra=vol.REMOVE_EXTRA,
)
PART_LINK = vol.Schema({vol.Required("part_id"): ID, vol.Optional("quantity"): NUMBER})
TASK_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): ID,
        vol.Optional("object_id"): ID,
        vol.Optional("name"): NAME,
        vol.Optional("category"): vol.In(MAINT_CATEGORIES),
        vol.Optional("priority"): vol.In(PRIORITIES),
        vol.Optional("notes"): vol.Any(None, _text(MAX_TEXT)),
        vol.Optional("spec_note"): vol.Any(None, _text(MAX_SUMMARY)),
        vol.Optional("schedule_mode"): vol.In(SCHEDULE_MODES),
        vol.Optional("interval_value"): NUMBER,
        vol.Optional("interval_unit"): vol.In(INTERVAL_UNITS),
        vol.Optional("next_due"): DATE,
        vol.Optional("anchor_date"): DATE,
        vol.Optional("last_done"): DATE,
        vol.Optional("warning_days"): NUMBER,
        vol.Optional("warning_percent"): NUMBER,
        vol.Optional("schedule_time"): vol.Any(None, _text(8)),
        vol.Optional("active_months"): vol.All([vol.Coerce(int)], vol.Length(max=12)),
        vol.Optional("triggers"): vol.All([TRIGGER_SCHEMA], vol.Length(max=10)),
        vol.Optional("trigger_logic"): vol.In(TRIGGER_LOGIC),
        vol.Optional("assigned_to"): vol.Any(None, _text(255)),
        vol.Optional("rotation"): vol.All([_text(255)], vol.Length(max=20)),
        vol.Optional("nfc_tag_id"): vol.Any(None, _text(128)),
        vol.Optional("require_nfc"): bool,
        vol.Optional("on_complete_action"): vol.Any(
            None,
            vol.Schema(
                {
                    vol.Required("service"): _text(100),
                    vol.Optional("entity_id"): vol.Any(_text(255), vol.All([_text(255)], vol.Length(max=20))),
                    vol.Optional("data"): vol.Schema({vol.Optional(str): vol.Any(str, int, float, bool)}),
                }
            ),
        ),
        vol.Optional("manual_doc_id"): OPT_ID,
        vol.Optional("manual_page"): NUMBER,
        vol.Optional("archived"): bool,
        vol.Optional("parts"): vol.All([PART_LINK], vol.Length(max=20)),
    }
)
PART_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): ID,
        vol.Optional("plan_id"): ID,
        vol.Optional("object_id"): OPT_ID,
        vol.Optional("part_name"): NAME,
        vol.Optional("mpn_gtin"): vol.Any(None, _text(64)),
        vol.Optional("storage_location"): OPT_NAME,
        vol.Optional("current_stock"): NUMBER,
        vol.Optional("reorder_threshold"): NUMBER,
        vol.Optional("restock_quantity"): NUMBER,
        vol.Optional("unit_cost"): NUMBER,
        vol.Optional("reorder_url"): vol.Any(None, _text(2048)),
        vol.Optional("restock_action"): vol.In(RESTOCK_ACTIONS),
        vol.Optional("notes"): vol.Any(None, _text(2000)),
    }
)


def _manager(coordinator: Any) -> Any:
    return coordinator.maintenance.manager


def _undo(coordinator: Any, kind: str, payload: Any, actor: Any) -> str:
    return coordinator.planner.stash_undo(kind, payload, actor)


@_command({vol.Required("type"): "omniplan/maint_board", vol.Required("plan_id"): ID}, guard=lambda m: {"plans": [m["plan_id"]]})
async def ws_board(hass, connection, msg, coordinator, actor):
    board = await _manager(coordinator).board(msg["plan_id"], actor)
    board["people"] = coordinator.people_info()
    board["me"] = coordinator.person_for_user(actor.user_id)
    return board


@_command(
    {vol.Required("type"): "omniplan/maint_save_object", vol.Required("object"): OBJECT_SCHEMA},
    guard=lambda m: {"mobjects": [m["object"].get("id"), m["object"].get("parent_object_id")], "plans": [m["object"].get("plan_id")]},
)
async def ws_save_object(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).save_object(msg["object"], actor)


@_command({vol.Required("type"): "omniplan/maint_delete_object", vol.Required("object_id"): ID}, guard=lambda m: {"mobjects": [m["object_id"]]})
async def ws_delete_object(hass, connection, msg, coordinator, actor):
    payload = await _manager(coordinator).delete_object(msg["object_id"])
    return {"undo": _undo(coordinator, "maint_object", payload, actor)}


@_command(
    {vol.Required("type"): "omniplan/maint_save_task", vol.Required("task"): TASK_SCHEMA},
    guard=lambda m: {
        "mtasks": [m["task"].get("id")],
        "mobjects": [m["task"].get("object_id")],
        "mdocs": [m["task"].get("manual_doc_id")],
        "mparts": [p["part_id"] for p in m["task"].get("parts") or []],
    },
)
async def ws_save_task(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).save_task(msg["task"], actor)


@_command({vol.Required("type"): "omniplan/maint_delete_task", vol.Required("task_id"): ID}, guard=lambda m: {"mtasks": [m["task_id"]]})
async def ws_delete_task(hass, connection, msg, coordinator, actor):
    payload = await _manager(coordinator).delete_task(msg["task_id"])
    return {"undo": _undo(coordinator, "maint_task", payload, actor)}


@_command(
    {
        vol.Required("type"): "omniplan/maint_complete",
        vol.Required("task_id"): ID,
        vol.Optional("cost"): NUMBER,
        vol.Optional("duration_minutes"): NUMBER,
        vol.Optional("notes"): vol.Any(None, _text(4000)),
        vol.Optional("feedback"): vol.Any(None, vol.In(FEEDBACK_CHOICES)),
        vol.Optional("performed_by"): vol.Any(None, _text(255)),
        vol.Optional("photo_doc_id"): OPT_ID,
        vol.Optional("parts"): vol.All([PART_LINK], vol.Length(max=20)),
        vol.Optional("completed_at"): DATE,
        vol.Optional("salt"): vol.Schema({vol.Optional("level"): NUMBER, vol.Optional("bags"): NUMBER}),
        vol.Optional("method"): vol.In(("manual", "qr")),
    },
    guard=lambda m: {"mtasks": [m["task_id"]], "mdocs": [m.get("photo_doc_id")], "mparts": [p["part_id"] for p in m.get("parts") or []]},
)
async def ws_complete(hass, connection, msg, coordinator, actor):
    result = await _manager(coordinator).complete(
        msg["task_id"],
        actor,
        method=msg.get("method", "manual"),
        cost=msg.get("cost"),
        duration_minutes=msg.get("duration_minutes"),
        notes=msg.get("notes"),
        feedback=msg.get("feedback"),
        performed_by=msg.get("performed_by"),
        photo_doc_id=msg.get("photo_doc_id"),
        parts=msg.get("parts"),
        completed_at=msg.get("completed_at"),
        salt=msg.get("salt"),
    )
    return {"event": result["event"], "next_due": result["next_due"], "undo": _undo(coordinator, "maint_completion", result["undo"], actor)}


@_command(
    {vol.Required("type"): "omniplan/maint_skip", vol.Required("task_id"): ID, vol.Optional("notes"): vol.Any(None, _text(4000))},
    guard=lambda m: {"mtasks": [m["task_id"]]},
)
async def ws_skip(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).skip(msg["task_id"], actor, msg.get("notes"))


@_command(
    {
        vol.Required("type"): "omniplan/maint_bulk",
        vol.Required("task_ids"): IDS,
        vol.Required("action"): vol.In(("complete", "skip", "assign", "delete", "priority")),
        vol.Optional("value"): vol.Any(None, _text(255)),
    },
    guard=lambda m: {"mtasks": m["task_ids"]},
)
async def ws_bulk(hass, connection, msg, coordinator, actor):
    manager = _manager(coordinator)
    done, errors = 0, []
    for task_id in msg["task_ids"]:
        try:
            if msg["action"] == "complete":
                await manager.complete(task_id, actor)
            elif msg["action"] == "skip":
                await manager.skip(task_id, actor)
            elif msg["action"] == "delete":
                await manager.delete_task(task_id)
            elif msg["action"] == "assign":
                await manager.save_task({"id": task_id, "assigned_to": msg.get("value") or None}, actor)
            elif msg["action"] == "priority":
                await manager.save_task({"id": task_id, "priority": msg.get("value") or "none"}, actor)
            done += 1
        except Exception as err:  # noqa: BLE001 - report per task, keep going
            errors.append({"task_id": task_id, "message": getattr(err, "message", str(err))})
    return {"count": done, "errors": errors}


@_command(
    {
        vol.Required("type"): "omniplan/maint_history",
        vol.Optional("object_id"): ID,
        vol.Optional("task_id"): ID,
        vol.Optional("from"): DATE,
        vol.Optional("to"): DATE,
        vol.Optional("limit", default=500): vol.All(int, vol.Range(min=1, max=2000)),
    },
    guard=lambda m: {"mobjects": [m.get("object_id")], "mtasks": [m.get("task_id")]},
)
async def ws_history(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).history(object_id=msg.get("object_id"), task_id=msg.get("task_id"), start=msg.get("from"), end=msg.get("to"), limit=msg["limit"])


@_command(
    {vol.Required("type"): "omniplan/maint_save_part", vol.Required("part"): PART_SCHEMA},
    guard=lambda m: {"mparts": [m["part"].get("id")], "plans": [m["part"].get("plan_id")], "mobjects": [m["part"].get("object_id")]},
)
async def ws_save_part(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).save_part(msg["part"], actor)


@_command({vol.Required("type"): "omniplan/maint_delete_part", vol.Required("part_id"): ID}, guard=lambda m: {"mparts": [m["part_id"]]})
async def ws_delete_part(hass, connection, msg, coordinator, actor):
    payload = await _manager(coordinator).delete_part(msg["part_id"])
    return {"undo": _undo(coordinator, "maint_part", payload, actor)}


@_command(
    {vol.Required("type"): "omniplan/maint_adjust_stock", vol.Required("part_id"): ID, vol.Required("delta"): vol.All(vol.Coerce(float), vol.Range(min=-10000, max=10000))},
    guard=lambda m: {"mparts": [m["part_id"]]},
)
async def ws_adjust_stock(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).adjust_stock(msg["part_id"], msg["delta"], actor)


@_command(
    {
        vol.Required("type"): "omniplan/maint_add_link",
        vol.Required("object_id"): ID,
        vol.Required("url"): _text(2048),
        vol.Optional("title"): OPT_NAME,
        vol.Optional("category"): vol.In(DOC_CATEGORIES),
        vol.Optional("task_id"): OPT_ID,
    },
    guard=lambda m: {"mobjects": [m["object_id"]], "mtasks": [m.get("task_id")]},
)
async def ws_add_link(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).add_document(
        {"object_id": msg["object_id"], "kind": "link", "url": msg["url"], "title": msg.get("title"), "category": msg.get("category"), "task_id": msg.get("task_id")},
        actor,
    )


@_command({vol.Required("type"): "omniplan/maint_delete_document", vol.Required("document_id"): ID}, guard=lambda m: {"mdocs": [m["document_id"]]})
async def ws_delete_document(hass, connection, msg, coordinator, actor):
    payload = await _manager(coordinator).delete_document(msg["document_id"])
    return {"undo": _undo(coordinator, "maint_document", payload, actor)}


@_command(
    {
        vol.Required("type"): "omniplan/maint_salt_log",
        vol.Required("object_id"): ID,
        vol.Required("kind"): vol.In(SALT_OBS_KINDS),
        vol.Optional("level"): NUMBER,
        vol.Optional("bags"): NUMBER,
    },
    guard=lambda m: {"mobjects": [m["object_id"]]},
)
async def ws_salt_log(hass, connection, msg, coordinator, actor):
    manager = _manager(coordinator)
    if msg["kind"] == "fill":
        result = await manager.salt_fill(msg["object_id"], actor, level=msg.get("level"), bags=msg.get("bags"))
        if "undo" in result:
            return {"event": result["event"], "undo": _undo(coordinator, "maint_completion", result["undo"], actor)}
        return result
    return {"observation": await manager.salt_log(msg["object_id"], msg["kind"], level=msg.get("level"), actor=actor)}


@_command({vol.Required("type"): "omniplan/maint_nfc_listen", vol.Required("task_id"): ID}, guard=lambda m: {"mtasks": [m["task_id"]]})
async def ws_nfc_listen(hass, connection, msg, coordinator, actor):
    manager = _manager(coordinator)
    expires = manager.start_nfc_binding(msg["task_id"], actor)
    task = await manager._task(msg["task_id"])  # noqa: SLF001 - same subsystem
    coordinator.planner.notify([task["plan_id"]])
    return {"expires": expires.isoformat()}


def _base_url(hass: Any, origin: str | None) -> str:
    try:
        return get_url(hass, prefer_external=True)
    except NoURLAvailableError:
        parts = urlsplit(origin or "")
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return ""


@_command(
    {
        vol.Required("type"): "omniplan/maint_qr",
        vol.Optional("task_id"): ID,
        vol.Optional("object_id"): ID,
        vol.Optional("action"): vol.In(("open", "complete")),
        vol.Optional("origin"): vol.Any(None, _text(255)),
    },
    guard=lambda m: {"mtasks": [m.get("task_id")], "mobjects": [m.get("object_id")]},
)
async def ws_qr(hass, connection, msg, coordinator, actor):
    base = _base_url(hass, msg.get("origin"))
    if msg.get("task_id"):
        url = f"{base}/omniplan?maint_task={msg['task_id']}" + ("&do=complete" if msg.get("action") == "complete" else "")
    elif msg.get("object_id"):
        url = f"{base}/omniplan?maint_object={msg['object_id']}"
    else:
        raise PlannerError("invalid", "Pick a task or an object")
    return {"url": url, "rows": encode_rows(url)}


@_command(
    {vol.Required("type"): "omniplan/maint_recommendation", vol.Required("task_id"): ID, vol.Optional("apply", default=False): bool},
    guard=lambda m: {"mtasks": [m["task_id"]]},
)
async def ws_recommendation(hass, connection, msg, coordinator, actor):
    manager = _manager(coordinator)
    if msg["apply"]:
        return {"task": await manager.apply_recommendation(msg["task_id"], actor)}
    task = await manager._task(msg["task_id"])  # noqa: SLF001 - same subsystem
    return {"recommendation": await manager.recommendation(task, refresh=True)}


@_command(
    {
        vol.Required("type"): "omniplan/maint_apply_template",
        vol.Required("plan_id"): ID,
        vol.Required("template_id"): _text(40),
        vol.Optional("parent_object_id"): OPT_ID,
        vol.Optional("name"): OPT_NAME,
    },
    guard=lambda m: {"plans": [m["plan_id"]], "mobjects": [m.get("parent_object_id")]},
)
async def ws_apply_template(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).apply_template(msg["plan_id"], msg["template_id"], actor, parent_object_id=msg.get("parent_object_id"), name=msg.get("name"))


@_command({vol.Required("type"): "omniplan/maint_battery_fleet", vol.Required("plan_id"): ID}, guard=lambda m: {"plans": [m["plan_id"]]})
async def ws_battery_fleet(hass, connection, msg, coordinator, actor):
    states = [(s.entity_id, s.state, dict(s.attributes)) for domain in ("sensor", "binary_sensor") for s in hass.states.async_all(domain)]
    return await _manager(coordinator).battery_fleet(msg["plan_id"], states, actor)


@_command(
    {vol.Required("type"): "omniplan/maint_spend", vol.Required("plan_id"): ID, vol.Required("year"): vol.All(int, vol.Range(min=2000, max=2200))},
    guard=lambda m: {"plans": [m["plan_id"]]},
)
async def ws_spend(hass, connection, msg, coordinator, actor):
    return await _manager(coordinator).spend(msg["plan_id"], msg["year"])


@_command(
    {
        vol.Required("type"): "omniplan/maint_save_settings",
        vol.Required("plan_id"): ID,
        vol.Required("settings"): vol.Schema(
            {
                vol.Optional("cost_mode"): vol.In(COST_MODES),
                vol.Optional("currency"): vol.Any(None, _text(8)),
                vol.Optional("notify_service"): vol.Any(None, _text(100)),
                vol.Optional("default_warning_days"): vol.Any(None, vol.Coerce(int)),
                vol.Optional("shopping_plan_id"): OPT_ID,
                vol.Optional("chore_plan_id"): OPT_ID,
            }
        ),
    },
    guard=lambda m: {"plans": [m["plan_id"], m["settings"].get("shopping_plan_id"), m["settings"].get("chore_plan_id")]},
)
async def ws_save_settings(hass, connection, msg, coordinator, actor):
    plan = await coordinator.planner.update_plan(msg["plan_id"], {"settings": msg["settings"]}, actor)
    return {"settings": _manager(coordinator)._settings(plan)}  # noqa: SLF001 - same subsystem


COMMANDS = (
    ws_board,
    ws_save_object,
    ws_delete_object,
    ws_save_task,
    ws_delete_task,
    ws_complete,
    ws_skip,
    ws_bulk,
    ws_history,
    ws_save_part,
    ws_delete_part,
    ws_adjust_stock,
    ws_add_link,
    ws_delete_document,
    ws_salt_log,
    ws_nfc_listen,
    ws_qr,
    ws_recommendation,
    ws_apply_template,
    ws_battery_fleet,
    ws_spend,
    ws_save_settings,
)
__all__ = ["COMMANDS"]
