"""The wishlist, gift & price tracker against a real Home Assistant core."""

from __future__ import annotations

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.taskdragon import coordinator as coordinator_module
from custom_components.taskdragon.const import DOMAIN, EVENT_PRICE_ALERT

OPTIONS = {"people": [{"entity_id": "person.alex"}, {"entity_id": "person.sam"}], "rollover_hour": 0}


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


async def _wishlist(ws: WS) -> dict:
    board = await ws.ok({"type": "taskdragon/get_board"})
    return next(p for p in board["plans"] if p["archetype"] == "wishlist_hoard")


async def test_wishlist_is_seeded_without_a_todo_entity(hass: HomeAssistant, entry, clients) -> None:
    admin, _member = clients
    plan = await _wishlist(admin)
    assert plan["name"] == "Wishlist & Gifts"
    todo_plans = {state.attributes.get("plan_id") for state in hass.states.async_all("todo")}
    assert todo_plans and plan["id"] not in todo_plans, "gift lists never get a to-do entity anyone can read"
    # Reloading doesn't add a second wishlist.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    plans = [p for p in await entry.runtime_data.db.list_plans() if p["archetype"] == "wishlist_hoard"]
    assert len(plans) == 1


async def test_anti_spoiler_masking_over_websocket(hass: HomeAssistant, entry, clients) -> None:
    admin, member = clients
    plan = await _wishlist(admin)
    board = await admin.ok({"type": "taskdragon/hoard_board", "plan_id": plan["id"]})
    xmas = next(s for s in board["sections"] if s["name"] == "Christmas")
    assert {p["entity_id"] for p in board["people"]} == {"person.alex", "person.sam"}

    gift = await admin.ok({"type": "taskdragon/hoard_save_item", "item": {"plan_id": plan["id"], "title": "Telescope", "section_id": xmas["id"], "gift_for_person_id": "person.sam", "hiding_spot": "Attic"}})
    assert gift["secret"] and gift["hiding_spot"] == "Attic"
    sam_board = await member.ok({"type": "taskdragon/hoard_board", "plan_id": plan["id"]})
    assert sam_board["items"] == [] and sam_board["hidden"][0]["count"] == 1
    assert "Telescope" not in json.dumps(sam_board) and "Attic" not in json.dumps(sam_board)
    for message in (
        {"type": "taskdragon/hoard_save_item", "item": {"id": gift["id"], "title": "peek"}},
        {"type": "taskdragon/hoard_delete_item", "item_id": gift["id"]},
        {"type": "taskdragon/hoard_history", "item_id": gift["id"]},
        {"type": "taskdragon/hoard_check_now", "item_id": gift["id"]},
        {"type": "taskdragon/hoard_add_url", "item_id": gift["id"], "url": "https://shop.example/x"},
    ):
        assert await member.error(message) == "not_found"

    # Admin deletes and undoes; the WebSocket guard refuses plans the user can't open.
    deleted = await admin.ok({"type": "taskdragon/hoard_delete_item", "item_id": gift["id"]})
    await admin.ok({"type": "taskdragon/undo", "token": deleted["undo"]})
    assert (await admin.ok({"type": "taskdragon/hoard_board", "plan_id": plan["id"]}))["items"][0]["id"] == gift["id"]
    assert await member.error({"type": "taskdragon/hoard_board", "plan_id": "f" * 32}) == "not_found"
    assert await admin.error({"type": "taskdragon/hoard_add_url", "item_id": gift["id"], "url": "http://192.168.1.1/"}) == "invalid"


async def test_settings_are_admin_only(hass: HomeAssistant, entry, clients) -> None:
    admin, member = clients
    settings = await member.ok({"type": "taskdragon/hoard_settings"})
    assert settings["check_hours"] == 6 and settings["tracking"] is True
    assert await member.error({"type": "taskdragon/hoard_settings", "settings": {"tracking": False}}) == "unauthorized"
    saved = await admin.ok({"type": "taskdragon/hoard_settings", "settings": {"check_hours": 12, "protection_days": 14}})
    assert saved["check_hours"] == 12 and saved["protection_days"] == 14


async def test_scan_price_check_and_add(hass: HomeAssistant, entry, clients, monkeypatch) -> None:
    admin, member = clients
    plan = await _wishlist(admin)
    product = {
        "title": "Cordless Router",
        "brand": "Makita",
        "model": "RT0701C",
        "image": "https://img.example/router.jpg",
        "offers": [{"merchant": "Home Depot", "price": 99.0, "link": "https://www.homedepot.com/p/1", "in_stock": True}],
        "source": "UPCitemdb",
    }

    async def lookup(code):
        assert code == "0088381650380"
        return product

    monkeypatch.setattr(entry.runtime_data.hoard.products, "lookup", lookup)
    checked: list = []

    async def no_network(url_ids):
        checked.append(list(url_ids))
        return 0

    monkeypatch.setattr(entry.runtime_data.hoard, "check_urls", no_network)
    res = await member.ok({"type": "taskdragon/scan_price_check", "barcode": "088381650380", "format": "upc_a", "plan_id": plan["id"]})
    assert res["product"]["title"] == "Cordless Router" and res["best_online"]["price"] == 99.0 and res["display"] == "088381650380"
    assert any(link["name"] == "CamelCamelCamel" for link in res["search_links"])
    saved = await member.ok(
        {
            "type": "taskdragon/hoard_add_from_scan",
            "plan_id": plan["id"],
            "barcode": res["barcode"],
            "title": res["product"]["title"],
            "offers": [{"url": res["online"][0]["url"], "retailer_name": "Home Depot"}],
            "shelf_price": 89,
            "store": "Menards",
            "bought": True,
        }
    )
    assert saved["status"] == "purchased" and saved["paid_price"] == 89 and saved["protection"]["active"]
    await hass.async_block_till_done()
    assert checked, "the new store link is checked in the background"
    assert await member.error({"type": "taskdragon/hoard_add_from_scan", "plan_id": plan["id"]}) == "invalid"


async def test_price_alerts_hide_secret_gifts(hass: HomeAssistant, entry, clients, monkeypatch) -> None:
    admin, _member = clients
    plan = await _wishlist(admin)
    events = []
    hass.bus.async_listen(EVENT_PRICE_ALERT, lambda event: events.append(event.data))
    created: list[tuple[str, str]] = []
    monkeypatch.setattr(coordinator_module.persistent_notification, "async_create", lambda hass_, message, title=None, notification_id=None: created.append((title, message)))
    coordinator = entry.runtime_data
    base = {"item_id": "abc", "plan_id": plan["id"], "title": "Telescope", "price": 199.0, "retailer": "Best Buy", "url": "https://shop.example/t", "target_price": 200, "paid_price": None}
    await coordinator._price_alert({**base, "kind": "target", "secret": True})
    await coordinator._price_alert({**base, "kind": "protection", "secret": False, "paid_price": 249.0, "savings": 50.0, "days_since_purchase": 3, "days_left": 27})
    await hass.async_block_till_done()
    assert events[0]["secret"] and events[0]["title"] is None and events[0]["price"] is None
    assert events[1]["title"] == "Telescope" and events[1]["savings"] == 50.0
    assert "Telescope" not in created[0][1] and created[0][1].startswith("🐉 A secret gift")
    assert created[1][0] == "TaskDragon price protection" and "($50.00 less than you paid 3 days ago)" in created[1][1]
    assert f"hoard_plan={plan['id']}&hoard_item=abc" in created[1][1]
