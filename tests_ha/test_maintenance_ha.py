"""Maintenance subsystem against a real Home Assistant core."""

from __future__ import annotations

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.omniplan.const import DOMAIN

OPTIONS = {"people": [{"entity_id": "person.alex"}], "rollover_hour": 0}
PDF_BYTES = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


class WS:
    def __init__(self, client) -> None:
        self.client = client
        self._ids = iter(range(1, 100000))

    async def call(self, message: dict) -> dict:
        msg_id = next(self._ids)
        await self.client.send_json({"id": msg_id, **message})
        while True:
            reply = await self.client.receive_json()
            if reply.get("id") == msg_id and reply.get("type") != "event":
                return reply

    async def ok(self, message: dict):
        reply = await self.call(message)
        assert reply["success"], reply
        return reply["result"]


@pytest.fixture
async def entry(hass: HomeAssistant, hass_admin_user) -> MockConfigEntry:
    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex", "user_id": hass_admin_user.id})
    config_entry = MockConfigEntry(domain=DOMAIN, title="OmniPlan", version=1, minor_version=2, data={}, options=OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def admin(hass: HomeAssistant, entry, hass_ws_client) -> WS:
    return WS(await hass_ws_client(hass))


async def _plan_id(ws: WS) -> str:
    board = await ws.ok({"type": "omniplan/get_board"})
    return next(p["id"] for p in board["plans"] if p["archetype"] == "home_maintenance")


async def test_objects_tasks_complete_undo_and_board(hass: HomeAssistant, entry, admin: WS) -> None:
    plan_id = await _plan_id(admin)
    furnace = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Furnace", "manufacturer_model": "Carrier 59SC5"}})
    part = await admin.ok({"type": "omniplan/maint_save_part", "part": {"plan_id": plan_id, "object_id": furnace["id"], "part_name": "16x25x4 Air Filter", "current_stock": 3, "unit_cost": 12}})
    task = await admin.ok(
        {
            "type": "omniplan/maint_save_task",
            "task": {"object_id": furnace["id"], "name": "Replace Filter", "category": "Replacement", "interval_value": 6, "interval_unit": "months", "parts": [{"part_id": part["id"], "quantity": 1}]},
        }
    )
    board = await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id})
    [view] = board["tasks"]
    assert view["code"] == "#1.1" and view["status"] == "ok" and view["interval_label"] == "6 Months"
    assert board["summary"]["objects"] == 1 and board["people"][0]["entity_id"] == "person.alex"

    done = await admin.ok({"type": "omniplan/maint_complete", "task_id": task["id"], "cost": 5, "duration_minutes": 12, "feedback": "just_right"})
    assert done["event"]["code"] == "#1.1-1" and done["event"]["person_entity_id"] == "person.alex" and done["event"]["total_cost"] == 17
    assert "undo" in done and "task" not in done
    await admin.ok({"type": "omniplan/undo", "token": done["undo"]})
    assert await admin.ok({"type": "omniplan/maint_history", "task_id": task["id"]}) == []
    assert (await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id}))["parts"][0]["current_stock"] == 3

    skipped = await admin.ok({"type": "omniplan/maint_skip", "task_id": task["id"]})
    assert skipped["kind"] == "skipped"
    deleted = await admin.ok({"type": "omniplan/maint_delete_object", "object_id": furnace["id"]})
    assert (await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id}))["objects"] == []
    await admin.ok({"type": "omniplan/undo", "token": deleted["undo"]})
    assert len((await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id}))["tasks"]) == 1

    main = await admin.ok({"type": "omniplan/get_board"})
    assert main["maintenance"][plan_id]["tasks"] == 1


