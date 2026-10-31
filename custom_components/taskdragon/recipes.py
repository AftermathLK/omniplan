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


def scaled_quantity(value: float) -> str:
    """A scaled ingredient amount for a shopping-list line ("1.5", "3", "0.333")."""
    return f"{round(float(value), 3):g}"


def ingredient_rows(recipe: Mapping[str, Any], scale: float = 1.0) -> list[dict[str, Any]]:
    """One row per real ingredient line of a Mealie recipe (section titles are skipped).

    ``text`` is what goes on the shopping list ("2 cups all-purpose flour"),
    ``name`` the food used for matching, ``label`` Mealie's food label. With
    ``scale`` (a batch multiplier) parsed amounts are multiplied; free-text
    lines stay as written.
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
            qty_text = f"{scaled_quantity(float(quantity) * scale)} {unit_name}".strip() if quantity else ""
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


# ---------------------------------------------------------------------------
# Recipe detail (the cooking view)
# ---------------------------------------------------------------------------
_FRACTIONS = {"½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875}
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "forty-five": 45, "forty five": 45, "sixty": 60, "ninety": 90,
}
_NUMBER = (
    r"(?:\d+\s+\d+/\d+|\d+(?:[.,]\d+)?\s*[½⅓⅔¼¾⅛⅜⅝⅞]?|\d+/\d+|[½⅓⅔¼¾⅛⅜⅝⅞]"
    r"|(?:forty[- ]five|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|sixty|ninety|an?)\b)"
)
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}
_DURATION_RE = re.compile(
    r"(?<![\w./])(?P<a>" + _NUMBER + r")(?:\s*(?:-|–|—|to|or)\s*(?P<b>" + _NUMBER + r"))?\s*"
    r"(?P<unit>hours?|hrs?|minutes?|mins?|seconds?|secs?)\b"
    r"(?:,?\s*(?:and\s+)?(?P<extra>\d+)\s*(?:minutes?|mins?)\b)?",
    re.IGNORECASE,
)
_COMPACT_RE = re.compile(r"(?<![\w.])(?P<h>\d{1,2})h(?:\s?(?P<m>\d{1,2})m(?:in)?)?\b", re.IGNORECASE)
_HALF_HOUR_RE = re.compile(r"\b(?:(?P<whole>an?|one)\s+hour\s+and\s+a\s+half|half\s+an?\s+hour)\b", re.IGNORECASE)
MAX_TIMER_SECONDS = 48 * 3600
MAX_TIMERS_PER_STEP = 6
NUTRITION_LABELS = (
    ("calories", "Calories"),
    ("fatContent", "Fat"),
    ("saturatedFatContent", "Saturated fat"),
    ("transFatContent", "Trans fat"),
    ("unsaturatedFatContent", "Unsaturated fat"),
    ("cholesterolContent", "Cholesterol"),
    ("sodiumContent", "Sodium"),
    ("carbohydrateContent", "Carbohydrates"),
    ("fiberContent", "Fiber"),
    ("sugarContent", "Sugar"),
    ("proteinContent", "Protein"),
)


def _amount(text: str | None) -> float | None:
    if not text:
        return None
    value = text.strip().lower()
    if value in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[value])
    if value in _FRACTIONS:
        return _FRACTIONS[value]
    match = re.match(r"^(\d+)\s+(\d+)/(\d+)$", value)
    if match:
        return int(match[1]) + int(match[2]) / max(int(match[3]), 1)
    match = re.match(r"^(\d+)/(\d+)$", value)
    if match:
        return int(match[1]) / max(int(match[2]), 1)
    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*([½⅓⅔¼¾⅛⅜⅝⅞])?$", value)
    if match:
        return float(match[1].replace(",", ".")) + (_FRACTIONS[match[2]] if match[2] else 0.0)
    return None


def timer_label(seconds: int, max_seconds: int | None = None) -> str:
    """Short timer text: "45s", "15m", "1h 30m", "25–30m"."""

    def one(total: int) -> str:
        hours, rest = divmod(int(total), 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        if minutes:
            return f"{minutes}m {secs}s" if secs else f"{minutes}m"
        return f"{secs}s"

    if max_seconds and max_seconds > seconds:
        low, high = one(seconds), one(max_seconds)
        if low.endswith("m") and high.endswith("m") and "h" not in low + high:
            return f"{low[:-1]}–{high}"
        return f"{low}–{high}"
    return one(seconds)


def parse_durations(text: str) -> list[dict[str, Any]]:
    """Cooking times mentioned in an instruction ("simmer for 15 minutes", "bake 25-30 mins", "1 hr 15 min").

    Ranges start at the lower bound (``max_seconds`` holds the upper one).
    """
    found: list[tuple[int, dict[str, Any]]] = []
    taken: list[tuple[int, int]] = []

    def add(start: int, end: int, seconds: float, max_seconds: float | None, raw: str) -> None:
        if any(start < b and end > a for a, b in taken):
            return
        low = int(round(seconds))
        high = int(round(max_seconds)) if max_seconds and max_seconds > seconds else None
        if not 5 <= low <= MAX_TIMER_SECONDS or (high and high > MAX_TIMER_SECONDS):
            return
        taken.append((start, end))
        found.append((start, {"seconds": low, "max_seconds": high, "label": timer_label(low, high), "text": raw.strip()[:60]}))

    for match in _HALF_HOUR_RE.finditer(text or ""):
        add(match.start(), match.end(), 5400 if match["whole"] else 1800, None, match[0])
    for match in _DURATION_RE.finditer(text or ""):
        unit = _UNIT_SECONDS[match["unit"][0].lower()]
        first, second = _amount(match["a"]), _amount(match["b"])
        if first is None:
            continue
        extra = int(match["extra"]) * 60 if match["extra"] and unit == 3600 else 0
        add(match.start(), match.end(), first * unit + (0 if second else extra), second * unit + extra if second else None, match[0])
    for match in _COMPACT_RE.finditer(text or ""):
        add(match.start(), match.end(), int(match["h"]) * 3600 + int(match["m"] or 0) * 60, None, match[0])
    found.sort(key=lambda pair: pair[0])
    return [timer for _, timer in found[:MAX_TIMERS_PER_STEP]]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _servings(raw: Mapping[str, Any]) -> float | None:
    for key in ("recipeServings", "recipeYieldQuantity"):
        number = _number(raw.get(key))
        if number and number > 0:
            return number
    match = re.match(r"^\s*(\d+(?:[.,]\d+)?)", str(raw.get("recipeYield") or ""))
    return float(match[1].replace(",", ".")) if match else None


def _http_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:2048] if re.match(r"^https?://[^\s]+$", text, re.IGNORECASE) else None


def _unit(unit: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not unit:
        return None
    name = _short(unit.get("name"), 40)
    if not name:
        return None
    return {
        "name": name,
        "plural": _short(unit.get("pluralName"), 40),
        "abbreviation": _short(unit.get("abbreviation"), 20),
        "use_abbreviation": bool(unit.get("useAbbreviation")),
        "fraction": unit.get("fraction") is not False,
    }


def recipe_detail(
    raw: Mapping[str, Any],
    *,
    seasonings: Iterable[str],
    skip_seasonings: bool,
    pantry: Sequence[Mapping[str, Any]],
    url: str | None = None,
) -> dict[str, Any]:
    """Everything the cooking view shows, with pantry/seasoning flags on each ingredient.

    Ingredient ``index`` values match :func:`ingredient_rows`, so the card can
    send them straight to the grocery import. Section headers come through as
    ``{"type": "section"}`` entries in reading order.
    """
    keys = seasoning_keys(seasonings)
    in_stock = [item for item in pantry if item.get("stock_state") == "in_stock"]
    settings = raw.get("settings") if isinstance(raw.get("settings"), Mapping) else {}
    disable_amount = bool(settings.get("disableAmount"))
    rows = {row["index"]: row for row in ingredient_rows(raw)}
    ingredients: list[dict[str, Any]] = []
    counts = {"ingredients": 0, "pantry": 0, "seasonings": 0, "missing": 0}
    for index, ingredient in enumerate(raw.get("recipeIngredient") or []):
        if not isinstance(ingredient, Mapping):
            continue
        title = _short(ingredient.get("title"), 120)
        if title:
            ingredients.append({"type": "section", "title": title})
        row = rows.get(index)
        if row is None:
            continue
        food = ingredient.get("food") if isinstance(ingredient.get("food"), Mapping) else None
        unit = ingredient.get("unit") if isinstance(ingredient.get("unit"), Mapping) else None
        quantity = _number(ingredient.get("quantity"))
        food_name = _short((food or {}).get("name"), 120)
        scalable = bool(food_name) and bool(quantity) and quantity > 0 and not disable_amount
        seasoning = is_seasoning(row["name"], keys, row.get("label"))
        covered = pantry_match(row["name"], in_stock)
        skip = SKIP_SEASONING if skip_seasonings and seasoning else SKIP_PANTRY if covered is not None else None
        counts["ingredients"] += 1
        counts["pantry"] += covered is not None
        counts["seasonings"] += seasoning
        counts["missing"] += skip is None
        ingredients.append(
            {
                "type": "ingredient",
                "index": index,
                "display": _short(ingredient.get("display"), 240) or row["text"],
                "text": row["text"],
                "quantity": round(quantity, 4) if scalable else None,
                "unit": _unit(unit) if scalable else None,
                "food": food_name,
                "food_plural": _short((food or {}).get("pluralName"), 120) if food else None,
                "note": _short(ingredient.get("note"), 200) if food_name else None,
                "is_excluded_spice": seasoning,
                "in_pantry": covered is not None,
                "pantry_item": covered.get("name") if covered is not None else None,
                "skip": skip,
            }
        )
    steps: list[dict[str, Any]] = []
    for step in raw.get("recipeInstructions") or []:
        if isinstance(step, Mapping):
            text = str(step.get("text") or "").strip()
            title = _short(step.get("title") or step.get("summary"), 200)
        else:
            text, title = str(step or "").strip(), None
        if not text and not title:
            continue
        text = text[:4000]
        steps.append({"index": len(steps), "title": title, "text": text, "timers": parse_durations(text)})
    notes = []
    for note in raw.get("notes") or []:
        if isinstance(note, Mapping) and (note.get("title") or note.get("text")):
            notes.append({"title": _short(note.get("title"), 200), "text": str(note.get("text") or "").strip()[:4000] or None})
    nutrition_raw = raw.get("nutrition") if isinstance(raw.get("nutrition"), Mapping) else {}
    nutrition = [
        {"key": key, "label": label, "value": _short(nutrition_raw.get(key), 40)}
        for key, label in NUTRITION_LABELS
        if _short(nutrition_raw.get(key), 40)
    ]
    recipe_id = str(raw.get("id") or "") or None
    return {
        "id": recipe_id,
        "slug": str(raw.get("slug") or ""),
        "name": _short(raw.get("name"), 200) or str(raw.get("slug") or "Recipe"),
        "description": str(raw.get("description") or "").strip()[:2000] or None,
        "has_image": bool(raw.get("image")) and bool(recipe_id),
        "categories": _names(raw.get("recipeCategory")),
        "tags": _names(raw.get("tags")),
        "prep_time": _short(raw.get("prepTime"), 40),
        "cook_time": _short(raw.get("performTime") or raw.get("cookTime"), 40),
        "total_time": _short(raw.get("totalTime"), 40),
        "yield": _short(raw.get("recipeYield"), 60),
        "servings": _servings(raw),
        "rating": raw.get("rating") if isinstance(raw.get("rating"), (int, float)) else None,
        "url": url,
        "source_url": _http_url(raw.get("orgURL")),
        "skip_seasonings": bool(skip_seasonings),
        "ingredients": ingredients,
        "instructions": steps,
        "notes": notes,
        "nutrition": nutrition,
        "counts": counts,
    }
