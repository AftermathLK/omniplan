"""Mealie recipe browsing and the grocery import filter (no Home Assistant imports).

Turning a recipe into a shopping list should not dump the whole spice rack onto
it. :func:`classify_ingredients` marks each ingredient line as wanted, a
*seasoning* (on the user-editable exclusion list, or labelled as a spice in
Mealie) or *pantry* (something the household's pantry says is in stock). The
card pre-unchecks the skipped lines and the "instant add" path drops them.

Matching works on normalized name keys: preparation words, descriptors such as
"ground" or "dried", parentheses and anything after the first comma are removed
and words are singularized, so "1 tsp freshly ground black pepper, to taste"
matches the "black pepper" entry. Seasonings must match a whole entry ("water"
never swallows "coconut water"); pantry items match the end of an ingredient
name, so a pantry "Flour" covers "all-purpose flour".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .units import name_key, parse_item

DEFAULT_SEASONINGS: tuple[str, ...] = (
    "salt",
    "kosher salt",
    "sea salt",
    "table salt",
    "black pepper",
    "pepper",
    "white pepper",
    "peppercorn",
    "salt and pepper",
    "garlic powder",
    "garlic salt",
    "onion powder",
    "paprika",
    "smoked paprika",
    "cumin",
    "oregano",
    "thyme",
    "rosemary",
    "parsley flakes",
    "dill weed",
    "cayenne",
    "cayenne pepper",
    "chili powder",
    "red pepper flakes",
    "crushed red pepper",
    "cinnamon",
    "nutmeg",
    "allspice",
    "cloves",
    "ginger powder",
    "turmeric",
    "curry powder",
    "italian seasoning",
    "taco seasoning",
    "cajun seasoning",
    "seasoned salt",
    "bay leaf",
    "vanilla extract",
    "oil",
    "cooking oil",
    "vegetable oil",
    "canola oil",
    "olive oil",
    "extra virgin olive oil",
    "cooking spray",
    "nonstick cooking spray",
    "water",
    "ice",
    "homemade veggie seasoning",
)

# Words that describe a spice rather than name it ("ground cumin" is cumin).
_DESCRIPTORS = frozenset(
    {
        "ground", "dried", "dry", "whole", "crushed", "cracked", "coarse", "fine", "flaked", "rubbed",
        "freshly", "fresh", "kosher", "pure", "pinch", "dash", "plus", "more", "extra", "about", "taste",
        "needed", "for", "serving", "frying", "greasing", "sprinkling", "garnish",
    }
)
# Kept for matching even though they look like descriptors ("kosher salt" is a list entry of its own).
_KEEP_WITH = {"kosher": {"salt"}, "extra": {"virgin"}}
# Trailing form words dropped as a fallback ("thyme leaves" -> "thyme").
_FORM_WORDS = frozenset({"leaf", "sprig", "stalk", "stick", "seed", "powder"})
_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|\band\b|\bor\b)\s*")
_SPICE_LABEL_RE = re.compile(r"spice|season|herb|condiment", re.IGNORECASE)
SKIP_SEASONING = "seasoning"
SKIP_PANTRY = "pantry"
MAX_SEASONINGS = 300
MAX_SEASONING_LENGTH = 60


def _words(text: str) -> list[str]:
    return name_key(text).split()


def match_key(text: str) -> str:
    """Normalized key for seasoning/pantry matching."""
    words = _words(text)
    kept = []
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if word in _DESCRIPTORS and following not in _KEEP_WITH.get(word, set()):
            continue
        kept.append(word)
    return " ".join(kept)


def clean_seasonings(entries: Iterable[Any]) -> list[str]:
    """Validate the user-editable exclusion list: trimmed, lowercase, unique, bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        text = " ".join(str(entry or "").split()).lower()[:MAX_SEASONING_LENGTH]
        key = match_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= MAX_SEASONINGS:
            break
    return out


def seasoning_keys(entries: Iterable[str]) -> frozenset[str]:
    return frozenset(key for key in (match_key(entry) for entry in entries) if key)


def _candidate_keys(name: str) -> list[str]:
    key = match_key(name)
    if not key:
        return []
    out = [key]
    words = key.split()
    if len(words) > 1 and words[-1] in _FORM_WORDS:
        out.append(" ".join(words[:-1]))
    return out


def is_seasoning(name: str, keys: frozenset[str], label: str | None = None) -> bool:
    """A seasoning by label, by list entry, or a combination such as "salt and pepper"."""
    if label and _SPICE_LABEL_RE.search(label):
        return True
    if not name:
        return False
    if any(candidate in keys for candidate in _candidate_keys(name)):
        return True
    head = re.split(r"[(,]", name.lower(), maxsplit=1)[0]
    parts = [part for part in _SPLIT_RE.split(head) if part.strip()]
    return len(parts) > 1 and all(any(c in keys for c in _candidate_keys(part)) for part in parts)


