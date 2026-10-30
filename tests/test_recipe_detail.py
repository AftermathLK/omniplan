"""The cooking view: recipe detail parsing, step timers and batch scaling."""

from __future__ import annotations

import pytest
from conftest import plan_by_name
from test_recipe_browser import ALEX, ORZO, TODAY, FakeClient, Tick

from omniplan.planner import PlannerError
from omniplan.recipe_browser import RecipeBrowser
from omniplan.recipes import DEFAULT_SEASONINGS, ingredient_rows, parse_durations, recipe_detail, timer_label

LASAGNA = {
    "id": "5a5a5a5a-1111-4222-8333-444455556666",
    "slug": "lasagna",
    "name": "Weeknight Lasagna",
    "description": "Layers of goodness.",
    "image": "abc",
    "recipeYield": "8 servings",
    "prepTime": "20 minutes",
    "performTime": "45 minutes",
    "totalTime": "1 hour 5 minutes",
    "orgURL": "javascript:alert(1)",
    "recipeCategory": [{"name": "Pasta"}],
    "tags": [{"name": "Freezer"}],
    "recipeIngredient": [
        {"title": "Sauce", "quantity": 1, "unit": {"name": "pound", "pluralName": "pounds", "abbreviation": "lb"}, "food": {"name": "ground beef"}, "display": "1 pound ground beef"},
        {"quantity": 0.5, "unit": {"name": "teaspoon", "abbreviation": "tsp", "useAbbreviation": True}, "food": {"name": "kosher salt"}},
        {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "marinara sauce"}, "note": "jarred is fine"},
        {"title": "Cheese layer", "note": "1 (15 oz) container ricotta"},
        {"quantity": 1, "food": {"name": "egg", "pluralName": "eggs"}},
        {"title": "Only a heading"},
    ],
    "recipeInstructions": [
        {"title": "Brown", "text": "Brown the beef for 8-10 minutes, then simmer with the sauce 15 minutes."},
        {"text": "Layer, cover and bake for 25 to 30 mins. Rest half an hour."},
        "Serve.",
        {"text": ""},
    ],
    "notes": [{"title": "Make ahead", "text": "Freezes for 3 months."}, {"title": "", "text": ""}],
    "nutrition": {"calories": "540", "proteinContent": "32 g", "sodiumContent": None},
}


def test_step_timers() -> None:
    first, second = (parse_durations(step["text"]) for step in LASAGNA["recipeInstructions"][:2])
    assert [(t["seconds"], t["max_seconds"], t["label"]) for t in first] == [(480, 600, "8–10m"), (900, None, "15m")]
    assert [(t["seconds"], t["max_seconds"]) for t in second] == [(1500, 1800), (1800, None)]
    assert parse_durations("Smoke 1h30m at 225F, then wrap for 2 hours") == [
        {"seconds": 5400, "max_seconds": None, "label": "1h 30m", "text": "1h30m"},
        {"seconds": 7200, "max_seconds": None, "label": "2h", "text": "2 hours"},
    ]
    assert parse_durations("Stir in 2 cups cheese at 350 degrees") == []
    assert parse_durations("Let it sit for 3 days") == []  # days are not kitchen timers
    assert timer_label(45) == "45s" and timer_label(3600) == "1h" and timer_label(90) == "1m 30s"


