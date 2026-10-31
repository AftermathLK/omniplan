"""Brain dump parsing (rules engine and LLM output validation)."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import TZ

from taskdragon.brain_dump import (
    BrainDumpContext,
    async_parse,
    build_prompt,
    extract_json,
    extract_when,
    normalize_llm_actions,
    parse_with_rules,
    split_clauses,
    split_items,
)

PLANS = [
    {"id": "chores", "name": "Daily Chores", "archetype": "daily_chores", "settings": {"role": "chores"}},
    {"id": "groc", "name": "Groceries", "archetype": "shopping_errands", "settings": {"role": "groceries"}},
    {"id": "hw", "name": "Hardware", "archetype": "shopping_errands", "settings": {"role": "hardware"}},
    {"id": "proj", "name": "Projects", "archetype": "projects_diy", "settings": {}},
    {"id": "maint", "name": "Home Maintenance", "archetype": "home_maintenance", "settings": {}},
]
PEOPLE = [{"entity_id": "person.alex", "name": "Alex"}, {"entity_id": "person.sam", "name": "Sam Smith"}]


@pytest.fixture
def ctx() -> BrainDumpContext:
    # Tuesday 2026-10-27 at 21:00
    return BrainDumpContext(now=datetime(2026, 10, 27, 21, 0, tzinfo=TZ), plans=PLANS, people=PEOPLE, speaker="person.alex")


def test_spec_example(ctx: BrainDumpContext) -> None:
    actions = parse_with_rules(
        "Remind me to pick up 2x4s at Menards on Saturday morning and add garlic powder to the grocery list", ctx
    )
    assert actions == [
        {
            "type": "task",
            "summary": "Pick up 2x4s at Menards",
            "plan_id": "proj",
            "due_date": "2026-10-31",
            "due_time": None,
            "time_of_day_bucket": "morning",
            "context_tags": ["Errand"],
            "assigned_to": ["person.alex"],
            "priority": "none",
            "estimated_minutes": None,
        },
        {"type": "shopping", "name": "Garlic powder", "quantity": None, "category": "Spices/Pantry", "plan_id": "groc"},
    ]


def test_split_clauses_keeps_items_together() -> None:
    assert split_clauses("buy milk and eggs and call mom") == ["buy milk and eggs", "call mom"]
    assert split_clauses("clean the garage; mow the lawn") == ["clean the garage", "mow the lawn"]
    assert split_clauses("water plants, then feed the cat") == ["water plants", "feed the cat"]


def test_split_items_respects_compounds() -> None:
    assert split_items("milk, eggs and salt and pepper") == ["milk", "eggs", "salt and pepper"]
    assert split_items("some bananas & the bread") == ["bananas", "bread"]


def test_multiple_items_and_quantities(ctx: BrainDumpContext) -> None:
    actions = parse_with_rules("add 2 lbs ground beef, milk and 3 cans of tomatoes to the shopping list", ctx)
    assert [(a["name"], a["quantity"], a["category"]) for a in actions] == [
        ("Ground beef", "2 lbs", "Meat"),
        ("Milk", None, "Dairy"),
        ("Tomatoes", "3 cans", "Spices/Pantry"),
    ]


def test_were_out_of_goes_to_groceries(ctx: BrainDumpContext) -> None:
    [action] = parse_with_rules("we're out of paper towels", ctx)
    assert action["type"] == "shopping" and action["plan_id"] == "groc"


def test_hardware_items_go_to_hardware_list(ctx: BrainDumpContext) -> None:
    [action] = parse_with_rules("add deck screws to the hardware list", ctx)
    assert action["plan_id"] == "hw" and action["category"] == "Hardware"


def test_assignment_by_name_and_time(ctx: BrainDumpContext) -> None:
    actions = parse_with_rules("ask Sam to mow the lawn tomorrow afternoon and call the dentist at 3pm", ctx)
    mow, call = actions
    assert mow["summary"] == "Mow the lawn"
    assert mow["assigned_to"] == ["person.sam"]
    assert mow["due_date"] == "2026-10-28" and mow["time_of_day_bucket"] == "afternoon"
    assert mow["context_tags"] == ["Garage/Yard"]
    assert call["due_time"] == "15:00" and call["summary"] == "Call the dentist"


def test_maintenance_routing_priority_and_estimate(ctx: BrainDumpContext) -> None:
    [action] = parse_with_rules("change the furnace filter next week, urgent, 15 min", ctx)
    assert action["plan_id"] == "maint"
    assert action["priority"] == "urgent"
    assert action["estimated_minutes"] == 15
    assert action["due_date"] == "2026-11-02"


@pytest.mark.parametrize(
    ("phrase", "due", "bucket"),
    [
        ("do it tonight", "2026-10-27", "evening"),
        ("do it this weekend", "2026-10-31", None),
        ("do it in 3 days", "2026-10-30", None),
        ("do it on 11/5", "2026-11-05", None),
        ("do it on Nov 12th", "2026-11-12", None),
        ("do it on Tuesday", "2026-11-03", None),
        ("do it next Friday", "2026-11-06", None),
    ],
)
def test_extract_when(phrase: str, due: str, bucket: str | None) -> None:
    text, info = extract_when(phrase, datetime(2026, 10, 27, tzinfo=TZ).date())
    assert text == "do it"
    assert info.due_date.isoformat() == due
    assert info.bucket == bucket


def test_bare_add_is_shopping(ctx: BrainDumpContext) -> None:
    [action] = parse_with_rules("add bananas", ctx)
    assert action == {"type": "shopping", "name": "Bananas", "quantity": None, "category": "Produce", "plan_id": "groc"}


def test_add_to_named_task_plan_creates_task(ctx: BrainDumpContext) -> None:
    [action] = parse_with_rules("add vacuum the stairs to the daily chores list", ctx)
    assert action["type"] == "task" and action["plan_id"] == "chores" and action["summary"] == "Vacuum the stairs"


def test_llm_output_is_validated_and_repaired(ctx: BrainDumpContext) -> None:
    reply = """Sure! ```json
    {"actions": [
      {"type": "task", "summary": "pick up 2x4s", "plan_id": "made-up", "due_date": "2026-10-31",
       "time_of_day_bucket": "morning", "context_tags": ["Errand", "Bogus"], "assigned_to": ["person.alex", "person.ghost"],
       "priority": "critical", "estimated_minutes": "30"},
      {"type": "shopping", "name": "garlic powder", "plan_id": "chores"},
      {"type": "unknown"}
    ]}
    ```"""
    actions = normalize_llm_actions(extract_json(reply), ctx)
    task, item = actions
    assert task["plan_id"] == "proj"  # invented id rerouted
    assert task["context_tags"] == ["Errand"]
    assert task["assigned_to"] == ["person.alex"]
    assert task["priority"] == "none"
    assert task["estimated_minutes"] == 30
    assert item["plan_id"] == "groc" and item["category"] == "Spices/Pantry"


def test_prompt_mentions_plans_people_and_date(ctx: BrainDumpContext) -> None:
    prompt = build_prompt("hello", ctx)
    assert "id=groc" in prompt and "person.sam" in prompt and "2026-10-27 (Tuesday)" in prompt


class FakeServices:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple] = []

    async def async_call(self, domain, service, data, blocking=False, return_response=False):
        self.calls.append((domain, service, data))
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"response": {"speech": {"plain": {"speech": self.reply}}}}


class FakeHass:
    def __init__(self, reply: str | Exception) -> None:
        self.services = FakeServices(reply)


async def test_async_parse_uses_llm_when_configured(ctx: BrainDumpContext) -> None:
    hass = FakeHass('{"actions": [{"type": "shopping", "name": "Milk"}]}')
    actions, engine = await async_parse(hass, "milk please", ctx, "conversation.ollama")
    assert engine == "llm"
    assert actions[0]["name"] == "Milk"
    assert hass.services.calls[0][:2] == ("conversation", "process")
    assert hass.services.calls[0][2]["agent_id"] == "conversation.ollama"


async def test_async_parse_falls_back_on_bad_llm_reply(ctx: BrainDumpContext) -> None:
    for reply in ("I'm sorry, I can't help with that.", RuntimeError("agent offline")):
        actions, engine = await async_parse(FakeHass(reply), "add bananas", ctx, "conversation.ollama")
        assert engine == "rules"
        assert actions[0]["name"] == "Bananas"


async def test_async_parse_without_agent_uses_rules(ctx: BrainDumpContext) -> None:
    hass = FakeHass("unused")
    actions, engine = await async_parse(hass, "add bananas", ctx, None)
    assert engine == "rules" and hass.services.calls == []
    assert actions[0]["name"] == "Bananas"
