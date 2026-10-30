"""Barcode mappings and scan actions on a real SQLite planner + maintenance manager."""

from __future__ import annotations

import pytest
from conftest import plan_by_name

from omniplan.barcode_manager import BarcodeManager
from omniplan.database import MEVENTS, MPARTS
from omniplan.maintenance_manager import MaintenanceManager
from omniplan.openfoodfacts import LookupFailed
from omniplan.planner import Actor, PlannerError

ALEX = Actor(user_id="u-alex", user_name="Alex", person="person.alex", is_admin=True)
SAM = Actor(user_id="u-sam", user_name="Sam", person="person.sam")
SALT_BAG = "036000291452"  # UPC-A as printed
OTHER_SALT = "4006381333931"
FILTER_BOX = "96385074"
EAN = OTHER_SALT


class FakeProducts:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, code: str):
        self.calls.append(code)
        if self.fail:
            raise LookupFailed("offline")
        if code == "0" + SALT_BAG:
            return {"name": "Morton System Saver Pellets", "brand": "Morton", "quantity": "40 lb", "categories": ["salts", "water softener salts"], "source": "openfoodfacts"}
        return None


@pytest.fixture
async def scan(planner):
    MaintenanceManager(planner.db, planner)
    products = FakeProducts()
    manager = BarcodeManager(db=planner.db, planner=planner, lookup=products)
    manager.test_products = products  # type: ignore[attr-defined]
    return manager


async def _part(planner, **fields):
    plan = await plan_by_name(planner, "Home Maintenance")
    softener = await planner.maintenance.save_object({"plan_id": plan["id"], "name": "Water Softener"}, ALEX)
    data = {"plan_id": plan["id"], "object_id": softener["id"], "part_name": "Softener salt pellets", "current_stock": 2, "reorder_threshold": 1, "restock_quantity": 2, "unit_cost": 6.5, **fields}
    return await planner.maintenance.save_part(data, ALEX), softener


async def test_unknown_barcode_suggests_product_and_lists_choices(planner, scan) -> None:
    part, softener = await _part(planner)
    result = await scan.lookup(SALT_BAG, ALEX, symbology="upc_a", context="maintenance")
    assert result["barcode"] == "0" + SALT_BAG and result["display"] == SALT_BAG
    assert result["match"] is None and result["lookup"] == "found"
    assert result["suggestion"]["title"] == "System Saver Pellets" and result["suggestion"]["brand"] == "Morton"
    options = result["options"]
    assert options["suggested_part_id"] == part["id"]
    assert [p["part_name"] for p in options["parts"]] == ["Softener salt pellets"]
    assert options["parts"][0]["object_name"] == "Water Softener" and options["objects"][0]["id"] == softener["id"]
    assert {p["name"] for p in options["pantry_plans"]} == {"Pantry & Spices"}
    # Non-retail codes are never sent anywhere; a failing lookup is reported, not raised.
    assert (await scan.lookup("FLTR-1", ALEX, symbology="code_128"))["lookup"] == "not_retail"
    scan.test_products.fail = True
    assert (await scan.lookup(OTHER_SALT, ALEX))["lookup"] == "error"
    await scan.save_settings({"openfoodfacts": False})
    assert (await scan.lookup(OTHER_SALT, ALEX))["lookup"] == "disabled"
    assert scan.test_products.calls == ["0" + SALT_BAG, OTHER_SALT]


