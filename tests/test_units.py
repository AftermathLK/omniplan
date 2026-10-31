"""Unit normalization, merging and aisle categorization."""

from __future__ import annotations

import pytest

from taskdragon.units import (
    COUNT,
    MASS,
    VOLUME,
    categorize,
    categorize_parsed,
    combine_quantity_text,
    format_quantity,
    merge_items,
    merge_quantities,
    name_key,
    parse_item,
    parse_number,
    parse_quantity_text,
)


@pytest.mark.parametrize(
    ("text", "value"),
    [("1/2", 0.5), ("1 1/2", 1.5), ("0.25", 0.25), ("½", 0.5), ("1½", 1.5), ("3", 3.0), ("x", None)],
)
def test_parse_number(text: str, value: float | None) -> None:
    assert parse_number(text) == value


def test_parse_item_with_weight() -> None:
    item = parse_item("2 lbs ground beef")
    assert item.name == "Ground beef"
    assert item.quantity is not None
    assert item.quantity.dimension == MASS
    assert item.quantity.amount == pytest.approx(907.18, rel=1e-3)


def test_parse_item_without_quantity() -> None:
    item = parse_item("garlic powder")
    assert item.name == "Garlic powder"
    assert item.quantity is None


@pytest.mark.parametrize(
    ("text", "name", "dimension", "unit"),
    [
        ("0.5 cup flour", "Flour", VOLUME, "cup"),
        ("120 ml milk", "Milk", VOLUME, "ml"),
        ("2 T butter", "Butter", VOLUME, "tbsp"),
        ("1 t salt", "Salt", VOLUME, "tsp"),
        ("1 fl oz vanilla", "Vanilla", VOLUME, "floz"),
        ("8 oz cream cheese", "Cream cheese", MASS, "oz"),
        ("2 cans diced tomatoes", "Diced tomatoes", COUNT, "can"),
        ("3 large eggs", "Large eggs", COUNT, "each"),
        ("milk x2", "Milk", COUNT, "each"),
        ("2x 2x4s", "2x4s", COUNT, "each"),
    ],
)
def test_parse_item_units(text: str, name: str, dimension: str, unit: str) -> None:
    item = parse_item(text)
    assert item.name == name
    assert item.quantity is not None
    assert item.quantity.dimension == dimension
    assert item.quantity.unit_key == unit


def test_parse_item_keeps_model_numbers_in_name() -> None:
    assert parse_item("Vitamin D3").quantity is None
    assert parse_item("Pick up 2x4s").name == "Pick up 2x4s"


def test_parse_item_ranges_buy_the_upper_end() -> None:
    item = parse_item("2-3 cloves garlic")
    assert item.quantity is not None and item.quantity.amount == 3
    assert format_quantity(item.quantity) == "3 cloves"


def test_merge_half_cup_and_120_ml_is_one_cup() -> None:
    """The headline example: 0.5 cup + 120 ml -> 1 cup / 240 ml."""
    [milk] = merge_items(["0.5 cup milk", "120 ml milk"])
    assert milk.display_quantity == "1 cup / 240 ml"
    assert milk.category == "Dairy"


def test_merge_tablespoons_and_quarter_cup() -> None:
    [oil] = merge_items(["2 tbsp olive oil", "0.25 cup olive oil"])
    # 2 tbsp + 4 tbsp
    assert oil.display_quantity.startswith("6 tbsp")


def test_merge_metric_only_stays_metric() -> None:
    [stock] = merge_items(["500 ml chicken stock", "1 l chicken stock"])
    assert stock.display_quantity == "1.5 L"


def test_merge_weights_across_systems() -> None:
    [beef] = merge_items(["1 lb ground beef", "500 g ground beef"])
    assert beef.display_quantity.startswith("2.1 lb")
    assert beef.category == "Meat"


def test_incompatible_units_are_kept_side_by_side() -> None:
    [garlic] = merge_items(["2 cloves garlic", "1 tbsp minced garlic", "garlic"])
    assert garlic.display_quantity == "2 cloves + 1 tbsp / 15 ml"


def test_plurals_and_prep_words_deduplicate() -> None:
    merged = merge_items(["Tomatoes", "2 tomatoes", "1 diced tomato", "fresh basil", "basil"])
    assert [item.key for item in merged] == ["tomato", "basil"]
    assert merged[0].display_quantity == "3"


def test_merge_quantities_marks_us_system_when_mixed() -> None:
    a = parse_item("1 cup water").quantity
    b = parse_item("250 ml water").quantity
    assert a and b
    [total] = merge_quantities([a, b])
    assert total.system == "us"
    assert total.amount == pytest.approx(486.6, rel=1e-3)


@pytest.mark.parametrize(
    ("existing", "addition", "expected"),
    [
        ("0.5 cup", "120 ml", "1 cup / 240 ml"),
        ("1 cup / 240 ml", "2 tbsp", "1 1/8 cups / 265 ml"),
        ("2 lb", "8 oz", "2 1/2 lb / 1.13 kg"),
        ("3", "2", "5"),
        ("3", "2 cans", "3 + 2 cans"),
        (None, "1 dozen", "12"),
        (None, None, None),
    ],
)
def test_combine_quantity_text(existing: str | None, addition: str | None, expected: str | None) -> None:
    assert combine_quantity_text(existing, addition) == expected


def test_parse_quantity_text_reads_our_own_format() -> None:
    qty = parse_quantity_text("1 cup / 240 ml")
    assert qty is not None and qty.unit_key == "cup"
    assert parse_item("1 cup / 240 ml Milk").name == "Milk"


@pytest.mark.parametrize(
    ("name", "aisle"),
    [
        ("Garlic powder", "Spices/Pantry"),
        ("Garlic", "Produce"),
        ("Ground beef", "Meat"),
        ("Chicken broth", "Spices/Pantry"),
        ("Sour cream", "Dairy"),
        ("Peanut butter", "Spices/Pantry"),
        ("Butter", "Dairy"),
        ("Bell pepper", "Produce"),
        ("Black pepper", "Spices/Pantry"),
        ("Eggplant", "Produce"),
        ("2x4s", "Hardware"),
        ("Deck screws", "Hardware"),
        ("Furnace filter", "Hardware"),
        ("Paper towels", "Other"),
    ],
)
def test_categorize(name: str, aisle: str) -> None:
    assert categorize(name) == aisle


def test_packaging_unit_moves_produce_to_pantry() -> None:
    assert categorize_parsed(parse_item("2 cans diced tomatoes")) == "Spices/Pantry"
    assert categorize_parsed(parse_item("3 tomatoes")) == "Produce"


def test_name_key() -> None:
    assert name_key("Fresh Basil Leaves (chopped)") == name_key("basil leaf")
    assert name_key("Berries") == "berry"
    assert name_key("Molasses") == "molasses"
