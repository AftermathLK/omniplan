"""Recipe browser on a real SQLite planner with a fake Mealie client."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import plan_by_name

from taskdragon.mealie_client import MealieError
from taskdragon.planner import Actor, PlannerError
from taskdragon.recipe_browser import RecipeBrowser

ALEX = Actor(user_id="u-alex", user_name="Alex", person="person.alex", is_admin=True)
SAM = Actor(user_id="u-sam", user_name="Sam", person="person.sam")
TODAY = date(2026, 10, 27)

ORZO = {
    "id": "0d2b6f1a-1111-4222-8333-444455556666",
    "slug": "chicken-sausage-orzo",
    "name": "Chicken Sausage Orzo",
    "recipeIngredient": [
        {"quantity": 12, "unit": {"abbreviation": "oz"}, "food": {"name": "chicken sausage"}},
        {"quantity": 1.5, "unit": {"name": "cup"}, "food": {"name": "orzo"}},
        {"quantity": 1, "unit": {"abbreviation": "tsp"}, "food": {"name": "kosher salt"}},
        {"note": "Freshly ground black pepper"},
        {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "chicken broth"}},
        {"quantity": 1, "food": {"name": "yellow onion"}},
    ],
}


class FakeClient:
    def __init__(self) -> None:
        self.list_calls = 0
        self.image_calls = 0
        self.planned: list[tuple[date, str, str]] = []
        self.recipes = {ORZO["slug"]: ORZO}

    def recipe_url(self, slug):
        return f"http://mealie.local/g/home/r/{slug}"

    async def async_list_recipes(self):
        self.list_calls += 1
        return [
            {"id": ORZO["id"], "slug": ORZO["slug"], "name": ORZO["name"], "recipeCategory": [{"name": "Chicken"}], "tags": [{"name": "Weeknight"}], "image": "k1"},
            {"id": "aaaabbbb-0000", "slug": "brisket", "name": "Smoked Brisket", "recipeCategory": [{"name": "Smoker/BBQ"}], "tags": []},
            {"slug": "", "name": "broken"},
        ]

    async def async_get_mealplans(self, start, end):
        orzo = {"recipeId": ORZO["id"], "recipe": {"id": ORZO["id"], "slug": ORZO["slug"]}}
        return [orzo, dict(orzo), {"recipeId": "aaaabbbb-0000", "recipe": {"slug": "brisket"}}]

    async def async_get_recipe(self, slug):
        if slug not in self.recipes:
            raise MealieError("Recipe not found in Mealie")
        return self.recipes[slug]

    async def async_create_mealplan(self, day, recipe_id, entry_type="dinner"):
        self.planned.append((day, recipe_id, entry_type))
        return {"id": "mp1"}

    async def async_get_recipe_image(self, recipe_id, size="min-original.webp"):
        self.image_calls += 1
        return b"RIFF....WEBP" + recipe_id.encode(), "image/webp"


class Tick:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def setup(planner):
    client = FakeClient()
    holder = {"client": client}
    changed: list[bool] = []
    tick = Tick()
    browser = RecipeBrowser(
        mealie=lambda: holder["client"],
        planner=planner,
        db=planner.db,
        today=lambda: TODAY,
        on_mealplan_changed=lambda: changed.append(True),
        monotonic=tick,
    )
    return browser, client, holder, changed, tick


async def test_recipe_list_is_cached_with_catalog_and_counts(setup) -> None:
    browser, client, _holder, _changed, tick = setup
    data = await browser.recipes()
    assert [r["slug"] for r in data["recipes"]] == ["chicken-sausage-orzo", "brisket"]
    orzo = data["recipes"][0]
    assert orzo["times_cooked"] == 2 and orzo["has_image"] and orzo["url"].endswith("/r/chicken-sausage-orzo")
    assert data["categories"] == ["Chicken", "Smoker/BBQ"] and data["tags"] == ["Weeknight"]
    assert data["settings"]["skip_seasonings"] is True and "kosher salt" in data["settings"]["seasonings"]
    await browser.recipes()
    assert client.list_calls == 1  # cached
    tick.value += 16 * 60
    await browser.recipes()
    assert client.list_calls == 2  # expired
    await browser.recipes(refresh=True)
    assert client.list_calls == 3


async def test_settings_are_cleaned_and_persisted(setup) -> None:
    browser, *_ = setup
    saved = await browser.save_settings({"skip_seasonings": False, "seasonings": [" Cumin ", "cumin", "MSG"]})
    assert saved["skip_seasonings"] is False and saved["seasonings"] == ["cumin", "msg"]
    again = await browser.settings()
    assert again["seasonings"] == ["cumin", "msg"] and "salt" in again["defaults"]


async def test_preview_marks_seasonings_and_only_visible_pantry_items(setup, planner) -> None:
    browser, *_ = setup
    pantry = await plan_by_name(planner, "Pantry & Spices")
    broth = await planner.save_pantry_item({"plan_id": pantry["id"], "name": "Chicken broth"})
    await planner.set_pantry_state(broth["id"], "in_stock")
    secret = await planner.create_plan({"name": "Sam's stash", "archetype": "pantry_spices", "is_private": True}, SAM)
    onion = await planner.save_pantry_item({"plan_id": secret["id"], "name": "Yellow onion"})
    await planner.set_pantry_state(onion["id"], "in_stock")

    as_alex = await browser.preview(ORZO["slug"], ALEX)
    reasons = {row["text"]: (row["skip"], row["pantry_item"]) for row in as_alex["ingredients"]}
    assert reasons["1 tsp kosher salt"] == ("seasoning", None)
    assert reasons["Freshly ground black pepper"] == ("seasoning", None)
    assert reasons["2 cup chicken broth"] == ("pantry", "Chicken broth")
    assert reasons["1 yellow onion"] == (None, None)  # Sam's private pantry is invisible to Alex
    as_sam = await browser.preview(ORZO["slug"], SAM)
    assert {row["text"]: row["skip"] for row in as_sam["ingredients"]}["1 yellow onion"] == "pantry"

    keep = await browser.preview(ORZO["slug"], ALEX, skip_seasonings=False)
    assert keep["skip_seasonings"] is False and [r["text"] for r in keep["ingredients"] if r["skip"]] == ["2 cup chicken broth"]


async def test_add_to_groceries_instant_and_reviewed(setup, planner) -> None:
    browser, client, _holder, changed, _tick = setup
    groceries = await plan_by_name(planner, "Groceries")
    result = await browser.add_to_groceries(ORZO["slug"], ALEX)
    assert result["plan_id"] == groceries["id"] and result["recipe"] == "Chicken Sausage Orzo"
    assert sorted(result["added"]) == ["Chicken broth", "Chicken sausage", "Orzo", "Yellow onion"]
    assert result["skipped_seasonings"] == 2 and result["skipped_pantry"] == 0 and result["meal_plan"] is None
    items = {t["summary"]: t for t in await planner.db.list_tasks(groceries["id"], top_level_only=True)}
    assert items["Orzo"]["quantity"] == "1.5 cup" and items["Chicken sausage"]["source"] == "mealie_recipe"

    # Reviewed: exactly the checked lines (here: re-checking the salt), merging into the same items.
    again = await browser.add_to_groceries(ORZO["slug"], ALEX, indices=[1, 2], day="2026-10-30", entry_type="dinner")
    assert again["added"] == ["Orzo", "Kosher salt"] and again["merged"] == 1
    assert again["meal_plan"] == {"date": "2026-10-30", "entry_type": "dinner", "recipe": "Chicken Sausage Orzo"}
    assert client.planned == [(date(2026, 10, 30), ORZO["id"], "dinner")] and changed == [True]
    items = {t["summary"]: t for t in await planner.db.list_tasks(groceries["id"], top_level_only=True)}
    assert items["Orzo"]["quantity"].startswith("3 cups")  # merged, with the metric equivalent


async def test_target_plan_must_be_a_visible_shopping_list(setup, planner) -> None:
    browser, *_ = setup
    chores = await plan_by_name(planner, "Daily Chores")
    with pytest.raises(PlannerError) as err:
        await browser.add_to_groceries(ORZO["slug"], ALEX, plan_id=chores["id"])
    assert err.value.code == "invalid"
    secret = await planner.create_plan({"name": "Sam's list", "archetype": "shopping_errands", "is_private": True}, SAM)
    with pytest.raises(PlannerError) as err:
        await browser.add_to_groceries(ORZO["slug"], ALEX, plan_id=secret["id"])
    assert err.value.code == "not_found"


async def test_plan_meal_validates_the_day_and_can_skip_ingredients(setup, planner) -> None:
    browser, client, *_ = setup
    only_plan = await browser.plan_meal(ORZO["slug"], "2026-11-02", ALEX, entry_type="lunch")
    assert only_plan["added"] == [] and client.planned[-1] == (date(2026, 11, 2), ORZO["id"], "lunch")
    with_items = await browser.plan_meal(ORZO["slug"], "2026-11-03", ALEX, add_ingredients=True)
    assert "Orzo" in with_items["added"] and with_items["meal_plan"]["date"] == "2026-11-03"
    for bad in ("someday", "2030-01-01"):
        with pytest.raises(PlannerError):
            await browser.plan_meal(ORZO["slug"], bad, ALEX)
    with pytest.raises(PlannerError):
        await browser.plan_meal(ORZO["slug"], "2026-11-02", ALEX, entry_type="brunch")
    assert len(client.planned) == 2  # nothing written for the rejected requests


async def test_image_cache_and_disabled_mealie(setup) -> None:
    browser, client, holder, *_ = setup
    first = await browser.image("aaaabbbb-0000")
    second = await browser.image("aaaabbbb-0000")
    assert first == second and first[1] == "image/webp" and client.image_calls == 1
    holder["client"] = None
    with pytest.raises(PlannerError) as err:
        await browser.recipes()
    assert err.value.code == "mealie_disabled"