async def test_two_brands_share_a_part_use_and_restock(planner, scan) -> None:
    part, _ = await _part(planner)
    await scan.bind(SALT_BAG, ALEX, target_type="maintenance_part", target_id=part["id"], product_name="System Saver", brand="Morton")
    bound = await scan.bind(OTHER_SALT, ALEX, target_type="maintenance_part", target_id=part["id"], symbology="ean_13")
    assert bound["match"]["part"]["part_name"] == "Softener salt pellets" and bound["replaced"] is None
    assert [b["display"] for b in await scan.list_for("maintenance_part", part["id"], ALEX)] == [SALT_BAG, OTHER_SALT]

    # Scanning the EAN-13 form of the UPC-A finds the same mapping.
    found = await scan.lookup("0" + SALT_BAG, ALEX, symbology="ean_13")
    assert found["match"]["target_id"] == part["id"] and found["match"]["part"]["current_stock"] == 2
    assert found["match"]["mapped"] and found["match"]["default_delta_qty"] == 1

    used = await scan.apply_action(SALT_BAG, ALEX, action="use_one")
    assert used["match"]["part"]["current_stock"] == 1 and used["previous_stock"] == 2
    assert used["reorder"]["summary"] == "Softener salt pellets"  # at the threshold: on the Hardware list
    hardware = await plan_by_name(planner, "Hardware")
    pending = (await planner.db.row(MPARTS, part["id"]))["pending_ref"]
    assert (await planner.db.get_task(pending))["plan_id"] == hardware["id"]
    events = await planner.db.rows(MEVENTS, {"kind": "part_used"})
    assert len(events) == 1 and events[0]["method"] == "scan" and events[0]["data"]["barcode"] == "0" + SALT_BAG

    restocked = await scan.apply_action(OTHER_SALT, ALEX, action="restock_qty", quantity=4)
    assert restocked["match"]["part"]["current_stock"] == 5
    assert restocked["closed"]["uid"] == pending
    assert (await planner.db.get_task(pending))["status"] == "completed"
    stored = await planner.db.row(MPARTS, part["id"])
    assert stored["current_stock"] == 5 and stored["pending_ref"] is None  # bought once, not added twice
    purchase = await planner.db.rows(MEVENTS, {"kind": "purchase", "method": "scan"})
    assert purchase[0]["cost"] == 26.0

    await planner.undo(restocked["undo"], ALEX)
    stored = await planner.db.row(MPARTS, part["id"])
    assert stored["current_stock"] == 1 and stored["pending_ref"] == pending
    assert (await planner.db.get_task(pending))["status"] != "completed"
    assert await planner.db.rows(MEVENTS, {"kind": "purchase", "method": "scan"}) == []

    with pytest.raises(PlannerError):
        await scan.apply_action(SALT_BAG, ALEX, action="set_pantry_status", status="in_stock")
    with pytest.raises(PlannerError):
        await scan.apply_action("12345678", ALEX, action="use_one", symbology="code_128")
    assert any(event == "omniplan_barcode_action" for event, _ in planner.test_events)


async def test_pantry_scan_marks_in_stock_with_undo_and_override(planner, scan) -> None:
    pantry = await plan_by_name(planner, "Pantry & Spices")
    salt = await planner.save_pantry_item({"plan_id": pantry["id"], "name": "Kosher salt", "low_action": "shopping"})
    await planner.set_pantry_state(salt["id"], "out", "none", ALEX)
    bound = await scan.bind(EAN, ALEX, target_type="pantry_item", target_id=salt["id"], apply_default=True)
    assert bound["applied"]["previous"] == "out" and bound["match"]["item"]["stock_state"] == "in_stock"

    await planner.undo(bound["applied"]["undo"], ALEX)
    assert (await planner.db.get_pantry_item(salt["id"]))["stock_state"] == "out"
    groceries = await plan_by_name(planner, "Groceries")
    assert not [t for t in await planner.db.list_tasks(groceries["id"]) if t["summary"].lower() == "kosher salt"]

    again = await scan.lookup(EAN, ALEX, context="pantry", apply_default=True)
    assert again["applied"]["status"] == "in_stock" and again["match"]["item"]["name"] == "Kosher salt"
    # Already in stock: scanning again changes nothing and offers no undo.
    assert (await scan.lookup(EAN, ALEX, apply_default=True))["applied"]["undo"] is None

    out = await scan.apply_action(EAN, ALEX, action="set_pantry_status", status="out", low_action="shopping")
    assert out["created"] and out["created"][0]["plan_id"] == groceries["id"]
    mapping = await planner.db.get_barcode(EAN)
    assert mapping["scan_count"] == 2