async def test_permissions(hass: HomeAssistant, entry, admin: WS, hass_ws_client, hass_read_only_access_token) -> None:
    member = WS(await hass_ws_client(hass, hass_read_only_access_token))
    plan_id = await _plan_id(admin)
    obj = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Dryer"}})
    action = {"service": "button.press", "entity_id": "button.dryer_filter_reset"}
    reply = await member.call({"type": "omniplan/maint_save_task", "task": {"object_id": obj["id"], "name": "Clean Vent", "interval_value": 1, "interval_unit": "years", "on_complete_action": action}})
    assert reply["error"]["code"] == "unauthorized"
    reply = await member.call({"type": "omniplan/maint_save_settings", "plan_id": plan_id, "settings": {"notify_service": "notify.everyone"}})
    assert reply["error"]["code"] == "unauthorized"

    private = await member.ok({"type": "omniplan/save_plan", "plan": {"name": "My garage", "archetype": "home_maintenance", "is_private": True}})
    mower = await member.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": private["id"], "name": "Mower"}})
    for message in (
        {"type": "omniplan/maint_board", "plan_id": private["id"]},
        {"type": "omniplan/maint_save_object", "object": {"id": mower["id"], "name": "Mine"}},
        {"type": "omniplan/maint_history", "object_id": mower["id"]},
        {"type": "omniplan/maint_qr", "object_id": mower["id"]},
    ):
        assert (await admin.call(message))["error"]["code"] == "not_found", message


async def test_triggers_notify_with_zone_gating(hass: HomeAssistant, entry, admin: WS) -> None:
    calls = async_mock_service(hass, "notify", "phone")
    plan_id = await _plan_id(admin)
    await admin.ok({"type": "omniplan/maint_save_settings", "plan_id": plan_id, "settings": {"notify_service": "notify.phone"}})
    hass.states.async_set("sensor.water_total", "5000", {"unit_of_measurement": "gal"})
    hass.states.async_set("zone.cabin", "0", {"persons": [], "friendly_name": "Cabin"})
    home = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Water Filter"}})
    cabin = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Cabin Pump", "zone_entity_id": "zone.cabin"}})
    trigger = {"type": "counter", "entity_id": "sensor.water_total", "delta": 1000}
    for obj in (home, cabin):
        await admin.ok({"type": "omniplan/maint_save_task", "task": {"object_id": obj["id"], "name": "Replace Cartridge", "schedule_mode": "none", "triggers": [trigger]}})
    await hass.async_block_till_done()

    hass.states.async_set("sensor.water_total", "6100", {"unit_of_measurement": "gal"})
    await hass.async_block_till_done()
    await entry.runtime_data.maintenance.tick()
    await hass.async_block_till_done()
    assert len(calls) == 1 and calls[0].data["message"].startswith("Water Filter: Replace Cartridge was triggered")
    assert calls[0].data["data"]["url"].startswith("/omniplan?maint_task=")

    # Someone arrives at the cabin: its held notification goes out.
    hass.states.async_set("zone.cabin", "1", {"persons": ["person.alex"], "friendly_name": "Cabin"})
    await hass.async_block_till_done()
    assert len(calls) == 2 and calls[1].data["message"].startswith("Cabin Pump:")
    board = await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id})
    assert {t["status"] for t in board["tasks"]} == {"triggered"} and board["summary"]["triggered"] == 2


async def test_nfc_scan_and_device_reset_action(hass: HomeAssistant, entry, admin: WS, hass_admin_user) -> None:
    presses = async_mock_service(hass, "button", "press")
    plan_id = await _plan_id(admin)
    obj = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Fridge"}})
    task = await admin.ok(
        {
            "type": "omniplan/maint_save_task",
            "task": {
                "object_id": obj["id"],
                "name": "Replace Water Filter",
                "interval_value": 6,
                "interval_unit": "months",
                "nfc_tag_id": "04:A2:19",
                "require_nfc": True,
                "on_complete_action": {"service": "button.press", "entity_id": "button.fridge_filter_reset"},
            },
        }
    )
    reply = await admin.call({"type": "omniplan/maint_complete", "task_id": task["id"]})
    assert reply["error"]["code"] == "nfc_required"
    hass.bus.async_fire("tag_scanned", {"tag_id": "04:A2:19", "device_id": "phone"}, context=Context(user_id=hass_admin_user.id))
    await hass.async_block_till_done()
    [event] = await admin.ok({"type": "omniplan/maint_history", "task_id": task["id"]})
    assert event["method"] == "nfc" and event["code"] == "#1.1-1" and event["person_entity_id"] == "person.alex"
    assert len(presses) == 1 and presses[0].data["entity_id"] == ["button.fridge_filter_reset"]
    assert presses[0].context.user_id == hass_admin_user.id

    # Binding mode: the next scanned tag is attached to the task.
    other = await admin.ok({"type": "omniplan/maint_save_task", "task": {"object_id": obj["id"], "name": "Clean Coils", "interval_value": 1, "interval_unit": "years"}})
    await admin.ok({"type": "omniplan/maint_nfc_listen", "task_id": other["id"]})
    hass.bus.async_fire("tag_scanned", {"tag_id": "NEW-TAG"})
    await hass.async_block_till_done()
    board = await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id})
    assert next(t for t in board["tasks"] if t["id"] == other["id"])["nfc_tag_id"] == "NEW-TAG"


