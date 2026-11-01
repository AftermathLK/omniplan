"""1.4.1 security audit fixes against a real Home Assistant core."""

from __future__ import annotations

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.taskdragon.const import DOMAIN
from custom_components.taskdragon.price_tracker import Page

OPTIONS = {"people": [{"entity_id": "person.alex", "calendar": "calendar.alex_work"}, {"entity_id": "person.sam"}], "rollover_hour": 0}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%audit\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


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

    async def error(self, message: dict) -> str:
        reply = await self.call(message)
        assert not reply["success"], reply
        return reply["error"]["code"]


@pytest.fixture
async def entry(hass: HomeAssistant, hass_admin_user, hass_read_only_user) -> MockConfigEntry:
    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex", "user_id": hass_admin_user.id})
    hass.states.async_set("person.sam", "home", {"friendly_name": "Sam", "user_id": hass_read_only_user.id})
    config_entry = MockConfigEntry(domain=DOMAIN, title="TaskDragon", version=1, minor_version=2, data={}, options=OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def clients(hass: HomeAssistant, entry, hass_ws_client, hass_access_token, hass_read_only_access_token):
    return WS(await hass_ws_client(hass, hass_access_token)), WS(await hass_ws_client(hass, hass_read_only_access_token))


async def test_upload_fields_are_capped_and_events_checked(hass: HomeAssistant, entry, clients, hass_client) -> None:
    admin, _member = clients
    board = await admin.ok({"type": "taskdragon/get_board"})
    plan_id = next(p["id"] for p in board["plans"] if p["archetype"] == "home_maintenance")
    heater = await admin.ok({"type": "taskdragon/maint_save_object", "object": {"plan_id": plan_id, "name": "Water Heater"}})
    car = await admin.ok({"type": "taskdragon/maint_save_object", "object": {"plan_id": plan_id, "name": "Car"}})
    task = await admin.ok({"type": "taskdragon/maint_save_task", "task": {"object_id": car["id"], "name": "Oil", "interval_value": 1, "interval_unit": "years"}})
    done = await admin.ok({"type": "taskdragon/maint_complete", "task_id": task["id"]})
    client = await hass_client()

    def form(**fields):
        data = aiohttp.FormData()
        for key, value in fields.items():
            data.add_field(key, value)
        data.add_field("file", PDF, filename="r.pdf", content_type="application/pdf")
        return data

    huge = await client.post("/api/taskdragon/documents", data=form(object_id=heater["id"], title="x" * 200_000))
    assert huge.status == 413
    wrong = await client.post("/api/taskdragon/documents", data=form(object_id=heater["id"], event_id=done["event"]["id"]))
    assert wrong.status == 400
    right = await client.post("/api/taskdragon/documents", data=form(object_id=car["id"], event_id=done["event"]["id"]))
    assert right.status == 200


async def test_admin_only_service_and_import_filter(hass: HomeAssistant, entry, clients, hass_read_only_user) -> None:
    admin, member = clients
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "run_shift_balance", {}, blocking=True, return_response=True, context=Context(user_id=hass_read_only_user.id))
    result = await hass.services.async_call(DOMAIN, "run_shift_balance", {}, blocking=True, return_response=True)
    assert isinstance(result, dict)
    assert (await member.ok({"type": "taskdragon/mealie_import_settings"}))["seasonings"]
    assert await member.error({"type": "taskdragon/mealie_import_settings", "settings": {"skip_seasonings": False}}) == "unauthorized"
    assert (await admin.ok({"type": "taskdragon/mealie_import_settings", "settings": {"skip_seasonings": False}}))["skip_seasonings"] is False
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "parse_brain_dump", {"text": "buy milk", "agent_id": "conversation.some_cloud_agent", "dry_run": True}, blocking=True, return_response=True, context=Context(user_id=hass_read_only_user.id)
        )


