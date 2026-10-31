"""End-to-end tests: config flow, setup, todo entities, WebSocket API, services, listeners."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

import custom_components.taskdragon as integration
from custom_components.taskdragon.const import DOMAIN
from custom_components.taskdragon.database import TaskDragonDB
from custom_components.taskdragon.mealie_client import MealieAuthError
from custom_components.taskdragon.migration import LEGACY_PANEL_URL_PATH

OPTIONS = {
    "people": [
        {"entity_id": "person.alex", "calendar": None, "keywords": "Shift"},
        {"entity_id": "person.sam", "calendar": "calendar.sam_work", "keywords": "Shift, PRN"},
    ],
    "weather_entity": "weather.home",
    "rollover_hour": 0,
}


@pytest.fixture
async def people(hass: HomeAssistant) -> None:
    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex", "user_id": "alex-user"})
    hass.states.async_set("person.sam", "not_home", {"friendly_name": "Sam"})


@pytest.fixture
async def entry(hass: HomeAssistant, people) -> MockConfigEntry:
    config_entry = MockConfigEntry(domain=DOMAIN, title="TaskDragon", data={}, options=OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


# ----------------------------------------------------------------------
# Moving over from the integration's former name
# ----------------------------------------------------------------------
LEGACY_DATA = {"mealie_url": "http://mealie.local:9000", "mealie_api_token": "legacy-token", "mealie_verify_ssl": True}


async def test_config_flow_imports_previous_settings(hass: HomeAssistant, people) -> None:
    MockConfigEntry(domain="omniplan", title="OmniPlan", version=1, minor_version=2, data=LEGACY_DATA, options=OPTIONS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "import_legacy"
    with patch("custom_components.taskdragon.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_settings": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "TaskDragon"
    assert result["data"] == LEGACY_DATA and result["options"]["people"] == OPTIONS["people"]


async def test_declining_the_import_runs_the_normal_setup(hass: HomeAssistant, people) -> None:
    MockConfigEntry(domain="omniplan", title="OmniPlan", data={}, options=OPTIONS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_settings": False})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"


async def test_previous_database_is_copied_on_first_start(hass: HomeAssistant, people) -> None:
    os.makedirs(hass.config.path(".storage"), exist_ok=True)
    legacy = TaskDragonDB(hass.config.path(".storage", "omniplan.db"))
    await legacy.async_open()
    await legacy.create_plan({"name": "Garage sale", "archetype": "quick_list"})
    await legacy.async_close()
    config_entry = MockConfigEntry(domain=DOMAIN, title="TaskDragon", data={}, options=OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    names = [p["name"] for p in await config_entry.runtime_data.db.list_plans()]
    assert "Garage sale" in names
    # Old QR labels (/omniplan?maint_task=...) keep opening through a hidden copy of the panel.
    assert any(call.args[1:] == (LEGACY_PANEL_URL_PATH,) for call in integration._async_register_panel.await_args_list)
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


# ----------------------------------------------------------------------
# Config flow
# ----------------------------------------------------------------------
async def test_config_flow_with_shift_step(hass: HomeAssistant, people) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"household": ["person.alex", "person.sam"], "weather_entity": "weather.home"}
    )
    assert result["step_id"] == "shift"
    assert result["description_placeholders"] == {"person": "Alex"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"keywords": "Shift"})
    assert result["step_id"] == "shift"
    assert result["description_placeholders"] == {"person": "Sam"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"calendar": "calendar.sam_work", "keywords": "Shift, PRN"}
    )
    assert result["step_id"] == "mealie"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "schedule"
    with patch("custom_components.taskdragon.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rollover_hour": 3})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options["people"] == [
        {"entity_id": "person.alex", "calendar": None, "keywords": "Shift"},
        {"entity_id": "person.sam", "calendar": "calendar.sam_work", "keywords": "Shift, PRN"},
    ]
    assert options["rollover_hour"] == 3
    assert options["weather_entity"] == "weather.home"
    assert "mealie_url" not in options and "mealie_api_token" not in options
    assert result["data"] == {}


async def test_config_flow_validates_mealie_and_keeps_token_in_data(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"household": []})
    assert result["step_id"] == "mealie"
    for url in ("ftp://mealie.local", "http://user:pw@mealie.local", "http://169.254.169.254", "not a url"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mealie_url": url, "mealie_api_token": "t"})
        assert result["errors"] == {"mealie_url": "invalid_url"}, url
    with (
        patch("custom_components.taskdragon.config_flow.async_get_clientsession"),
        patch("custom_components.taskdragon.config_flow.MealieClient.async_get_self", side_effect=MealieAuthError("nope")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mealie_url": "http://mealie.local:9000", "mealie_api_token": "bad"}
        )
    assert result["errors"] == {"base": "invalid_auth"}
    # The token is never echoed back into the form.
    token_key = next(key for key in result["data_schema"].schema if str(key) == "mealie_api_token")
    assert not (token_key.description or {}).get("suggested_value")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"mealie_url": "http://mealie.local:9000", "mealie_api_token": ""}
    )
    assert result["errors"] == {"mealie_api_token": "token_required"}
    with (
        patch("custom_components.taskdragon.config_flow.async_get_clientsession"),
        patch("custom_components.taskdragon.config_flow.MealieClient.async_get_self", return_value={"groupSlug": "home"}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mealie_url": "http://mealie.local:9000/", "mealie_api_token": "s3cret"}
        )
    assert result["step_id"] == "schedule"
    with patch("custom_components.taskdragon.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {
        "mealie_url": "http://mealie.local:9000",
        "mealie_api_token": "s3cret",
        "mealie_verify_ssl": True,
    }
    assert not {"mealie_url", "mealie_api_token", "mealie_token"} & set(result["options"])


async def test_options_flow_round_trip(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"household": ["person.alex"]})
    assert result["step_id"] == "shift"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"keywords": "Work"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"leftover_days": 4})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["people"] == [{"entity_id": "person.alex", "calendar": None, "keywords": "Work"}]
    assert entry.options["leftover_days"] == 4


# ----------------------------------------------------------------------
# Setup & native todo entities
# ----------------------------------------------------------------------
async def test_setup_creates_todo_entities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    states = {state.entity_id: state for state in hass.states.async_all("todo")}
    assert {
        "todo.taskdragon_daily_chores",
        "todo.taskdragon_groceries",
        "todo.taskdragon_hardware",
        "todo.taskdragon_projects",
        "todo.taskdragon_home_maintenance",
        "todo.taskdragon_trip_packing",
        "todo.taskdragon_pantry_spices",
        "todo.taskdragon_quick_list",
    } <= set(states)
    assert states["todo.taskdragon_groceries"].attributes["archetype"] == "shopping_errands"


async def test_native_todo_services_round_trip(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        "todo", "add_item", {"item": "Garlic powder"}, target={"entity_id": "todo.taskdragon_groceries"}, blocking=True
    )
    await hass.services.async_call(
        "todo", "add_item", {"item": "Garlic powder"}, target={"entity_id": "todo.taskdragon_groceries"}, blocking=True
    )
    await hass.async_block_till_done()
    response = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": "todo.taskdragon_groceries"}, blocking=True, return_response=True
    )
    items = response["todo.taskdragon_groceries"]["items"]
    assert [item["summary"] for item in items] == ["Garlic powder"]  # deduplicated
    assert hass.states.get("todo.taskdragon_groceries").state == "1"

    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Garlic powder", "status": "completed"},
        target={"entity_id": "todo.taskdragon_groceries"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("todo.taskdragon_groceries").state == "0"

    await hass.services.async_call(
        "todo",
        "add_item",
        {"item": "Change furnace filter", "due_date": "2026-11-01", "description": "MERV 11"},
        target={"entity_id": "todo.taskdragon_home_maintenance"},
        blocking=True,
    )
    await hass.async_block_till_done()
    response = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": "todo.taskdragon_home_maintenance"}, blocking=True, return_response=True
    )
    [item] = response["todo.taskdragon_home_maintenance"]["items"]
    # Maintenance plans hold objects: the item became a one-off task of the "General" object.
    assert item["due"] == "2026-11-01" and item["summary"] == "General: Change furnace filter"
    coordinator = entry.runtime_data
    from custom_components.taskdragon.database import MTASKS

    [task] = await coordinator.db.rows(MTASKS)
    assert task["notes"] == "MERV 11" and task["category"] == "Replacement" and task["schedule_mode"] == "once"


async def test_pantry_entity_maps_stock_to_status(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        "todo", "add_item", {"item": "Taco seasoning"}, target={"entity_id": "todo.taskdragon_pantry_spices"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("todo.taskdragon_pantry_spices").state == "1"  # added as "low"
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Taco seasoning", "status": "completed"},
        target={"entity_id": "todo.taskdragon_pantry_spices"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("todo.taskdragon_pantry_spices").state == "0"


# ----------------------------------------------------------------------
# WebSocket API
# ----------------------------------------------------------------------
async def test_websocket_board_and_live_updates(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    client = await hass_ws_client(hass)
    events: list[dict] = []
    next_id = iter(range(1, 1000))

    async def call(message: dict) -> dict:
        """Send a command and return its reply, collecting subscription events on the way."""
        msg_id = next(next_id)
        await client.send_json({"id": msg_id, **message})
        while True:
            reply = await client.receive_json()
            if reply["type"] == "event":
                events.append(reply)
            elif reply["id"] == msg_id:
                return reply

    board = (await call({"type": "taskdragon/get_board"}))["result"]
    chores = next(p for p in board["plans"] if p["name"] == "Daily Chores")
    projects = next(p for p in board["plans"] if p["name"] == "Projects")
    assert {p["entity_id"] for p in board["people"]} == {"person.alex", "person.sam"}

    assert (await call({"type": "taskdragon/subscribe"}))["success"]
    frame = (await call({"type": "taskdragon/save_task", "task": {"plan_id": projects["id"], "summary": "Frame wall"}}))["result"]
    drywall = (
        await call(
            {
                "type": "taskdragon/save_task",
                "task": {"plan_id": projects["id"], "summary": "Hang drywall", "blocked_by": [frame["uid"]]},
            }
        )
    )["result"]
    assert drywall["status"] == "blocked"

    result = (await call({"type": "taskdragon/set_completed", "uid": frame["uid"], "completed": True}))["result"]
    assert result["unblocked"] == [drywall["uid"]]

    # Every change was pushed to the subscription.
    await hass.async_block_till_done()
    assert events and all(projects["id"] in e["event"]["plan_ids"] for e in events)

    dishes = (await call({"type": "taskdragon/save_task", "task": {"plan_id": chores["id"], "summary": "Dishes"}}))["result"]
    reply = await call({"type": "taskdragon/claim_task", "uid": dishes["uid"]})
    # The test user is not linked to a person entity, so claiming must explain why.
    assert reply["success"] is False and reply["error"]["code"] == "no_person"

    reply = await call({"type": "taskdragon/save_task", "task": {"plan_id": "missing", "summary": "x"}})
    assert reply["error"]["code"] == "not_found"  # same answer as for a plan you may not see

    deleted = (await call({"type": "taskdragon/delete_tasks", "uids": [dishes["uid"]]}))["result"]
    assert deleted["count"] == 1 and "snapshot" not in deleted  # undo data stays on the server
    restored = (await call({"type": "taskdragon/undo", "token": deleted["undo"]}))["result"]
    assert restored["restored"] == [dishes["uid"]]
    again = await call({"type": "taskdragon/undo", "token": deleted["undo"]})
    assert again["error"]["code"] == "undo_expired"  # single use


async def test_brain_dump_service(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    response = await hass.services.async_call(
        DOMAIN,
        "parse_brain_dump",
        {"text": "Remind me to pick up 2x4s at Menards on Saturday morning and add garlic powder to the grocery list"},
        blocking=True,
        return_response=True,
    )
    assert response["engine"] == "rules"
    assert [action["type"] for action in response["actions"]] == ["task", "shopping"]
    assert all(result["ok"] for result in response["results"])
    await hass.async_block_till_done()
    items = await hass.services.async_call(
        "todo", "get_items", {}, target={"entity_id": "todo.taskdragon_projects"}, blocking=True, return_response=True
    )
    assert [i["summary"] for i in items["todo.taskdragon_projects"]["items"]] == ["Pick up 2x4s at Menards"]


async def test_add_task_and_complete_services(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    created = await hass.services.async_call(
        DOMAIN,
        "add_task",
        {"plan": "Daily Chores", "summary": "Feed the cat", "recurrence_rule": "FREQ=DAILY", "assigned_to": ["person.alex"]},
        blocking=True,
        return_response=True,
    )
    done = await hass.services.async_call(
        DOMAIN, "complete_task", {"summary": "Feed the cat"}, blocking=True, return_response=True
    )
    assert done["uid"] == created["uid"]
    assert done["status"] == "completed" and done["next_due"] is not None


async def test_shift_balance_assigns_to_off_duty_person(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        DOMAIN, "add_task", {"plan": "Daily Chores", "summary": "Unload dishwasher", "shift_aware": True}, blocking=True
    )
    coordinator = entry.runtime_data
    today = coordinator.planner.today().isoformat()

    async def fake_events(entity_id, start, end):
        return [{"summary": "PRN shift", "start": f"{today}T07:00:00", "end": f"{today}T19:00:00"}]

    with patch.object(coordinator, "_calendar_events", side_effect=fake_events):
        result = await hass.services.async_call(DOMAIN, "run_shift_balance", {}, blocking=True, return_response=True)
    assert list(result["assignments"].values()) == ["person.alex"]  # Sam works today
    assert {p["entity_id"]: p["working"] for p in result["people"]} == {"person.alex": False, "person.sam": True}


async def test_context_snooze_wakes_when_person_arrives(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    coordinator = entry.runtime_data
    chores = await coordinator.planner.chores_plan()
    task = await coordinator.planner.save_task({"plan_id": chores["id"], "summary": "Bring in packages"})
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "taskdragon/snooze_context", "uid": task["uid"], "rule_type": "person_home", "config": {"person": "person.sam"}}
    )
    assert (await client.receive_json())["result"]["status"] == "snoozed"
    await hass.async_block_till_done()

    hass.states.async_set("person.sam", "work")
    await hass.async_block_till_done()
    assert (await coordinator.db.get_task(task["uid"]))["status"] == "snoozed"

    hass.states.async_set("person.sam", "home")
    await hass.async_block_till_done()
    assert (await coordinator.db.get_task(task["uid"]))["status"] == "needs_action"


async def test_unload_and_reload(hass: HomeAssistant, people) -> None:
    config_entry = MockConfigEntry(domain=DOMAIN, data={}, options=OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # Data survived the reload (same SQLite file): eight starter plans plus Wishlist & Gifts.
    assert len(await config_entry.runtime_data.db.list_plans()) == 9
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
