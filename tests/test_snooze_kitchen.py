"""Context-snooze rule evaluation and Mealie prep-timeline rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import TZ

from omniplan.kitchen import (
    build_prep_steps,
    discover_rules,
    find_meat,
    ingredient_lines,
    leftover_summary,
    parse_configured_rules,
)
from omniplan.mealie_client import item_hash, item_text
from omniplan.snooze import (
    StateView,
    caught_up_on_startup,
    entity_condition_met,
    entity_condition_reached,
    everyone_home,
    person_arrived,
    validate_rule,
    weekend_dry_should_wake,
)

ARMED = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Snooze rules
# ---------------------------------------------------------------------------
def test_person_arrived_requires_a_transition() -> None:
    assert person_arrived("not_home", "home")
    assert person_arrived("work", "home")
    assert not person_arrived("home", "home")
    assert not person_arrived("home", "not_home")


def test_everyone_home() -> None:
    assert everyone_home({"person.a": "home", "person.b": "home"}, ["person.a", "person.b"])
    assert not everyone_home({"person.a": "home", "person.b": "work"}, ["person.a", "person.b"])
    assert not everyone_home({}, [])


@pytest.mark.parametrize(
    ("state", "config", "met"),
    [
        ("on", {"state": "on"}, True),
        ("ON", {"state": "on"}, True),
        ("off", {"state": "on"}, False),
        ("61.5", {"above": 60}, True),
        ("59", {"above": 60}, False),
        ("50", {"above": 40, "below": 60}, True),
        ("abc", {"below": 60}, False),
        ("unavailable", {"state": "unavailable"}, False),
    ],
)
def test_entity_condition_met(state: str, config: dict, met: bool) -> None:
    assert entity_condition_met(state, config) is met


def test_entity_condition_reached_is_edge_triggered() -> None:
    assert entity_condition_reached("55", "65", {"above": 60})
    assert not entity_condition_reached("62", "65", {"above": 60})


def test_weekend_dry() -> None:
    saturday, monday = date(2026, 10, 31), date(2026, 11, 2)
    assert weekend_dry_should_wake(saturday, "sunny")
    assert weekend_dry_should_wake(saturday, "partlycloudy")
    assert not weekend_dry_should_wake(saturday, "rainy")
    assert not weekend_dry_should_wake(saturday, "lightning-rainy")
    assert not weekend_dry_should_wake(saturday, "snowy")
    assert not weekend_dry_should_wake(monday, "sunny")
    assert weekend_dry_should_wake(saturday, None)  # unknown forecast never strands the task


def test_validate_rule() -> None:
    assert validate_rule("entity_state", {"entity_id": "sensor.x", "below": "5"}) == {"entity_id": "sensor.x", "below": 5.0}
    with pytest.raises(ValueError):
        validate_rule("entity_state", {"entity_id": "nope", "state": "on"})
    with pytest.raises(ValueError):
        validate_rule("time", {})
    with pytest.raises(ValueError):
        validate_rule("bogus", {})


def test_startup_catch_up() -> None:
    later = ARMED + timedelta(hours=2)
    earlier = ARMED - timedelta(hours=2)
    views = {
        "person.a": StateView("home", later),
        "person.b": StateView("home", earlier),
        "binary_sensor.door": StateView("on", later),
    }
    assert caught_up_on_startup("person_home", ARMED, views, {"person": "person.a"}, [])
    assert not caught_up_on_startup("person_home", ARMED, views, {"person": "person.b"}, [])
    assert caught_up_on_startup("all_home", ARMED, views, {}, ["person.a", "person.b"])
    assert caught_up_on_startup("entity_state", ARMED, views, {"entity_id": "binary_sensor.door", "state": "on"}, [])


# ---------------------------------------------------------------------------
# Mealie prep timelines
# ---------------------------------------------------------------------------
BRISKET = {
    "name": "Smoked Brisket",
    "slug": "smoked-brisket",
    "tags": [{"name": "thaw-36h", "slug": "thaw-36h"}, {"name": "smoker-4h", "slug": "smoker-4h"}, {"name": "BBQ", "slug": "bbq"}],
    "recipeIngredient": [
        {"quantity": 12, "unit": {"name": "pound", "abbreviation": "lb"}, "food": {"name": "beef brisket"}, "note": "packer cut"},
        {"quantity": 0.25, "unit": {"name": "cup"}, "food": {"name": "kosher salt"}, "note": ""},
        {"note": "1/4 cup coarse black pepper", "display": "1/4 cup coarse black pepper"},
    ],
    "notes": [{"title": "Prep", "text": "Lead time: brine 24h"}],
    "extras": {},
}
CONFIGURED = parse_configured_rules(
    "thaw-36h => 36 | Move {meat} from freezer to fridge to thaw for {recipe}\n"
    "smoker-4h => 4 | Prep rub and fire up smoker for {recipe}\n"
    "# comment\n"
    "bad line\n"
    "pizza => 2 | Make the dough for {recipe}"
)


def test_parse_configured_rules() -> None:
    assert [(r.key, r.hours) for r in CONFIGURED] == [("thaw", 36.0), ("smoker", 4.0), ("pizza", 2.0)]


def test_discover_rules_from_tags_notes_and_keywords() -> None:
    rules = {r.key: r.hours for r in discover_rules(BRISKET, CONFIGURED)}
    assert rules == {"thaw": 36.0, "brine": 24.0, "smoker": 4.0}
    pizza = discover_rules({"name": "Friday pizza", "tags": []}, CONFIGURED)
    assert [(r.key, r.hours) for r in pizza] == [("pizza", 2.0)]


def test_extras_override_hours() -> None:
    recipe = {**BRISKET, "notes": [], "extras": {"omniplan": "thaw 48h, rest 30m"}}
    rules = {r.key: r.hours for r in discover_rules(recipe, CONFIGURED)}
    assert rules["thaw"] == 48.0 and rules["rest"] == 0.5


def test_build_prep_steps_spec_examples() -> None:
    dinner = datetime(2026, 10, 31, 18, 0, tzinfo=TZ)
    steps = {step.key: step for step in build_prep_steps(BRISKET, dinner, CONFIGURED)}
    assert steps["thaw"].summary == "Move brisket from freezer to fridge to thaw for Smoked Brisket"
    assert steps["thaw"].due == dinner - timedelta(hours=36)
    assert steps["smoker"].summary == "Prep rub and fire up smoker for Smoked Brisket"
    assert steps["smoker"].due == datetime(2026, 10, 31, 14, 0, tzinfo=TZ)
    assert steps["brine"].summary == "Start the brine for Smoked Brisket"


def test_find_meat_and_ingredients() -> None:
    assert find_meat(BRISKET) == "brisket"
    assert find_meat({"name": "Veggie Curry", "recipeIngredient": []}) is None
    lines = ingredient_lines(BRISKET)
    assert lines[0]["text"] == "12 lb beef brisket"
    assert lines[2]["name"] == "1/4 cup coarse black pepper"


def test_leftover_summary() -> None:
    assert leftover_summary("Chili", date(2026, 10, 27)) == "Eat or freeze leftover Chili (Cooked Tuesday)"


def test_mealie_item_text_and_hash() -> None:
    structured = {"quantity": 2, "unit": {"abbreviation": "lb"}, "food": {"name": "ground beef"}, "note": "", "display": ""}
    assert item_text(structured) == "2 lb ground beef"
    assert item_text({"display": "Milk (1 cup)"}) == "Milk (1 cup)"
    assert item_hash("Milk", False) == item_hash(" milk ", False) != item_hash("Milk", True)
