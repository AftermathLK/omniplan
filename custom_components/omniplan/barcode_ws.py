"""WebSocket commands for camera barcode scanning (``omniplan/barcode_*``).

Like every OmniPlan command these go through :func:`.websocket_api._command`:
the user is authenticated, every pantry item, part, plan and object id in the
message is authorized before the handler runs, and the manager hides mappings
whose target the user cannot see. Changing the Open Food Facts setting needs an
administrator.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from .const import BARCODE_ACTIONS, BARCODE_CONTEXTS, BARCODE_PANTRY, BARCODE_PART, BARCODE_SYMBOLOGIES, BARCODE_TARGETS, LOW_ACTIONS, STOCK_STATES
from .planner import PlannerError
from .websocket_api import ID, NAME, OPT_ID, OPT_NAME, _command

BARCODE = vol.All(str, vol.Length(min=1, max=256))
SYMBOLOGY = vol.Any(None, vol.In(BARCODE_SYMBOLOGIES))
AMOUNT = vol.Any(None, "", vol.All(vol.Coerce(float), vol.Range(min=0, max=1e9)))
CREATE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): NAME,
        vol.Optional("plan_id"): OPT_ID,
        vol.Optional("object_id"): OPT_ID,
        vol.Optional("category"): OPT_NAME,
        vol.Optional("storage_location"): OPT_NAME,
        vol.Optional("current_stock"): AMOUNT,
        vol.Optional("reorder_threshold"): AMOUNT,
        vol.Optional("restock_quantity"): AMOUNT,
    }
)


def _target_guard(msg: dict[str, Any]) -> dict[str, list[str | None]]:
    target = msg.get("target_id")
    create = msg.get("create") or {}
    return {
        "pantry": [target] if msg.get("target_type") == BARCODE_PANTRY else [],
        "mparts": [target] if msg.get("target_type") == BARCODE_PART else [],
        "plans": [create.get("plan_id")],
        "mobjects": [create.get("object_id")],
    }


@_command(
    {
        vol.Required("type"): "omniplan/barcode_lookup",
        vol.Required("barcode"): BARCODE,
        vol.Optional("format"): SYMBOLOGY,
        vol.Optional("context", default="auto"): vol.In(BARCODE_CONTEXTS),
        vol.Optional("apply_default", default=False): bool,
    }
)
async def ws_barcode_lookup(hass, connection, msg, coordinator, actor):
    """What a scanned code is linked to; unknown retail codes get an Open Food Facts suggestion."""
    return await coordinator.barcodes.lookup(
        msg["barcode"], actor, symbology=msg.get("format"), context=msg["context"], apply_default=msg["apply_default"]
    )


@_command(
    {
        vol.Required("type"): "omniplan/barcode_bind",
        vol.Required("barcode"): BARCODE,
        vol.Optional("format"): SYMBOLOGY,
        vol.Required("target_type"): vol.In(BARCODE_TARGETS),
        vol.Exclusive("target_id", "target"): ID,
        vol.Exclusive("create", "target"): CREATE_SCHEMA,
        vol.Optional("default_delta_qty", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
        vol.Optional("product_name"): OPT_NAME,
        vol.Optional("brand"): OPT_NAME,
        vol.Optional("apply_default", default=False): bool,
    },
    guard=_target_guard,
)
async def ws_barcode_bind(hass, connection, msg, coordinator, actor):
    if not msg.get("target_id") and not msg.get("create"):
        raise PlannerError("invalid", "Pick an item or create a new one")
    return await coordinator.barcodes.bind(
        msg["barcode"],
        actor,
        target_type=msg["target_type"],
        target_id=msg.get("target_id"),
        create=msg.get("create"),
        symbology=msg.get("format"),
        default_delta_qty=msg["default_delta_qty"],
        product_name=msg.get("product_name"),
        brand=msg.get("brand"),
        apply_default=msg["apply_default"],
    )


@_command({vol.Required("type"): "omniplan/barcode_unbind", vol.Required("barcode"): BARCODE})
async def ws_barcode_unbind(hass, connection, msg, coordinator, actor):
    return await coordinator.barcodes.unbind(msg["barcode"], actor)


@_command(
    {vol.Required("type"): "omniplan/barcode_list", vol.Required("target_type"): vol.In(BARCODE_TARGETS), vol.Required("target_id"): ID},
    guard=_target_guard,
)
async def ws_barcode_list(hass, connection, msg, coordinator, actor):
    return {"barcodes": await coordinator.barcodes.list_for(msg["target_type"], msg["target_id"], actor)}


@_command(
    {
        vol.Required("type"): "omniplan/barcode_apply_action",
        vol.Required("barcode"): BARCODE,
        vol.Optional("format"): SYMBOLOGY,
        vol.Required("action"): vol.In(BARCODE_ACTIONS),
        vol.Optional("quantity"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0.001, max=10000))),
        vol.Optional("status"): vol.Any(None, vol.In(STOCK_STATES)),
        vol.Optional("low_action"): vol.Any(None, vol.In(LOW_ACTIONS)),
    }
)
async def ws_barcode_apply_action(hass, connection, msg, coordinator, actor):
    """Use / restock a part or set a pantry item's stock, then log it (undo token included)."""
    return await coordinator.barcodes.apply_action(
        msg["barcode"],
        actor,
        action=msg["action"],
        symbology=msg.get("format"),
        quantity=msg.get("quantity"),
        status=msg.get("status"),
        low_action=msg.get("low_action"),
    )


@_command({vol.Required("type"): "omniplan/barcode_settings", vol.Optional("settings"): vol.Schema({vol.Optional("openfoodfacts"): bool})})
async def ws_barcode_settings(hass, connection, msg, coordinator, actor):
    if "settings" in msg:
        if not actor.is_admin:
            raise PlannerError("unauthorized", "Only an administrator can change barcode settings")
        return await coordinator.barcodes.save_settings(msg["settings"])
    return await coordinator.barcodes.settings()


COMMANDS = (ws_barcode_lookup, ws_barcode_bind, ws_barcode_unbind, ws_barcode_list, ws_barcode_apply_action, ws_barcode_settings)
__all__ = ["COMMANDS"]