def pantry_match(name: str, pantry: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The in-stock pantry item that covers this ingredient, if any (longest name wins)."""
    words = match_key(name).split()
    if not words:
        return None
    best: tuple[int, Mapping[str, Any]] | None = None
    for item in pantry:
        item_words = match_key(str(item.get("name") or "")).split()
        if not item_words or len(item_words) > len(words):
            continue
        if words[-len(item_words):] == item_words and (best is None or len(item_words) > best[0]):
            best = (len(item_words), item)
    return best[1] if best else None


def ingredient_rows(recipe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per real ingredient line of a Mealie recipe (section titles are skipped).

    ``text`` is what goes on the shopping list ("2 cups all-purpose flour"),
    ``name`` the food used for matching, ``label`` Mealie's food label.
    """
    rows: list[dict[str, Any]] = []
    for index, ingredient in enumerate(recipe.get("recipeIngredient") or []):
        if not isinstance(ingredient, Mapping):
            continue
        food = ingredient.get("food") if isinstance(ingredient.get("food"), Mapping) else None
        unit = ingredient.get("unit") if isinstance(ingredient.get("unit"), Mapping) else None
        label_obj = (food or {}).get("label")
        label = label_obj.get("name") if isinstance(label_obj, Mapping) else None
        note = str(ingredient.get("note") or "").strip()
        food_name = str((food or {}).get("name") or "").strip()
        if food_name:
            quantity = ingredient.get("quantity")
            unit_name = str((unit or {}).get("abbreviation") or (unit or {}).get("name") or "").strip() if unit else ""
            qty_text = f"{float(quantity):g} {unit_name}".strip() if quantity else ""
            text = " ".join(part for part in (qty_text, food_name) if part)
            name = food_name
        else:
            text = note or str(ingredient.get("display") or ingredient.get("originalText") or "").strip()
            note = ""
            name = parse_item(text).name if text else ""
        if not text or not name:
            continue
        rows.append({"index": index, "text": text[:200], "name": name[:120], "note": note[:200] or None, "label": label})
    return rows


def classify_ingredients(
    rows: Sequence[Mapping[str, Any]],
    *,
    seasonings: Iterable[str],
    skip_seasonings: bool,
    pantry: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mark each row ``skip`` = None | "seasoning" | "pantry" (with the pantry item's name)."""
    keys = seasoning_keys(seasonings)
    in_stock = [item for item in pantry if item.get("stock_state") == "in_stock"]
    out = []
    for row in rows:
        skip = None
        covered = None
        if skip_seasonings and is_seasoning(row["name"], keys, row.get("label")):
            skip = SKIP_SEASONING
        else:
            match = pantry_match(row["name"], in_stock)
            if match is not None:
                skip, covered = SKIP_PANTRY, match.get("name")
        out.append({**row, "skip": skip, "pantry_item": covered})
    return out


# ---------------------------------------------------------------------------
# Recipe list
# ---------------------------------------------------------------------------
def _names(values: Any) -> list[str]:
    out = []
    for value in values or []:
        name = value.get("name") if isinstance(value, Mapping) else value
        if isinstance(name, str) and name.strip() and name.strip() not in out:
            out.append(name.strip()[:60])
    return out


def _short(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else None


def times_cooked(mealplans: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How often each recipe (by id and by slug) appeared on past meal plans."""
    counts: dict[str, int] = {}
    for entry in mealplans:
        recipe = entry.get("recipe") if isinstance(entry.get("recipe"), Mapping) else {}
        for key in {str(entry.get("recipeId") or recipe.get("id") or ""), str(recipe.get("slug") or "")} - {""}:
            counts[key] = counts.get(key, 0) + 1
    return counts


def recipe_summary(raw: Mapping[str, Any], cooked: Mapping[str, int], url: str | None = None) -> dict[str, Any] | None:
    """The fields the card needs from a Mealie recipe list entry (None if unusable)."""
    slug = str(raw.get("slug") or "").strip()
    name = _short(raw.get("name"), 200)
    if not slug or not name:
        return None
    recipe_id = str(raw.get("id") or "")
    return {
        "id": recipe_id or None,
        "slug": slug,
        "name": name,
        "description": _short(raw.get("description"), 240),
        "categories": _names(raw.get("recipeCategory")),
        "tags": _names(raw.get("tags")),
        "total_time": _short(raw.get("totalTime"), 40),
        "prep_time": _short(raw.get("prepTime"), 40),
        "servings": _short(raw.get("recipeYield") or raw.get("recipeServings"), 40),
        "rating": raw.get("rating") if isinstance(raw.get("rating"), (int, float)) else None,
        "date_added": _short(raw.get("dateAdded") or raw.get("createdAt"), 32),
        "last_made": _short(raw.get("lastMade"), 32),
        "times_cooked": max(cooked.get(recipe_id, 0), cooked.get(slug, 0)),
        "has_image": bool(raw.get("image")) and bool(recipe_id),
        "image_key": _short(raw.get("image"), 64),
        "url": url,
    }


def catalog(recipes: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    categories = sorted({c for r in recipes for c in r["categories"]}, key=str.lower)
    tags = sorted({t for r in recipes for t in r["tags"]}, key=str.lower)
    return {"categories": categories, "tags": tags}
