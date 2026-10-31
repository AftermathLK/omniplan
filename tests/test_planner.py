"""Planner behaviours: shopping dedupe, pantry actions, snoozing, packing, BOM, undo."""

from __future__ import annotations

import pytest
from conftest import plan_by_name

from taskdragon.planner import Actor, PlannerError

ALEX = Actor(user_id="u1", user_name="Alex", person="person.alex")


async def test_seeded_plans(planner) -> None:
    plans = await planner.db.list_plans()
    assert [p["archetype"] for p in plans] == [
        "quick_list",
        "daily_chores",
        "shopping_errands",
        "shopping_errands",
        "projects_diy",
        "home_maintenance",
        "packing_trips",
        "pantry_spices",
    ]
    assert await planner.seed_defaults() == []  # idempotent
    quick = plans[0]
    assert quick["name"] == "Quick List" and quick["settings"]["role"] == "quick" and not quick["is_private"]
    sections = await planner.db.list_sections([quick["id"]])
    assert [s["name"] for s in sections] == ["Calls to Make", "House Stuff", "Weekend", "Quick Ideas"]


async def test_shopping_merges_quantities_and_learns_categories(planner) -> None:
    groceries = await plan_by_name(planner, "Groceries")
    first = await planner.add_shopping_item(groceries["id"], "0.5 cup milk", ALEX)
    second = await planner.add_shopping_item(groceries["id"], "120 ml milk", ALEX)
    assert second["merged"] is True
    assert second["task"]["uid"] == first["task"]["uid"]
    assert second["task"]["quantity"] == "1 cup / 240 ml"
    assert second["task"]["category"] == "Dairy"

    # Moving an item to another aisle is remembered for next time.
    await planner.move_task(first["task"]["uid"], None, category="Other")
    await planner.set_completed(first["task"]["uid"], True, ALEX)
    readded = await planner.add_shopping_item(groceries["id"], "milk", ALEX)
    assert readded["task"]["uid"] == first["task"]["uid"]  # re-opened, not duplicated
    assert readded["task"]["status"] == "needs_action"
    assert readded["task"]["category"] == "Other"

    history = await planner.db.item_history(groceries["id"])
    assert history[0]["times_added"] == 3


async def test_save_task_on_shopping_plan_dedupes(planner) -> None:
    groceries = await plan_by_name(planner, "Groceries")
    a = await planner.save_task({"plan_id": groceries["id"], "summary": "2 lbs ground beef"})
    b = await planner.save_task({"plan_id": groceries["id"], "summary": "1 lb ground beef"})
    assert a["uid"] == b["uid"]
    assert b["quantity"].startswith("3 lb")
    assert b["category"] == "Meat"


async def test_hardware_plan_defaults_category(planner) -> None:
    hardware = await plan_by_name(planner, "Hardware")
    item = await planner.add_shopping_item(hardware["id"], "zip ties", ALEX)
    assert item["task"]["category"] == "Hardware"


async def test_frequently_bought_excludes_items_on_the_list(planner) -> None:
    groceries = await plan_by_name(planner, "Groceries")
    eggs = await planner.add_shopping_item(groceries["id"], "eggs", ALEX)
    await planner.add_shopping_item(groceries["id"], "coffee", ALEX)
    await planner.set_completed(eggs["task"]["uid"], True, ALEX)
    names = [row["display_name"] for row in await planner.item_history(groceries["id"])]
    assert names == ["Eggs"]


async def test_pantry_low_pushes_ingredients(planner) -> None:
    pantry = await plan_by_name(planner, "Pantry & Spices")
    groceries = await plan_by_name(planner, "Groceries")
    item = await planner.save_pantry_item(
        {
            "plan_id": pantry["id"],
            "name": "Taco seasoning",
            "low_action": "shopping",
            "ingredients": "2 tbsp chili powder\n1 tbsp cumin",
            "is_batch": True,
        }
    )
    result = await planner.set_pantry_state(item["id"], "low", actor=ALEX)
    assert [c["summary"] for c in result["created"]] == ["Chili powder", "Cumin"]
    tasks = await planner.db.list_tasks(groceries["id"])
    assert {t["summary"]: t["category"] for t in tasks} == {"Chili powder": "Spices/Pantry", "Cumin": "Spices/Pantry"}
    assert any(event == "taskdragon_pantry_low" for event, _ in planner.test_events)
    # Going from low to out does not push twice.
    again = await planner.set_pantry_state(item["id"], "out", actor=ALEX)
    assert again["created"] == []


async def test_pantry_ask_then_batch_task_then_restock(planner) -> None:
    pantry = await plan_by_name(planner, "Pantry & Spices")
    item = await planner.save_pantry_item(
        {"plan_id": pantry["id"], "name": "BBQ rub", "mealie_recipe_slug": "bbq-rub", "ingredients": ["1/4 cup paprika"]}
    )
    asked = await planner.set_pantry_state(item["id"], "low", actor=ALEX)
    assert asked["action_needed"] is True and asked["created"] == []

    chosen = await planner.set_pantry_state(item["id"], "low", action="batch_task", actor=ALEX)
    [created] = chosen["created"]
    task = await planner.db.get_task(created["uid"])
    assert task["summary"] == "Make a new batch of BBQ rub"
    assert task["link_url"] == "http://mealie.local/g/home/r/bbq-rub"
    assert "1/4 cup paprika" in task["description"]

    # Finishing the batch chore restocks the pantry item.
    await planner.set_completed(task["uid"], True, ALEX)
    assert (await planner.db.get_pantry_item(item["id"]))["stock_state"] == "in_stock"