def test_recipe_detail_flags_sections_and_scaling_data() -> None:
    pantry = [{"name": "Marinara sauce", "stock_state": "in_stock"}, {"name": "Eggs", "stock_state": "out"}]
    detail = recipe_detail(LASAGNA, seasonings=DEFAULT_SEASONINGS, skip_seasonings=True, pantry=pantry, url="http://mealie.local/r/lasagna")
    kinds = [(row["type"], row.get("title") or row.get("food") or row.get("text")) for row in detail["ingredients"]]
    assert kinds == [
        ("section", "Sauce"),
        ("ingredient", "ground beef"),
        ("ingredient", "kosher salt"),
        ("ingredient", "marinara sauce"),
        ("section", "Cheese layer"),
        ("ingredient", "1 (15 oz) container ricotta"),
        ("ingredient", "egg"),
        ("section", "Only a heading"),
    ]
    beef, salt, sauce, ricotta, egg = (row for row in detail["ingredients"] if row["type"] == "ingredient")
    assert beef["quantity"] == 1 and beef["unit"]["plural"] == "pounds" and beef["display"] == "1 pound ground beef"
    assert salt["is_excluded_spice"] and salt["skip"] == "seasoning" and salt["unit"]["use_abbreviation"]
    assert sauce["in_pantry"] and sauce["pantry_item"] == "Marinara sauce" and sauce["note"] == "jarred is fine"
    assert ricotta["quantity"] is None and ricotta["unit"] is None  # free text can't be scaled
    assert egg["food_plural"] == "eggs" and not egg["in_pantry"]  # an "out" pantry item doesn't count
    assert [row["index"] for row in (beef, salt, sauce, ricotta, egg)] == [r["index"] for r in ingredient_rows(LASAGNA)]
    assert detail["counts"] == {"ingredients": 5, "pantry": 1, "seasonings": 1, "missing": 3}
    assert [s["text"] for s in detail["instructions"]] == [LASAGNA["recipeInstructions"][0]["text"], LASAGNA["recipeInstructions"][1]["text"], "Serve."]
    assert detail["instructions"][0]["title"] == "Brown" and detail["instructions"][2]["timers"] == []
    assert detail["notes"] == [{"title": "Make ahead", "text": "Freezes for 3 months."}]
    assert detail["nutrition"] == [{"key": "calories", "label": "Calories", "value": "540"}, {"key": "proteinContent", "label": "Protein", "value": "32 g"}]
    assert detail["servings"] == 8 and detail["cook_time"] == "45 minutes" and detail["source_url"] is None
    assert detail["categories"] == ["Pasta"] and detail["tags"] == ["Freezer"] and detail["has_image"]

    keep = recipe_detail(LASAGNA, seasonings=DEFAULT_SEASONINGS, skip_seasonings=False, pantry=[], url=None)
    assert [row["skip"] for row in keep["ingredients"] if row["type"] == "ingredient"] == [None] * 5


def test_ingredient_rows_scale_parsed_amounts_only() -> None:
    rows = {row["index"]: row["text"] for row in ingredient_rows(LASAGNA, 2)}
    assert rows[0] == "2 lb ground beef" and rows[1] == "1 tsp kosher salt" and rows[2] == "4 cup marinara sauce"
    assert rows[3] == "1 (15 oz) container ricotta"
    assert ingredient_rows(LASAGNA, 1 / 3)[2]["text"] == "0.667 cup marinara sauce"


async def test_browser_detail_scaled_add_and_full_size_image(planner) -> None:
    client = FakeClient()
    client.recipes[LASAGNA["slug"]] = LASAGNA
    browser = RecipeBrowser(mealie=lambda: client, planner=planner, db=planner.db, today=lambda: TODAY, monotonic=Tick())
    detail = await browser.detail(LASAGNA["slug"], ALEX)
    assert detail["image_url"] == f"/api/omniplan/mealie_image/{LASAGNA['id']}?size=original"
    assert detail["url"].endswith("/r/lasagna") and detail["skip_seasonings"] is True

    groceries = await plan_by_name(planner, "Groceries")
    missing = [row["index"] for row in detail["ingredients"] if row["type"] == "ingredient" and not row["skip"]]
    result = await browser.add_to_groceries(LASAGNA["slug"], ALEX, indices=missing, scale=2)
    assert sorted(result["added"]) == ["(15 oz) container ricotta", "Egg", "Ground beef", "Marinara sauce"]
    items = {t["summary"]: t for t in await planner.db.list_tasks(groceries["id"], top_level_only=True)}
    assert items["Ground beef"]["quantity"].startswith("2 lb")
    with pytest.raises(PlannerError):
        await browser.add_to_groceries(LASAGNA["slug"], ALEX, scale=50)

    await browser.image(ORZO["id"], "original")
    await browser.image(ORZO["id"])
    assert client.image_calls == 2  # the two sizes are cached separately
    with pytest.raises(PlannerError):
        await browser.image(ORZO["id"], "huge")