async def test_documents_and_service_record_over_http(hass: HomeAssistant, entry, admin: WS, hass_client, hass_client_no_auth) -> None:
    plan_id = await _plan_id(admin)
    obj = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": plan_id, "name": "Water Heater"}})
    client = await hass_client()

    async def upload(data: bytes, filename: str):
        form = aiohttp.FormData()
        form.add_field("object_id", obj["id"])
        form.add_field("category", "manual")
        form.add_field("file", data, filename=filename, content_type="text/html")
        return await client.post("/api/omniplan/documents", data=form)

    response = await upload(PDF_BYTES, "Owner's manual <script>.pdf")
    assert response.status == 200
    doc = await response.json()
    assert doc["content_type"] == "application/pdf" and doc["filename"] == "Owner_s manual _script.pdf"
    again = await (await upload(PDF_BYTES, "copy.pdf")).json()  # deduplicated on disk
    runtime = entry.runtime_data.maintenance
    import os

    assert len([n for n in os.listdir(runtime.docs_dir) if len(n) == 64]) == 1 and again["id"] != doc["id"]
    rejected = await upload(b"<html><script>alert(1)</script></html>", "evil.html")
    assert rejected.status == 415
    rejected = await upload(b"<svg onload=alert(1)>", "logo.txt")
    assert rejected.status == 415

    download = await client.get(f"/api/omniplan/documents/{doc['id']}")
    assert download.status == 200 and await download.read() == PDF_BYTES
    assert download.headers["X-Content-Type-Options"] == "nosniff" and "sandbox" in download.headers["Content-Security-Policy"]
    assert (await (await hass_client_no_auth()).get(f"/api/omniplan/documents/{doc['id']}")).status == 401
    assert await async_setup_component(hass, "auth", {})  # always loaded in a real install
    signed = await admin.ok({"type": "auth/sign_path", "path": f"/api/omniplan/documents/{doc['id']}"})
    assert (await (await hass_client_no_auth()).get(signed["path"])).status == 200

    task = await admin.ok({"type": "omniplan/maint_save_task", "task": {"object_id": obj["id"], "name": "Flush Tank", "interval_value": 1, "interval_unit": "years", "manual_doc_id": doc["id"], "manual_page": 12}})
    await admin.ok({"type": "omniplan/maint_complete", "task_id": task["id"], "cost": 20, "notes": "Drained 5 gal of sediment"})
    record = await client.get(f"/api/omniplan/service_record/{obj['id']}?from=2000-01-01")
    body = await record.read()
    assert record.status == 200 and record.headers["Content-Type"] == "application/pdf" and body.startswith(b"%PDF-1.4")
    assert b"Flush Tank #1.1-1" in body and b"1 entries" in body

    link = await admin.ok({"type": "omniplan/maint_add_link", "object_id": obj["id"], "url": "https://example.com/warranty", "category": "warranty"})
    assert link["kind"] == "link"
    assert (await admin.call({"type": "omniplan/maint_add_link", "object_id": obj["id"], "url": "javascript:alert(1)"}))["success"] is False
    removed = await admin.ok({"type": "omniplan/maint_delete_document", "document_id": doc["id"]})
    assert (await client.get(f"/api/omniplan/documents/{doc['id']}")).status == 404
    await admin.ok({"type": "omniplan/undo", "token": removed["undo"]})
    assert (await client.get(f"/api/omniplan/documents/{doc['id']}")).status == 200