async def test_create_on_the_spot_and_mpn_fallback(planner, scan) -> None:
    created = await scan.bind(SALT_BAG, ALEX, target_type="pantry_item", create={"name": "Peanut butter"}, apply_default=True)
    assert created["created"] and created["match"]["item"]["plan_name"] == "Pantry & Spices"
    assert created["applied"]["undo"] is None  # new items start in stock

    plan = await plan_by_name(planner, "Home Maintenance")
    part = await scan.bind(FILTER_BOX, ALEX, target_type="maintenance_part", create={"name": "Furnace filter 16x25x4", "plan_id": plan["id"], "current_stock": 3, "reorder_threshold": 1})
    stored = await planner.db.row(MPARTS, part["match"]["target_id"])
    assert stored["current_stock"] == 3 and stored["mpn_gtin"] == FILTER_BOX

    # A part whose MPN/GTIN field holds the code counts as linked, even without a mapping.
    manual = await planner.maintenance.save_part({"plan_id": plan["id"], "part_name": "Humidifier pad", "mpn_gtin": "0-36000-29145-2"}, ALEX)
    await planner.db.delete_barcode("0" + SALT_BAG)
    found = await scan.lookup(SALT_BAG, ALEX)
    assert found["match"]["target_id"] == manual["id"] and not found["match"]["mapped"]

    # Relinking replaces the old target and says so.
    relinked = await scan.bind(FILTER_BOX, ALEX, target_type="maintenance_part", target_id=manual["id"], default_delta_qty=2)
    assert relinked["replaced"]["name"] == "Furnace filter 16x25x4"
    assert (await scan.apply_action(FILTER_BOX, ALEX, action="use_one"))["quantity"] == 2
    await scan.unbind(FILTER_BOX, ALEX)
    assert await planner.db.get_barcode(FILTER_BOX) is None
    with pytest.raises(PlannerError):
        await scan.unbind(FILTER_BOX, ALEX)
    with pytest.raises(PlannerError):
        await scan.bind(FILTER_BOX, ALEX, target_type="maintenance_part", create={"name": " "})
    with pytest.raises(PlannerError):
        await scan.bind(FILTER_BOX, ALEX, target_type="pantry_item", target_id="missing")


async def test_private_targets_stay_private(planner, scan) -> None:
    private = await planner.create_plan({"name": "Sam's snacks", "archetype": "pantry_spices", "is_private": True}, SAM)
    chips = await planner.save_pantry_item({"plan_id": private["id"], "name": "Chips"})
    await scan.bind(SALT_BAG, SAM, target_type="pantry_item", target_id=chips["id"])
    assert (await scan.lookup(SALT_BAG, SAM))["match"]["item"]["name"] == "Chips"

    # Alex can't see it: the code looks unknown, the pickers leave it out, nothing can be changed.
    seen = await scan.lookup(SALT_BAG, ALEX)
    assert seen["match"] is None and all(p["name"] != "Chips" for p in seen["options"]["pantry"])
    assert all(p["name"] != "Sam's snacks" for p in seen["options"]["pantry_plans"])
    with pytest.raises(PlannerError) as err:
        await scan.bind(SALT_BAG, ALEX, target_type="pantry_item", create={"name": "Salt"})
    assert err.value.code == "in_use"
    with pytest.raises(PlannerError):
        await scan.apply_action(SALT_BAG, ALEX, action="set_pantry_status", status="out")
    with pytest.raises(PlannerError):
        await scan.unbind(SALT_BAG, ALEX)
    with pytest.raises(PlannerError):
        await scan.list_for("pantry_item", chips["id"], ALEX)

    # A mapping whose target was deleted is simply relinked.
    await planner.delete_pantry_item(chips["id"])
    fresh = await scan.bind(SALT_BAG, ALEX, target_type="pantry_item", create={"name": "Salt"})
    assert fresh["match"]["item"]["name"] == "Salt"