async def test_buying_a_staple_restocks_it(planner) -> None:
    pantry = await plan_by_name(planner, "Pantry & Spices")
    item = await planner.save_pantry_item({"plan_id": pantry["id"], "name": "Flour", "low_action": "shopping"})
    result = await planner.set_pantry_state(item["id"], "out", actor=ALEX)
    await planner.set_completed(result["created"][0]["uid"], True, ALEX)
    assert (await planner.db.get_pantry_item(item["id"]))["stock_state"] == "in_stock"


async def test_time_snooze_wakes_up(planner, clock) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task({"plan_id": chores["id"], "summary": "Call plumber", "status": "in_process"})
    await planner.snooze_until(task["uid"], "2026-10-27T18:00:00-05:00", ALEX)
    assert (await planner.db.get_task(task["uid"]))["status"] == "snoozed"
    assert await planner.wake_due_snoozes() == []
    clock.set_local(2026, 10, 27, 18, 1)
    assert await planner.wake_due_snoozes() == [task["uid"]]
    woke = await planner.db.get_task(task["uid"])
    assert woke["status"] == "in_process"  # previous status restored
    assert any(event == "taskdragon_task_unsnoozed" for event, _ in planner.test_events)


async def test_context_snooze_rules(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task({"plan_id": chores["id"], "summary": "Clean gutters"})
    await planner.snooze_context(task["uid"], "person_home", {}, ALEX)
    [rule] = await planner.db.list_snooze_rules()
    assert rule["config"] == {"person": "person.alex"}

    await planner.snooze_context(task["uid"], "all_home", {}, ALEX)
    [rule] = await planner.db.list_snooze_rules()
    assert rule["config"] == {"people": ["person.alex", "person.sam"]}

    await planner.snooze_context(task["uid"], "weekend_dry", {}, ALEX)
    [rule] = await planner.db.list_snooze_rules()
    assert rule["config"] == {"weather_entity": "weather.home"}

    await planner.snooze_context(task["uid"], "entity_state", {"entity_id": "sensor.temp", "above": "60"}, ALEX)
    [rule] = await planner.db.list_snooze_rules()
    assert rule["config"] == {"entity_id": "sensor.temp", "above": 60.0}

    with pytest.raises(PlannerError):
        await planner.snooze_context(task["uid"], "entity_state", {"entity_id": "sensor.temp"}, ALEX)
    with pytest.raises(PlannerError):
        await planner.snooze_context(task["uid"], "person_home", {}, Actor(user_name="guest"))

    await planner.unsnooze(task["uid"])
    assert await planner.db.list_snooze_rules() == []
    assert (await planner.db.get_task(task["uid"]))["status"] == "needs_action"


async def test_snoozed_blocked_task_wakes_up_blocked(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    a = await planner.save_task({"plan_id": projects["id"], "summary": "A"})
    b = await planner.save_task({"plan_id": projects["id"], "summary": "B", "blocked_by": [a["uid"]]})
    await planner.snooze_until(b["uid"], "2026-10-28T08:00:00-05:00", ALEX)
    await planner.set_completed(a["uid"], True, ALEX)
    assert (await planner.db.get_task(b["uid"]))["status"] == "snoozed"
    await planner.unsnooze(b["uid"])
    assert (await planner.db.get_task(b["uid"]))["status"] == "needs_action"


async def test_packing_clone_and_reset(planner) -> None:
    template = await plan_by_name(planner, "Trip Packing")
    toiletries = await planner.save_task({"plan_id": template["id"], "summary": "Toiletry bag", "category": "Bathroom"})
    await planner.save_task({"parent_uid": toiletries["uid"], "summary": "Toothbrush"})
    trip = await planner.clone_plan(template["id"], "Lake cabin", "2026-11-06")
    assert trip["is_template"] is False and trip["template_id"] == template["id"]
    items = await planner.db.list_tasks(trip["id"])
    assert sorted(t["summary"] for t in items) == ["Toiletry bag", "Toothbrush"]
    parent = next(t for t in items if t["summary"] == "Toiletry bag")
    child = next(t for t in items if t["summary"] == "Toothbrush")
    assert child["parent_uid"] == parent["uid"] and parent["due_date"] == "2026-11-06"

    await planner.set_completed(parent["uid"], True, ALEX)
    assert await planner.reset_plan(trip["id"]) == 2
    assert {t["status"] for t in await planner.db.list_tasks(trip["id"])} == {"needs_action"}
    # The master checklist is untouched.
    assert len(await planner.db.list_tasks(template["id"])) == 2


async def test_bom_push_and_acquire(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    hardware = await plan_by_name(planner, "Hardware")
    stud = await planner.save_bom_item({"plan_id": projects["id"], "name": "2x4x8 stud", "quantity": "12"})
    await planner.save_bom_item({"plan_id": projects["id"], "name": "Deck screws", "acquired": True})
    pushed = await planner.push_bom(projects["id"], actor=ALEX)
    assert [p["summary"] for p in pushed] == ["2x4x8 stud"]
    task = await planner.db.get_task(pushed[0]["uid"])
    assert task["plan_id"] == hardware["id"] and task["quantity"] == "12" and task["category"] == "Hardware"
    assert await planner.push_bom(projects["id"], actor=ALEX) == []  # already on the list

    await planner.set_completed(task["uid"], True, ALEX)
    assert (await planner.db.get_bom_item(stud["id"]))["acquired"] is True


async def test_claim_and_bulk_actions(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    a = await planner.save_task({"plan_id": chores["id"], "summary": "A", "assigned_to": "pool"})
    b = await planner.save_task({"plan_id": chores["id"], "summary": "B"})
    assert a["assigned_to"] == []
    claimed = await planner.claim_task(a["uid"], ALEX.person, ALEX)
    assert claimed["assigned_to"] == ["person.alex"]

    await planner.bulk([a["uid"], b["uid"]], "assign", "person.sam", ALEX)
    assert {t["assigned_to"][0] for t in await planner.db.get_tasks([a["uid"], b["uid"]])} == {"person.sam"}
    result = await planner.bulk([a["uid"], b["uid"]], "complete", None, ALEX)
    assert result["count"] == 2
    snapshot = await planner.clear_completed(chores["id"])
    assert len(snapshot["tasks"]) == 2
    await planner.restore(snapshot)
    assert len(await planner.db.list_tasks(chores["id"])) == 2


async def test_move_reorders_siblings(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    a, b, c = [await planner.save_task({"plan_id": chores["id"], "summary": s}) for s in "ABC"]
    await planner.move_task(c["uid"], None)
    await planner.move_task(a["uid"], b["uid"])
    order = [t["summary"] for t in await planner.db.siblings(chores["id"], None)]
    assert order == ["C", "B", "A"]


async def test_moving_task_to_another_plan_moves_subtasks(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    projects = await plan_by_name(planner, "Projects")
    parent = await planner.save_task({"plan_id": chores["id"], "summary": "Garage cleanout"})
    child = await planner.save_task({"parent_uid": parent["uid"], "summary": "Sort bins"})
    moved = await planner.save_task({"uid": parent["uid"], "plan_id": projects["id"]})
    assert moved["kanban_column"] == "backlog"
    assert (await planner.db.get_task(child["uid"]))["plan_id"] == projects["id"]


async def test_leftover_expiry_and_generated_task_upsert(planner, clock) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.upsert_generated_task(
        "leftover",
        "leftover:42",
        {"plan_id": chores["id"], "summary": "Eat or freeze leftover Chili (Cooked Tuesday)", "expires_at": "2026-10-30T20:00:00-05:00"},
    )
    same = await planner.upsert_generated_task("leftover", "leftover:42", {"plan_id": chores["id"], "summary": "Eat or freeze leftover Chili (Cooked Tuesday)"})
    assert same["uid"] == task["uid"]
    assert await planner.expire_leftovers() == 0
    clock.set_local(2026, 10, 30, 20, 1)
    assert await planner.expire_leftovers() == 1
    assert (await planner.db.get_task(task["uid"]))["priority"] == "urgent"
    assert await planner.expire_leftovers() == 0

    removed = await planner.remove_generated_tasks("leftover", lambda ref: False)
    assert removed == 1


async def test_validation_errors(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    with pytest.raises(PlannerError):
        await planner.save_task({"plan_id": chores["id"], "summary": "   "})
    with pytest.raises(PlannerError):
        await planner.save_task({"plan_id": "missing", "summary": "x"})
    with pytest.raises(PlannerError):
        await planner.create_plan({"name": "X", "archetype": "nope"})
    with pytest.raises(PlannerError):
        await planner.save_task({"uid": (await planner.save_task({"plan_id": chores["id"], "summary": "p"}))["uid"], "status": "weird"})


async def test_plan_crud(planner) -> None:
    plan = await planner.create_plan({"name": "Costco", "archetype": "shopping_errands", "settings": {"role": None}})
    updated = await planner.update_plan(plan["id"], {"name": "Costco run", "settings": {"mealie_list_id": "abc"}})
    assert updated["name"] == "Costco run" and updated["settings"] == {"mealie_list_id": "abc"}
    await planner.delete_plan(plan["id"])
    assert await planner.db.get_plan(plan["id"]) is None


async def test_retired_plan_icons_are_replaced(planner) -> None:
    plan = await planner.db.create_plan({"name": "Old maintenance", "archetype": "home_maintenance", "icon": "mdi:home-wrench"})
    await planner.ensure_defaults()
    assert (await planner.db.get_plan(plan["id"]))["icon"] == "mdi:tools"
    assert all(p["icon"] != "mdi:home-wrench" for p in await planner.db.list_plans(include_archived=True))