async def test_softener_salt_qr_todo_services_and_fleet(hass: HomeAssistant, entry, admin: WS) -> None:
    plan_id = await _plan_id(admin)
    hass.states.async_set("sensor.droplet_water", "1000", {"unit_of_measurement": "gal", "state_class": "total_increasing"})
    softener = await admin.ok({"type": "omniplan/maint_apply_template", "plan_id": plan_id, "template_id": "water_softener"})
    await admin.ok(
        {
            "type": "omniplan/maint_save_object",
            "object": {"id": softener["id"], "config": {"volume_entity": "sensor.droplet_water", "scale": "levels", "max_level": 8, "warning_level": 2}},
        }
    )
    await hass.async_block_till_done()
    filled = await admin.ok({"type": "omniplan/maint_salt_log", "object_id": softener["id"], "kind": "fill"})
    assert filled["event"]["code"] == "#1.1-1"
    hass.states.async_set("sensor.droplet_water", "5000", {"unit_of_measurement": "gal"})
    await hass.async_block_till_done()
    await admin.ok({"type": "omniplan/maint_salt_log", "object_id": softener["id"], "kind": "level", "level": 6})
    board = await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id})
    salt = next(o for o in board["objects"] if o["id"] == softener["id"])["salt"]
    assert salt["known"] and salt["level"] == pytest.approx(6) and salt["segments"] == 1 and salt["since_refill"] == pytest.approx(4000)
    assert salt["per_unit"] == pytest.approx(2000, rel=0.3)

    refill = next(t for t in board["tasks"] if t["name"] == "Refill Water Softener Salt")
    qr = await admin.ok({"type": "omniplan/maint_qr", "task_id": refill["id"], "action": "complete", "origin": "https://ha.example.com"})
    assert qr["url"].endswith(f"/omniplan?maint_task={refill['id']}&do=complete") and len(qr["rows"]) == len(qr["rows"][0])

    # Native todo: checking off a due item completes the maintenance task.
    check = next(t for t in board["tasks"] if t["name"] == "Check for Salt Bridges")
    await admin.ok({"type": "omniplan/maint_save_task", "task": {"id": check["id"], "next_due": "2020-01-01"}})
    await hass.async_block_till_done()
    entity = "todo.omniplan_home_maintenance"
    items = (await hass.services.async_call("todo", "get_items", {}, target={"entity_id": entity}, blocking=True, return_response=True))[entity]["items"]
    item = next(i for i in items if i["summary"] == "Water Softener: Check for Salt Bridges")
    assert item["status"] == "needs_action"
    await hass.services.async_call("todo", "update_item", {"item": item["uid"], "status": "completed"}, target={"entity_id": entity}, blocking=True)
    [event] = await admin.ok({"type": "omniplan/maint_history", "task_id": check["id"]})
    assert event["method"] == "todo"

    # Services.
    result = await hass.services.async_call(DOMAIN, "complete_maintenance", {"task": "Water Softener / Clean Brine Tank", "cost": 3}, blocking=True, return_response=True)
    assert result["event"] == "#1.3-1"
    summary = await hass.services.async_call(DOMAIN, "log_salt_level", {"object": "Water Softener", "kind": "level", "level": 5}, blocking=True, return_response=True)
    assert summary["level"] == pytest.approx(5)

    # One-click battery fleet.
    hass.states.async_set("sensor.lock_battery", "64", {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Front Door Lock Battery", "battery_type": "AA"})
    hass.states.async_set("sensor.outdoor_temperature", "12", {"device_class": "temperature"})
    fleet = await admin.ok({"type": "omniplan/maint_battery_fleet", "plan_id": plan_id})
    assert fleet["created"] == 1
    board = await admin.ok({"type": "omniplan/maint_board", "plan_id": plan_id})
    battery = next(t for t in board["tasks"] if t["name"] == "Replace Battery — Front Door Lock")
    assert battery["triggers"][0]["entity_id"] == "sensor.lock_battery" and battery["status"] == "ok"
    assert any(p["part_name"] == "AA Battery" for p in board["parts"])