async def test_people_calendars_only_for_admins(hass: HomeAssistant, entry, clients) -> None:
    admin, member = clients
    admin_people = {p["entity_id"]: p for p in (await admin.ok({"type": "taskdragon/get_board"}))["people"]}
    member_people = {p["entity_id"]: p for p in (await member.ok({"type": "taskdragon/get_board"}))["people"]}
    assert admin_people["person.alex"]["calendar"] == "calendar.alex_work"
    assert "calendar" not in member_people["person.alex"] and member_people["person.alex"]["user_id"], "user ids stay: lists are shared by user"


async def test_wishlist_photos_come_through_home_assistant(hass: HomeAssistant, entry, clients, hass_client, hass_client_no_auth, hass_read_only_access_token, monkeypatch) -> None:
    admin, member = clients
    board = await admin.ok({"type": "taskdragon/get_board"})
    plan = next(p for p in board["plans"] if p["archetype"] == "wishlist_hoard")
    hoard_board = await admin.ok({"type": "taskdragon/hoard_board", "plan_id": plan["id"]})
    xmas = next(s for s in hoard_board["sections"] if s["name"] == "Christmas")
    gift = await admin.ok({"type": "taskdragon/hoard_save_item", "item": {"plan_id": plan["id"], "title": "Bike", "section_id": xmas["id"], "gift_for_person_id": "person.sam", "image_url": "https://img.example/bike.png"}})
    fetched: list[str] = []

    async def fake_get(url, *, headers, max_bytes, truncate=False):
        fetched.append(url)
        return Page(url, 200, PNG, None, "image/png")

    monkeypatch.setattr(entry.runtime_data.hoard.fetcher, "get", fake_get)
    client = await hass_client()
    response = await client.get(gift["image_path"])
    assert response.status == 200 and response.headers["Content-Type"] == "image/png" and await response.read() == PNG
    assert response.headers["X-Content-Type-Options"] == "nosniff" and fetched == ["https://img.example/bike.png"]
    assert (await (await hass_client_no_auth()).get(gift["image_path"])).status == 401
    # Sam's own token: the photo of a gift for Sam doesn't exist as far as Sam's requests go.
    sam = await hass_client(hass_read_only_access_token)
    assert (await sam.get(gift["image_path"])).status == 404
    assert (await member.ok({"type": "taskdragon/hoard_board", "plan_id": plan["id"]}))["items"] == []


async def test_restored_device_actions_wait_for_an_administrator(hass: HomeAssistant, entry, clients) -> None:
    admin, _member = clients
    pressed: list[dict] = []
    hass.services.async_register("button", "press", lambda call: pressed.append(dict(call.data)))
    board = await admin.ok({"type": "taskdragon/get_board"})
    plan_id = next(p["id"] for p in board["plans"] if p["archetype"] == "home_maintenance")
    fridge = await admin.ok({"type": "taskdragon/maint_save_object", "object": {"plan_id": plan_id, "name": "Fridge"}})
    action = {"service": "button.press", "entity_id": "button.fridge_filter_reset"}
    task = await admin.ok({"type": "taskdragon/maint_save_task", "task": {"object_id": fridge["id"], "name": "Filter", "interval_value": 6, "interval_unit": "months", "on_complete_action": action}})
    await admin.ok({"type": "taskdragon/maint_complete", "task_id": task["id"]})
    await hass.async_block_till_done()
    assert len(pressed) == 1
    snapshot = await admin.ok({"type": "taskdragon/backup_create"})
    report = await admin.ok({"type": "taskdragon/backup_restore_snapshot", "name": snapshot["name"], "mode": "replace"})
    assert any("paused until an administrator" in w for w in report["warnings"])
    maint = await admin.ok({"type": "taskdragon/maint_board", "plan_id": plan_id})
    restored = next(t for t in maint["tasks"] if t["id"] == task["id"])
    assert restored["action_paused"] and "configured_by" not in restored["on_complete_action"]
    await admin.ok({"type": "taskdragon/maint_complete", "task_id": task["id"]})
    await hass.async_block_till_done()
    assert len(pressed) == 1, "a restored action doesn't run on its own"
    await admin.ok({"type": "taskdragon/maint_save_task", "task": {"id": task["id"], "on_complete_action": action}})
    await admin.ok({"type": "taskdragon/maint_complete", "task_id": task["id"]})
    await hass.async_block_till_done()
    assert len(pressed) == 2
