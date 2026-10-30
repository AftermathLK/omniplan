"""Barcode scanning and portable backups against a real Home Assistant core."""

from __future__ import annotations

import asyncio
import io
import json
import os
import zipfile

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.setup import async_setup_component

from custom_components.omniplan.const import DOMAIN

OPTIONS = {"people": [{"entity_id": "person.alex", "calendar": "calendar.alex_work"}], "rollover_hour": 0, "weather_entity": "weather.home"}
UPC = "036000291452"
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/0036000291452.json"


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
async def clients(hass: HomeAssistant, entry, hass_ws_client, hass_access_token, hass_read_only_access_token):
    return WS(await hass_ws_client(hass, hass_access_token)), WS(await hass_ws_client(hass, hass_read_only_access_token))


def _plan(board: dict, archetype: str) -> dict:
    return next(p for p in board["plans"] if p["archetype"] == archetype)


async def test_barcode_scanning_over_websocket(hass: HomeAssistant, entry, clients, aioclient_mock) -> None:
    admin, member = clients
    aioclient_mock.get(OFF_URL, json={"status": 1, "product": {"product_name": "Morton Salt Pellets", "brands": "Morton", "quantity": "40 lb", "categories_tags": ["en:water-softener-salts"]}})
    board = await admin.ok({"type": "omniplan/get_board"})
    pantry_plan = _plan(board, "pantry_spices")
    home = _plan(board, "home_maintenance")

    unknown = await member.ok({"type": "omniplan/barcode_lookup", "barcode": UPC, "format": "upc_a", "context": "maintenance"})
    assert unknown["match"] is None and unknown["lookup"] == "found" and unknown["suggestion"]["title"] == "Salt Pellets"
    assert len(aioclient_mock.mock_calls) == 1 and "fields=" in str(aioclient_mock.mock_calls[0][1])
    assert {p["id"] for p in unknown["options"]["maintenance_plans"]} == {home["id"]}

    obj = await admin.ok({"type": "omniplan/maint_save_object", "object": {"plan_id": home["id"], "name": "Water Softener"}})
    bound = await member.ok(
        {"type": "omniplan/barcode_bind", "barcode": UPC, "format": "upc_a", "target_type": "maintenance_part", "create": {"name": "Softener salt", "object_id": obj["id"], "current_stock": 3, "reorder_threshold": 1}}
    )
    part_id = bound["match"]["target_id"]
    assert bound["created"] and bound["match"]["part"]["object_name"] == "Water Softener"
    used = await member.ok({"type": "omniplan/barcode_apply_action", "barcode": "0" + UPC, "action": "use_one"})
    assert used["match"]["part"]["current_stock"] == 2 and used["undo"]
    restocked = await admin.ok({"type": "omniplan/barcode_apply_action", "barcode": UPC, "action": "restock_qty", "quantity": 4})
    assert restocked["match"]["part"]["current_stock"] == 6
    listed = await admin.ok({"type": "omniplan/barcode_list", "target_type": "maintenance_part", "target_id": part_id})
    assert [b["display"] for b in listed["barcodes"]] == [UPC]

    salt = await admin.ok({"type": "omniplan/save_pantry_item", "item": {"plan_id": pantry_plan["id"], "name": "Kosher salt"}})
    await admin.ok({"type": "omniplan/trigger_pantry_low", "item_id": salt["id"], "state": "out", "action": "none"})
    await admin.ok({"type": "omniplan/barcode_bind", "barcode": "4006381333931", "target_type": "pantry_item", "target_id": salt["id"]})
    scanned = await member.ok({"type": "omniplan/barcode_lookup", "barcode": "4006381333931", "context": "pantry", "apply_default": True})
    assert scanned["applied"]["previous"] == "out" and scanned["match"]["item"]["stock_state"] == "in_stock"
    undone = await member.call({"type": "omniplan/undo", "token": scanned["applied"]["undo"]})
    assert undone["success"]

    # Validation, unknown targets and the admin-only setting.
    for message in (
        {"type": "omniplan/barcode_lookup", "barcode": ""},
        {"type": "omniplan/barcode_bind", "barcode": UPC, "target_type": "pantry_item", "target_id": "x", "create": {"name": "y"}},
        {"type": "omniplan/barcode_apply_action", "barcode": UPC, "action": "explode"},
    ):
        assert not (await admin.call(message))["success"]
    missing = await admin.call({"type": "omniplan/barcode_bind", "barcode": "96385074", "target_type": "pantry_item", "target_id": "no-such-item"})
    assert missing["error"]["code"] == "not_found"
    denied = await member.call({"type": "omniplan/barcode_settings", "settings": {"openfoodfacts": False}})
    assert denied["error"]["code"] == "unauthorized"
    assert (await admin.ok({"type": "omniplan/barcode_settings", "settings": {"openfoodfacts": False}})) == {"openfoodfacts": False}
    off = await member.ok({"type": "omniplan/barcode_lookup", "barcode": "96385074"})
    assert off["lookup"] == "disabled" and len(aioclient_mock.mock_calls) == 1


