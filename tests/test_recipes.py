"""Recipe import filter: seasonings, pantry cross-check, recipe summaries."""

from __future__ import annotations

import pytest

from omniplan.recipes import (
    DEFAULT_SEASONINGS,
    catalog,
    classify_ingredients,
    clean_seasonings,
    ingredient_rows,
    is_seasoning,
    pantry_match,
    recipe_summary,
    seasoning_keys,
    times_cooked,
)

KEYS = seasoning_keys(DEFAULT_SEASONINGS)


@pytest.mark.parametrize(
    "name",
    [
        "Kosher salt",
        "salt",
        "freshly ground black pepper, to taste",
        "Ground cumin",
        "dried oregano",
        "Smoked Paprika",
        "bay leaves",
        "dried thyme leaves",
        "Extra-virgin olive oil",
        "salt and pepper",
        "Salt & freshly ground pepper",
        "red pepper flakes (optional)",
        "Nonstick cooking spray",
        "oil for frying",
        "Homemade Veggie Seasoning",
        "water",
    ],
)
def test_seasonings_are_recognized(name) -> None:
    assert is_seasoning(name, KEYS)


@pytest.mark.parametrize(
    "name",
    [
        "coconut water",
        "water chestnuts",
        "salted butter",
        "bell pepper",
        "red bell peppers",
        "garlic",
        "4 garlic cloves",
        "chicken sausage",
        "orzo",
        "fresh basil",
        "ice cream",
        "salt pork",
    ],
)
def test_groceries_are_not_mistaken_for_seasonings(name) -> None:
    assert not is_seasoning(name, KEYS)


def test_mealie_food_label_marks_a_spice() -> None:
    assert is_seasoning("Za'atar", KEYS, label="Spices & Seasonings")
    assert not is_seasoning("Za'atar", KEYS, label="Produce")


def test_pantry_matches_the_end_of_an_ingredient_name() -> None:
    pantry = [{"name": "Flour"}, {"name": "Rice"}, {"name": "Chicken broth"}]
    assert pantry_match("all-purpose flour", pantry)["name"] == "Flour"
    assert pantry_match("Long grain white rice", pantry)["name"] == "Rice"
    assert pantry_match("low-sodium chicken broth", pantry)["name"] == "Chicken broth"
    assert pantry_match("rice vinegar", pantry) is None
    assert pantry_match("broth", pantry) is None  # the pantry item is more specific than the ingredient


def _recipe() -> dict:
    return {
        "name": "Chicken Sausage Orzo",
        "recipeIngredient": [
            {"title": "Main", "quantity": 12, "unit": {"name": "ounce", "abbreviation": "oz"}, "food": {"name": "chicken sausage"}, "note": "sliced"},
            {"quantity": 1.5, "unit": {"name": "cup"}, "food": {"name": "orzo"}},
            {"quantity": 1, "unit": {"name": "teaspoon", "abbreviation": "tsp"}, "food": {"name": "kosher salt"}},
            {"quantity": 0, "unit": None, "food": None, "note": "Freshly ground black pepper, to taste"},
            {"quantity": 2, "unit": {"name": "cup"}, "food": {"name": "chicken broth"}},
            {"quantity": 1, "unit": {"name": "tablespoon"}, "food": {"name": "Za'atar", "label": {"name": "Spices"}}},
            {"note": "", "display": ""},  # empty line from the Mealie editor
            {"quantity": 0, "food": None, "note": "2 cups all-purpose flour"},
            "not a dict",
        ],
    }


def test_ingredient_rows_flatten_parsed_and_free_text_lines() -> None:
    rows = ingredient_rows(_recipe())
    assert [r["index"] for r in rows] == [0, 1, 2, 3, 4, 5, 7]
    assert rows[0] == {"index": 0, "text": "12 oz chicken sausage", "name": "chicken sausage", "note": "sliced", "label": None}
    assert rows[1]["text"] == "1.5 cup orzo"
    assert rows[3]["text"] == "Freshly ground black pepper, to taste"
    assert rows[5]["label"] == "Spices"
    assert rows[6]["name"] == "All-purpose flour" and rows[6]["text"] == "2 cups all-purpose flour"


def test_classify_skips_seasonings_and_what_the_pantry_has() -> None:
    pantry = [
        {"name": "Chicken broth", "stock_state": "in_stock"},
        {"name": "Flour", "stock_state": "out"},  # out of stock: buy it
    ]
    rows = classify_ingredients(ingredient_rows(_recipe()), seasonings=DEFAULT_SEASONINGS, skip_seasonings=True, pantry=pantry)
    skips = {r["index"]: (r["skip"], r["pantry_item"]) for r in rows}
    assert skips[0] == (None, None)  # chicken sausage
    assert skips[1] == (None, None)  # orzo
    assert skips[2] == ("seasoning", None)  # kosher salt
    assert skips[3] == ("seasoning", None)  # freshly ground black pepper, to taste
    assert skips[5] == ("seasoning", None)  # labelled a spice in Mealie
    assert skips[4] == ("pantry", "Chicken broth")
    assert skips[7] == (None, None)  # flour is out of stock

    keep_spices = classify_ingredients(ingredient_rows(_recipe()), seasonings=DEFAULT_SEASONINGS, skip_seasonings=False, pantry=pantry)
    assert [r["name"] for r in keep_spices if r["skip"]] == ["chicken broth"]


def test_clean_seasonings_trims_dedupes_and_bounds() -> None:
    cleaned = clean_seasonings(["  Smoked  Paprika ", "smoked paprika", "", None, "Ground Cumin", "cumin", "x" * 100])
    assert cleaned == ["smoked paprika", "ground cumin", "x" * 60]
    assert len(clean_seasonings(f"spice {i}" for i in range(1000))) == 300


def test_recipe_summary_catalog_and_times_cooked() -> None:
    plans = [
        {"recipeId": "r1", "recipe": {"id": "r1", "slug": "orzo"}},
        {"recipe": {"slug": "orzo"}},
        {"recipeId": "r2"},
        {"title": "Leftovers"},
    ]
    cooked = times_cooked(plans)
    assert cooked == {"r1": 1, "orzo": 2, "r2": 1}
    raw = {
        "id": "r1",
        "slug": "orzo",
        "name": "  Chicken   Sausage Orzo ",
        "description": "x" * 500,
        "recipeCategory": [{"name": "Chicken"}, {"name": "Pasta"}, {"name": "Chicken"}],
        "tags": [{"name": "Toddler/Egg-Free"}],
        "totalTime": "35 minutes",
        "image": "Ab1c",
        "dateAdded": "2026-03-01",
    }
    summary = recipe_summary(raw, cooked, url="http://mealie.local/g/home/r/orzo")
    assert summary["name"] == "Chicken Sausage Orzo" and len(summary["description"]) == 240
    assert summary["categories"] == ["Chicken", "Pasta"] and summary["times_cooked"] == 2
    assert summary["has_image"] is True and summary["image_key"] == "Ab1c"
    assert recipe_summary({"slug": "", "name": "x"}, {}) is None
    assert catalog([summary]) == {"categories": ["Chicken", "Pasta"], "tags": ["Toddler/Egg-Free"]}
