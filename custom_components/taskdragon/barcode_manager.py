"""Camera barcode scanning: barcode → pantry item / maintenance part mappings and scan actions.

No Home Assistant imports. The coordinator injects the Open Food Facts lookup
(any ``async (code) -> summary | None`` callable), so everything here runs in
plain unit tests.

A mapping links one normalized barcode to one pantry item or one maintenance
part. Several barcodes may point at the same target, so two brands of 40 lb
softener salt (or two filter makers) both count against the same part.

Privacy follows the rest of TaskDragon: a mapping whose target lives in a plan the
user cannot see behaves like an unknown barcode, the item pickers only list
what the user can see, and only the digits of retail barcodes are ever sent to
Open Food Facts (and only while that lookup is switched on).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from . import maintenance as m
from .barcodes import BarcodeError, best_match, display_barcode, lookup_gtin, normalize_barcode, suggested_title
from .const import (
    ARCHETYPE_MAINTENANCE,
    ARCHETYPE_PANTRY,
    BARCODE_ACTIONS,
    BARCODE_PANTRY,
    BARCODE_PART,
    BARCODE_RESTOCK,
    BARCODE_SET_PANTRY,
    BARCODE_TARGETS,
    BARCODE_USE_ONE,
    EVENT_BARCODE_ACTION,
    EVENT_KIND_PART_USED,
    EVENT_KIND_PURCHASE,
    LOW_ACTION_NONE,
    LOW_ACTIONS,
    MAX_NAME,
    STATUS_COMPLETED,
    STOCK_IN,
    STOCK_STATES,
)
from .database import MEVENTS, MOBJECTS, MPARTS
from .openfoodfacts import LookupFailed
from .planner import Actor, PlannerError

SETTINGS_META = "barcode_settings"
MAX_DELTA = 1000
MAX_RESTOCK = 10000
MAX_OPTIONS = 1500

Lookup = Callable[[str], Awaitable["dict[str, Any] | None"]]


def _not_linked() -> PlannerError:
    return PlannerError("not_found", "That barcode isn't linked to anything you can see yet")


def _round(value: Any) -> float:
    number = float(value or 0)
    return int(number) if number.is_integer() else round(number, 3)


class BarcodeManager:
    """Everything behind the card's 📷 Scan buttons."""

    def __init__(self, *, db: Any, planner: Any, lookup: Lookup | None = None) -> None:
        self.db = db
        self.planner = planner
        self._lookup = lookup
        planner.undo_handlers.update({"barcode_part": self._undo_part, "barcode_pantry": self._undo_pantry})

    @property
    def _maintenance(self) -> Any:
        if self.planner.maintenance is None:
            raise PlannerError("invalid", "Maintenance is not available")
        return self.planner.maintenance

    @staticmethod
    def _normalize(raw: Any, symbology: str | None) -> tuple[str, str]:
        try:
            return normalize_barcode(raw, symbology)
        except BarcodeError as err:
            raise PlannerError("invalid", str(err)) from err

    # ------------------------------------------------------------------
    # Settings (household-wide)
    # ------------------------------------------------------------------
    async def settings(self) -> dict[str, Any]:
        stored = await self.db.get_meta(SETTINGS_META, {}) or {}
        return {"openfoodfacts": bool(stored.get("openfoodfacts", True))}

    async def save_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        current = await self.settings()
        if "openfoodfacts" in changes:
            current["openfoodfacts"] = bool(changes["openfoodfacts"])
        await self.db.set_meta(SETTINGS_META, current)
        return current

    # ------------------------------------------------------------------
    # What the user can see
    # ------------------------------------------------------------------
    async def _plans(self, actor: Actor, archetype: str) -> list[dict[str, Any]]:
        return [p for p in await self.planner.accessible_plans(actor) if p["archetype"] == archetype]

    @staticmethod
    def _pantry_view(item: Mapping[str, Any], plan: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "id": item["id"],
            "name": item["name"],
            "category": item.get("category"),
            "quantity": item.get("quantity"),
            "stock_state": item["stock_state"],
            "plan_id": item["plan_id"],
            "plan_name": (plan or {}).get("name"),
        }

    @staticmethod
    def _part_view(part: Mapping[str, Any], obj: Mapping[str, Any] | None, plan: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "id": part["id"],
            "part_name": part["part_name"],
            "current_stock": _round(part["current_stock"]),
            "reorder_threshold": _round(part["reorder_threshold"]),
            "restock_quantity": _round(part["restock_quantity"]),
            "storage_location": part.get("storage_location"),
            "unit_cost": part.get("unit_cost"),
            "low": float(part["current_stock"] or 0) <= float(part["reorder_threshold"] or 0),
            "plan_id": part["plan_id"],
            "plan_name": (plan or {}).get("name"),
            "object_id": part.get("object_id"),
            "object_name": (obj or {}).get("name"),
            "object_number": (obj or {}).get("number"),
        }

    async def _target(self, target_type: str, target_id: str | None, actor: Actor) -> dict[str, Any] | None:
        """The target's card-ready view, or None when it is gone or not shared with ``actor``."""
        if not target_id:
            return None
        if target_type == BARCODE_PANTRY:
            item = await self.db.get_pantry_item(target_id)
            plan = await self.db.get_plan(item["plan_id"]) if item else None
            if item is None or not self.planner.can_access(plan, actor):
                return None
            return {"target_type": BARCODE_PANTRY, "target_id": item["id"], "item": self._pantry_view(item, plan)}
        if target_type == BARCODE_PART:
            part = await self.db.row(MPARTS, target_id)
            plan = await self.db.get_plan(part["plan_id"]) if part else None
            if part is None or not self.planner.can_access(plan, actor):
                return None
            obj = await self.db.row(MOBJECTS, part["object_id"]) if part.get("object_id") else None
            return {"target_type": BARCODE_PART, "target_id": part["id"], "part": self._part_view(part, obj, plan)}
        return None

    async def _visible_parts(self, actor: Actor) -> list[dict[str, Any]]:
        plans = {p["id"]: p for p in await self._plans(actor, ARCHETYPE_MAINTENANCE)}
        if not plans:
            return []
        objects = {o["id"]: o for o in await self.db.rows(MOBJECTS, {"plan_id": list(plans)})}
        return [
            self._part_view(part, objects.get(part.get("object_id")), plans.get(part["plan_id"]))
            for part in await self.db.rows(MPARTS, {"plan_id": list(plans)}, order=("part_name",))
        ]

    async def _visible_pantry(self, actor: Actor) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for plan in await self._plans(actor, ARCHETYPE_PANTRY):
            out.extend(self._pantry_view(item, plan) for item in await self.db.list_pantry(plan["id"]))
        return out

    async def _resolve(self, code: str, actor: Actor) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """``(mapping, target view)``; a part whose MPN/GTIN field holds the code counts as linked."""
        mapping = await self.db.get_barcode(code)
        if mapping is not None:
            view = await self._target(mapping["target_type"], mapping["target_id"], actor)
            if view is not None:
                return mapping, {**view, "mapped": True, "default_delta_qty": int(mapping["default_delta_qty"] or 1)}
        plan_ids = [p["id"] for p in await self._plans(actor, ARCHETYPE_MAINTENANCE)]
        for part in await self.db.rows(MPARTS, {"plan_id": plan_ids}) if plan_ids else []:
            gtin = str(part.get("mpn_gtin") or "").strip()
            if not gtin:
                continue
            try:
                same = self._normalize(gtin, None)[0] == code
            except PlannerError:
                continue
            if same:
                view = await self._target(BARCODE_PART, part["id"], actor)
                if view is not None:
                    return None, {**view, "mapped": False, "default_delta_qty": 1}
        return mapping, None

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    async def lookup(
        self, raw: Any, actor: Actor, *, symbology: str | None = None, context: str = "auto", apply_default: bool = False
    ) -> dict[str, Any]:
        """What a scan refers to. Unknown codes come back with a product suggestion and item pickers.

        With ``apply_default`` a pantry staple is marked In Stock right away (the
        put-the-groceries-away flow); parts always wait for an explicit action.
        """
        code, kind = self._normalize(raw, symbology)
        result: dict[str, Any] = {
            "barcode": code,
            "display": display_barcode(code),
            "symbology": kind,
            "context": context,
            "match": None,
            "suggestion": None,
            "options": None,
            "lookup": None,
            "applied": None,
        }
        mapping, view = await self._resolve(code, actor)
        if view is not None:
            if mapping is not None:
                await self.db.count_barcode_scan(code)
            result["match"] = view
            if apply_default and view["target_type"] == BARCODE_PANTRY:
                applied = await self._set_pantry(view, STOCK_IN, None, actor, code)
                result["match"] = applied.pop("match")
                result["applied"] = applied
            return result

        summary = None
        if lookup_gtin(code) is None:
            result["lookup"] = "not_retail"
        elif self._lookup is None or not (await self.settings())["openfoodfacts"]:
            result["lookup"] = "disabled"
        else:
            try:
                summary = await self._lookup(code)
                result["lookup"] = "found" if summary else "not_found"
            except LookupFailed:
                result["lookup"] = "error"
        title = suggested_title(summary)
        if summary:
            result["suggestion"] = {**summary, "title": title}
        pantry = await self._visible_pantry(actor)
        parts = await self._visible_parts(actor)
        # Match on the name plus the product's categories ("water softener salts" finds "Softener salt").
        query = " ".join([title or "", *((summary or {}).get("categories") or [])])
        pantry_match = best_match(query, pantry, "name")
        part_match = best_match(query, parts, "part_name")
        result["options"] = {
            "pantry": pantry[:MAX_OPTIONS],
            "parts": parts[:MAX_OPTIONS],
            "pantry_plans": [{"id": p["id"], "name": p["name"]} for p in await self._plans(actor, ARCHETYPE_PANTRY)],
            "maintenance_plans": [{"id": p["id"], "name": p["name"]} for p in await self._plans(actor, ARCHETYPE_MAINTENANCE)],
            "objects": await self._objects(actor),
            "suggested_pantry_id": pantry_match["id"] if pantry_match else None,
            "suggested_part_id": part_match["id"] if part_match else None,
        }
        return result

    async def _objects(self, actor: Actor) -> list[dict[str, Any]]:
        plans = [p["id"] for p in await self._plans(actor, ARCHETYPE_MAINTENANCE)]
        if not plans:
            return []
        return [
            {"id": o["id"], "name": o["name"], "number": o["number"], "plan_id": o["plan_id"]}
            for o in await self.db.rows(MOBJECTS, {"plan_id": plans, "archived": 0}, order=("number",))
        ]

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------
    async def bind(
        self,
        raw: Any,
        actor: Actor,
        *,
        target_type: str,
        target_id: str | None = None,
        create: Mapping[str, Any] | None = None,
        symbology: str | None = None,
        default_delta_qty: int = 1,
        product_name: str | None = None,
        brand: str | None = None,
        apply_default: bool = False,
    ) -> dict[str, Any]:
        """Link a barcode to an existing item/part, or to one created on the spot from ``create``."""
        if target_type not in BARCODE_TARGETS:
            raise PlannerError("invalid", "Link the barcode to a pantry item or a maintenance part")
        code, kind = self._normalize(raw, symbology)
        delta = int(default_delta_qty or 1)
        if not 1 <= delta <= MAX_DELTA:
            raise PlannerError("invalid", f"The default quantity must be between 1 and {MAX_DELTA}")
        existing = await self.db.get_barcode(code)
        if existing is not None and await self._target(existing["target_type"], existing["target_id"], actor) is None:
            if await self._exists(existing["target_type"], existing["target_id"]):
                raise PlannerError("in_use", "That barcode is already linked to something that isn't shared with you")
        created = False
        if create:
            target_id = await self._create(target_type, create, code, actor)
            created = True
        view = await self._target(target_type, target_id, actor)
        if view is None:
            raise PlannerError("not_found", "That item was not found (or isn't shared with you)")
        replaced = None
        if existing is not None and (existing["target_type"], existing["target_id"]) != (target_type, view["target_id"]):
            old = await self._target(existing["target_type"], existing["target_id"], actor)
            if old is not None:
                replaced = {"target_type": old["target_type"], "name": (old.get("item") or {}).get("name") or (old.get("part") or {}).get("part_name")}
        mapping = await self.db.save_barcode(
            {
                "barcode": code,
                "target_type": target_type,
                "target_id": view["target_id"],
                "default_delta_qty": delta,
                "symbology": kind,
                "product_name": (product_name or "").strip()[:MAX_NAME] or None,
                "brand": (brand or "").strip()[:MAX_NAME] or None,
                "created_by": actor.label,
            }
        )
        plan_id = (view.get("item") or view.get("part") or {}).get("plan_id")
        self.planner.notify([plan_id])
        result: dict[str, Any] = {
            "mapping": self._mapping_view(mapping),
            "match": {**view, "mapped": True, "default_delta_qty": delta},
            "replaced": replaced,
            "created": created,
            "applied": None,
        }
        if apply_default and target_type == BARCODE_PANTRY:
            applied = await self._set_pantry(result["match"], STOCK_IN, None, actor, code)
            result["match"] = applied.pop("match")
            result["applied"] = applied
        return result

    async def _exists(self, target_type: str, target_id: str) -> bool:
        if target_type == BARCODE_PANTRY:
            return await self.db.get_pantry_item(target_id) is not None
        return await self.db.row(MPARTS, target_id) is not None

    async def _create(self, target_type: str, create: Mapping[str, Any], code: str, actor: Actor) -> str:
        name = " ".join(str(create.get("name") or "").split())[:MAX_NAME]
        if not name:
            raise PlannerError("invalid", "Give the new item a name")
        if target_type == BARCODE_PANTRY:
            plans = await self._plans(actor, ARCHETYPE_PANTRY)
            plan = next((p for p in plans if p["id"] == create.get("plan_id")), None) if create.get("plan_id") else (plans[0] if plans else None)
            if plan is None:
                raise PlannerError("no_plan", "Create a Pantry & Spices plan first")
            item = await self.planner.save_pantry_item(
                {"plan_id": plan["id"], "name": name, "category": create.get("category") or None, "stock_state": STOCK_IN}
            )
            return item["id"]
        objects = {o["id"]: o for o in await self._objects(actor)}
        object_id = create.get("object_id") or None
        if object_id and object_id not in objects:
            raise PlannerError("not_found", "That object was not found (or isn't shared with you)")
        plans = await self._plans(actor, ARCHETYPE_MAINTENANCE)
        plan_id = objects[object_id]["plan_id"] if object_id else create.get("plan_id") or (plans[0]["id"] if plans else None)
        if not plan_id or plan_id not in {p["id"] for p in plans}:
            raise PlannerError("no_plan", "Create a Home Maintenance plan first")
        fields: dict[str, Any] = {"plan_id": plan_id, "object_id": object_id, "part_name": name}
        for key in ("current_stock", "reorder_threshold", "restock_quantity", "storage_location"):
            if create.get(key) not in (None, ""):
                fields[key] = create[key]
        if lookup_gtin(code):
            fields["mpn_gtin"] = display_barcode(code)
        part = await self._maintenance.save_part(fields, actor)
        return part["id"]

    @staticmethod
    def _mapping_view(mapping: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "barcode": mapping["barcode"],
            "display": display_barcode(mapping["barcode"]),
            "symbology": mapping.get("symbology"),
            "target_type": mapping["target_type"],
            "target_id": mapping["target_id"],
            "default_delta_qty": int(mapping.get("default_delta_qty") or 1),
            "product_name": mapping.get("product_name"),
            "brand": mapping.get("brand"),
            "scan_count": int(mapping.get("scan_count") or 0),
            "last_scanned_at": mapping.get("last_scanned_at"),
            "created_at": mapping.get("created_at"),
        }

    async def unbind(self, raw: Any, actor: Actor) -> dict[str, Any]:
        code, _ = self._normalize(raw, None)
        mapping = await self.db.get_barcode(code)
        if mapping is None:
            raise _not_linked()
        view = await self._target(mapping["target_type"], mapping["target_id"], actor)
        if view is None and await self._exists(mapping["target_type"], mapping["target_id"]):
            raise _not_linked()
        await self.db.delete_barcode(code)
        if view is not None:
            self.planner.notify([(view.get("item") or view.get("part") or {}).get("plan_id")])
        return {"barcode": code, "removed": True}

    async def list_for(self, target_type: str, target_id: str, actor: Actor) -> list[dict[str, Any]]:
        if await self._target(target_type, target_id, actor) is None:
            raise PlannerError("not_found", "That item was not found (or isn't shared with you)")
        return [self._mapping_view(m) for m in await self.db.barcodes_for(target_type, [target_id])]

    # ------------------------------------------------------------------
    # Scan actions
    # ------------------------------------------------------------------
    async def apply_action(
        self,
        raw: Any,
        actor: Actor,
        *,
        action: str,
        symbology: str | None = None,
        quantity: float | None = None,
        status: str | None = None,
        low_action: str | None = None,
    ) -> dict[str, Any]:
        """``use_one`` / ``restock_qty`` for parts, ``set_pantry_status`` for pantry items."""
        if action not in BARCODE_ACTIONS:
            raise PlannerError("invalid", "Unknown scan action")
        code, _ = self._normalize(raw, symbology)
        mapping, view = await self._resolve(code, actor)
        if view is None:
            raise _not_linked()
        if action == BARCODE_SET_PANTRY:
            if view["target_type"] != BARCODE_PANTRY:
                raise PlannerError("invalid", "That barcode belongs to a maintenance part")
            if status not in STOCK_STATES:
                raise PlannerError("invalid", "Pick In stock, Low or Out")
            if low_action is not None and low_action not in LOW_ACTIONS:
                raise PlannerError("invalid", "Unknown low-stock action")
            return await self._set_pantry(view, status, low_action, actor, code)
        if view["target_type"] != BARCODE_PART:
            raise PlannerError("invalid", "That barcode belongs to a pantry item")
        default = int((mapping or {}).get("default_delta_qty") or 1)
        amount = float(quantity) if quantity not in (None, "") else float(default)
        limit = MAX_DELTA if action == BARCODE_USE_ONE else MAX_RESTOCK
        if not 0 < amount <= limit:
            raise PlannerError("invalid", f"The quantity must be between 1 and {limit}")
        if action == BARCODE_USE_ONE:
            return await self._use_part(view, amount, actor, code)
        return await self._restock_part(view, amount, actor, code)

    def _fire(self, plan: Mapping[str, Any] | None, data: dict[str, Any], *text_keys: str) -> None:
        self.planner.fire(EVENT_BARCODE_ACTION, self.planner.event_payload(plan, data, *text_keys))

    async def _set_pantry(self, view: Mapping[str, Any], status: str, low_action: str | None, actor: Actor, code: str) -> dict[str, Any]:
        item = view["item"]
        previous = item["stock_state"]
        result = await self.planner.set_pantry_state(item["id"], status, low_action, actor)
        plan = await self.db.get_plan(item["plan_id"])
        fresh = await self._target(BARCODE_PANTRY, item["id"], actor) or dict(view)
        self._fire(
            plan,
            {"barcode": code, "target_type": BARCODE_PANTRY, "target_id": item["id"], "name": item["name"], "action": BARCODE_SET_PANTRY, "status": status, "user_id": actor.user_id},
            "name",
        )
        return {
            "action": BARCODE_SET_PANTRY,
            "status": status,
            "previous": previous,
            "match": {**view, **fresh},
            "created": result.get("created") or [],
            "action_needed": bool(result.get("action_needed")),
            "undo": self.planner.stash_undo("barcode_pantry", {"item_id": item["id"], "plan_id": item["plan_id"], "state": previous}, actor)
            if previous != status
            else None,
        }

    async def _log(self, part: Mapping[str, Any], kind: str, amount: float, actor: Actor, code: str) -> dict[str, Any]:
        return await self.db.insert_row(
            MEVENTS,
            {
                "plan_id": part["plan_id"],
                "object_id": part.get("object_id"),
                "kind": kind,
                "method": "scan",
                "occurred_at": m.to_iso(self.planner.now()),
                "user_id": actor.user_id,
                "user_name": actor.user_name,
                "person_entity_id": actor.person,
                "cost": round(amount * float(part["unit_cost"]), 2) if kind == EVENT_KIND_PURCHASE and part.get("unit_cost") else None,
                "data": {"part_id": part["id"], "part_name": part["part_name"], "quantity": amount, "barcode": code},
            },
        )

    async def _use_part(self, view: Mapping[str, Any], amount: float, actor: Actor, code: str) -> dict[str, Any]:
        before = await self.db.row(MPARTS, view["target_id"])
        assert before is not None
        updated = await self._maintenance.adjust_stock(before["id"], -amount, actor)
        event = await self._log(updated, EVENT_KIND_PART_USED, amount, actor, code)
        after = await self.db.row(MPARTS, before["id"]) or updated
        reorder = None
        if after.get("pending_ref") and after["pending_ref"] != before.get("pending_ref"):
            task = await self.db.get_task(after["pending_ref"])
            if task is not None:
                reorder = {"uid": task["uid"], "plan_id": task["plan_id"], "summary": task["summary"]}
        plan = await self.db.get_plan(before["plan_id"])
        self._fire(
            plan,
            {"barcode": code, "target_type": BARCODE_PART, "target_id": before["id"], "name": before["part_name"], "action": BARCODE_USE_ONE, "quantity": amount, "user_id": actor.user_id},
            "name",
        )
        undo = self.planner.stash_undo(
            "barcode_part",
            {"part_id": before["id"], "plan_id": before["plan_id"], "stock": before["current_stock"], "pending_ref": before.get("pending_ref"), "event_id": event["id"], "reopen": None},
            actor,
        )
        fresh = await self._target(BARCODE_PART, before["id"], actor)
        return {
            "action": BARCODE_USE_ONE,
            "quantity": amount,
            "previous_stock": _round(before["current_stock"]),
            "match": {**view, **(fresh or {})},
            "reorder": reorder,
            "closed": None,
            "undo": undo,
        }

    async def _restock_part(self, view: Mapping[str, Any], amount: float, actor: Actor, code: str) -> dict[str, Any]:
        before = await self.db.row(MPARTS, view["target_id"])
        assert before is not None
        closed = None
        reopen = None
        stock = float(before["current_stock"] or 0) + amount
        if before.get("pending_ref") and stock > float(before["reorder_threshold"] or 0):
            # The restock item on the shopping list is bought now: tick it off without adding the stock twice.
            pending = await self.db.get_task(before["pending_ref"])
            await self.db.update_row(MPARTS, before["id"], {"pending_ref": None})
            if pending is not None and pending["status"] != STATUS_COMPLETED:
                await self.planner.set_completed(pending["uid"], True, actor)
                closed = {"uid": pending["uid"], "plan_id": pending["plan_id"], "summary": pending["summary"]}
                reopen = pending["uid"]
        updated = await self._maintenance.adjust_stock(before["id"], amount, actor)
        event = await self._log(updated, EVENT_KIND_PURCHASE, amount, actor, code)
        plan = await self.db.get_plan(before["plan_id"])
        self._fire(
            plan,
            {"barcode": code, "target_type": BARCODE_PART, "target_id": before["id"], "name": before["part_name"], "action": BARCODE_RESTOCK, "quantity": amount, "user_id": actor.user_id},
            "name",
        )
        undo = self.planner.stash_undo(
            "barcode_part",
            {"part_id": before["id"], "plan_id": before["plan_id"], "stock": before["current_stock"], "pending_ref": before.get("pending_ref"), "event_id": event["id"], "reopen": reopen},
            actor,
        )
        fresh = await self._target(BARCODE_PART, before["id"], actor)
        return {
            "action": BARCODE_RESTOCK,
            "quantity": amount,
            "previous_stock": _round(before["current_stock"]),
            "match": {**view, **(fresh or {})},
            "reorder": None,
            "closed": closed,
            "undo": undo,
        }

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------
    async def _require_plan(self, plan_id: str | None, actor: Actor) -> None:
        plan = await self.db.get_plan(plan_id) if plan_id else None
        if not self.planner.can_access(plan, actor):
            raise PlannerError("not_found", "That item was not found (or isn't shared with you)")

    async def _undo_part(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        await self._require_plan(payload.get("plan_id"), actor)
        part = await self.db.row(MPARTS, payload["part_id"])
        if part is None:
            raise PlannerError("not_found", "That part was deleted")
        if payload.get("reopen"):
            task = await self.db.get_task(payload["reopen"])
            if task is not None and task["status"] == STATUS_COMPLETED:
                await self.planner.set_completed(task["uid"], False, actor)
        await self.db.update_row(MPARTS, part["id"], {"current_stock": payload["stock"], "pending_ref": payload.get("pending_ref")})
        if payload.get("event_id"):
            await self.db.delete_row(MEVENTS, payload["event_id"])
        self.planner.notify([part["plan_id"]])
        return {"restored": [part["id"]]}

    async def _undo_pantry(self, payload: Mapping[str, Any], actor: Actor) -> dict[str, Any]:
        await self._require_plan(payload.get("plan_id"), actor)
        if await self.db.get_pantry_item(payload["item_id"]) is None:
            raise PlannerError("not_found", "That pantry item was deleted")
        await self.planner.set_pantry_state(payload["item_id"], payload["state"], None if payload["state"] == STOCK_IN else LOW_ACTION_NONE, actor)
        return {"restored": [payload["item_id"]]}