async def test_portable_backup_download_restore_and_admin_only(hass: HomeAssistant, entry, clients, hass_client, hass_client_no_auth, hass_read_only_access_token) -> None:
    admin, member = clients
    coordinator = entry.runtime_data
    board = await admin.ok({"type": "omniplan/get_board"})
    quick = _plan(board, "quick_list")
    keep = await admin.ok({"type": "omniplan/save_task", "task": {"plan_id": quick["id"], "summary": "Keep this one"}})

    snap = await admin.ok({"type": "omniplan/backup_create"})
    folder = hass.config.path("backups", "omniplan")
    assert os.path.isfile(os.path.join(folder, snap["name"]))
    listing = await admin.ok({"type": "omniplan/backup_list"})
    assert listing["snapshots"][0]["name"] == snap["name"] and listing["settings"]["retention"] == 14
    assert listing["directory"] == "backups/omniplan"
    for message in ({"type": "omniplan/backup_list"}, {"type": "omniplan/backup_create"}, {"type": "omniplan/backup_restore_snapshot", "name": snap["name"], "mode": "merge"}):
        assert (await member.call(message))["error"]["code"] == "unauthorized"

    client = await hass_client()
    download = await client.get("/api/omniplan/backup/download")
    assert download.status == 200 and download.headers["Content-Type"] == "application/zip"
    assert download.headers["Content-Disposition"].startswith('attachment; filename="omniplan_portable_backup_')
    body = await download.read()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        manifest = json.loads(archive.read("manifest_bridge.json"))
        assert {"database_dump.json", "omniplan.db"} <= set(archive.namelist())
    assert manifest["source_platform"] == "home_assistant_custom_component"
    assert any(b["ha_entity_id"] == "weather.home" for b in manifest["hardware_bindings_bridge"])
    alex = next(u for u in manifest["users_bridge_map"] if u["person_entity_id"] == "person.alex")
    assert alex["role"] == "admin" and alex["work_calendar_ref"] == "calendar.alex_work"
    for _ in range(50):  # the server deletes its temporary file right after the last byte
        if not [n for n in os.listdir(folder) if n.startswith(".download")]:
            break
        await asyncio.sleep(0.05)
    assert [n for n in os.listdir(folder) if n.startswith(".download")] == []

    member_http = await hass_client(hass_read_only_access_token)
    assert (await member_http.get("/api/omniplan/backup/download")).status == 403
    assert (await (await hass_client_no_auth()).get("/api/omniplan/backup/download")).status == 401
    assert (await client.get("/api/omniplan/backup/download?snapshot=../omniplan.db")).status == 400  # Home Assistant's own filter
    assert (await client.get("/api/omniplan/backup/download?snapshot=secrets.yaml")).status == 404
    assert await async_setup_component(hass, "auth", {})
    signed = await admin.ok({"type": "auth/sign_path", "path": f"/api/omniplan/backup/download?snapshot={snap['name']}"})
    saved = await (await hass_client_no_auth()).get(signed["path"])
    assert saved.status == 200 and len(await saved.read()) == snap["size"]

    # Change things, then restore the downloaded file over HTTP.
    later = await admin.ok({"type": "omniplan/save_task", "task": {"plan_id": quick["id"], "summary": "Added after the backup"}})
    await admin.ok({"type": "omniplan/delete_tasks", "uids": [keep["uid"]]})
    extra_plan = await admin.ok({"type": "omniplan/save_plan", "plan": {"name": "Garage Sale", "archetype": "quick_list"}})
    await hass.async_block_till_done()
    garage_entity = next(s.entity_id for s in hass.states.async_all("todo") if s.attributes.get("friendly_name", "").endswith("Garage Sale"))

    def form(data: bytes, mode: str, name: str = "backup.zip") -> aiohttp.FormData:
        payload = aiohttp.FormData()
        payload.add_field("mode", mode)
        payload.add_field("file", data, filename=name, content_type="application/zip")
        return payload

    merged = await client.post("/api/omniplan/backup/restore", data=form(body, "merge"))
    assert merged.status == 200, await merged.text()
    report = await merged.json()
    assert report["mode"] == "merge" and report["safety_snapshot"].endswith("_pre-restore.zip")
    assert await coordinator.db.get_task(keep["uid"]) is not None and await coordinator.db.get_task(later["uid"]) is not None

    replaced = await client.post("/api/omniplan/backup/restore", data=form(body, "replace"))
    assert replaced.status == 200
    assert await coordinator.db.get_task(later["uid"]) is None and await coordinator.db.get_plan(extra_plan["id"]) is None
    await hass.async_block_till_done()
    assert hass.states.get(garage_entity) is None  # its to-do entity went with it

    assert (await client.post("/api/omniplan/backup/restore", data=form(b"PK\x03\x04garbage", "merge"))).status == 400
    assert (await client.post("/api/omniplan/backup/restore", data=form(body, "nuke"))).status == 400
    assert (await member_http.post("/api/omniplan/backup/restore", data=form(body, "replace"))).status == 403
    await asyncio.sleep(0.1)
    assert [n for n in os.listdir(folder) if n.startswith(".upload")] == []

    # The snapshot restore command and the schedule settings.
    restored = await admin.ok({"type": "omniplan/backup_restore_snapshot", "name": snap["name"], "mode": "merge"})
    assert restored["source"] == snap["name"]
    settings = await admin.ok({"type": "omniplan/backup_settings", "settings": {"retention": 3, "time": "02:30"}})
    assert settings == {"enabled": True, "retention": 3, "time": "02:30"}
    assert len((await admin.ok({"type": "omniplan/backup_list"}))["snapshots"]) == 3
    bad = await admin.call({"type": "omniplan/backup_settings", "settings": {"time": "9am"}})
    assert bad["error"]["code"] == "backup_error"


async def test_create_backup_service_is_admin_only(hass: HomeAssistant, entry, hass_admin_user, hass_read_only_user) -> None:
    result = await hass.services.async_call(DOMAIN, "create_backup", {}, blocking=True, return_response=True, context=Context(user_id=hass_admin_user.id))
    assert result["name"].startswith("omniplan_portable_backup_") and result["rows"] > 0
    automation = await hass.services.async_call(DOMAIN, "create_backup", {}, blocking=True, return_response=True)
    assert automation["name"] != result["name"]
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "create_backup", {}, blocking=True, return_response=True, context=Context(user_id=hass_read_only_user.id))
