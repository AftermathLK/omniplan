"""Kitchen logic: Mealie prep-timeline rules and leftover naming (pure logic).

Prep rules come from three places, merged per recipe:

* **Recipe tags** shaped ``<action>-<hours>h`` (``thaw-36h``, ``brine-24h``).
* **Recipe extras / notes / description** containing ``taskdragon: thaw 36h, smoker 4h``
  or ``lead time: thaw 36h``.
* **Configured rules** (one per line): ``<tag or keyword> => <hours> | <template>``.
  A configured rule overrides the hours and wording for a matching tag.

Templates may use ``{recipe}``, ``{meat}`` and ``{action}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .const import AISLE_MEAT
from .migration import LEGACY_PREP_MARKERS
from .units import categorize

DEFAULT_TEMPLATES: dict[str, str] = {
    "thaw": "Move {meat} from freezer to fridge to thaw for {recipe}",
    "defrost": "Move {meat} from freezer to fridge to thaw for {recipe}",
    "brine": "Start the brine for {recipe}",
    "marinate": "Start marinating {meat} for {recipe}",
    "smoker": "Prep rub and fire up smoker for {recipe}",
    "smoke": "Prep rub and fire up smoker for {recipe}",
    "soak": "Soak the beans for {recipe}",
    "rise": "Start the dough for {recipe}",
    "proof": "Start the dough for {recipe}",
    "dough": "Start the dough for {recipe}",
    "dry-rub": "Apply dry rub to {meat} for {recipe}",
    "rub": "Apply rub to {meat} for {recipe}",
    "temper": "Take {meat} out of the fridge to temper for {recipe}",
    "preheat": "Preheat the oven for {recipe}",
    "slow-cooker": "Load the slow cooker for {recipe}",
    "crockpot": "Load the slow cooker for {recipe}",
}

_TAG_RE = re.compile(r"^(?P<action>[a-z][a-z\- ]*?)[\s_-]*(?P<hours>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hour|hours|m|min|mins|d|day|days)$")
_INLINE_RE = re.compile(
    r"(?P<action>[a-z][a-z\-]*(?:\s[a-z\-]+)?)\s*[:=]?\s*(?P<hours>\d+(?:\.\d+)?)\s*(?P<unit>h|hr|hrs|hour|hours|m|min|mins|d|day|days)\b",
    re.IGNORECASE,
)
_INLINE_MARKERS = re.compile(r"(?:taskdragon|omniplan|lead[\s-]?times?|prep[\s-]?ahead)\s*[:\-]\s*(?P<body>[^\n]+)", re.IGNORECASE)

MEAT_WORDS = (
    "brisket", "pork shoulder", "pork butt", "pork loin", "tenderloin", "chuck roast", "short rib",
    "ribs", "turkey", "whole chicken", "chicken thighs", "chicken breasts", "chicken wings", "chicken",
    "steak", "ribeye", "sirloin", "ground beef", "beef", "pork chops", "pork", "lamb", "salmon",
    "shrimp", "fish", "sausage", "venison", "ham", "duck",
)


@dataclass(frozen=True)
class PrepRule:
    """A configured or discovered lead-time rule."""

    key: str  # stable identifier (e.g. "thaw")
    hours: float
    template: str | None = None
    match: str | None = None  # tag/keyword this rule matches, for configured rules


@dataclass(frozen=True)
class PrepStep:
    """A concrete prep task to create for a scheduled meal."""

    key: str
    hours: float
    summary: str
    due: datetime


def _to_hours(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit.startswith("m"):
        return value / 60.0
    if unit.startswith("d"):
        return value * 24.0
    return value


def parse_configured_rules(text: str | None) -> list[PrepRule]:
    """Parse ``tag => hours | template`` lines from the options flow."""
    rules: list[PrepRule] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        match_part, _, rest = line.partition("=>")
        hours_part, _, template = rest.partition("|")
        try:
            hours = float(hours_part.strip().rstrip("hH"))
        except ValueError:
            continue
        match = match_part.strip().lower()
        if not match:
            continue
        key = _TAG_RE.match(match)
        rule_key = key.group("action").strip().replace(" ", "-") if key else re.sub(r"[^a-z0-9]+", "-", match).strip("-")
        rules.append(PrepRule(key=rule_key, hours=hours, template=template.strip() or None, match=match))
    return rules


def _tag_names(recipe: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for tag in recipe.get("tags") or []:
        if isinstance(tag, Mapping):
            for field in ("slug", "name"):
                if tag.get(field):
                    names.append(str(tag[field]).strip().lower())
        elif tag:
            names.append(str(tag).strip().lower())
    for category in recipe.get("recipeCategory") or []:
        if isinstance(category, Mapping) and category.get("slug"):
            names.append(str(category["slug"]).strip().lower())
    return list(dict.fromkeys(names))


def _recipe_texts(recipe: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    if recipe.get("description"):
        texts.append(str(recipe["description"]))
    for note in recipe.get("notes") or []:
        if isinstance(note, Mapping):
            texts.append(f"{note.get('title') or ''}: {note.get('text') or ''}")
    extras = recipe.get("extras") or {}
    if isinstance(extras, Mapping):
        for key, value in extras.items():
            texts.append(f"{key}: {value}")
    return texts


def discover_rules(recipe: Mapping[str, Any], configured: Sequence[PrepRule]) -> list[PrepRule]:
    """Return the prep rules that apply to a recipe (one per action key, longest lead first)."""
    found: dict[str, PrepRule] = {}
    tags = _tag_names(recipe)
    texts = _recipe_texts(recipe)
    haystack = " ".join([str(recipe.get("name") or ""), *texts]).lower()

    # 1. Configured rules match a tag exactly; keyword rules ("brisket") may also match the text.
    for rule in configured:
        if not rule.match:
            continue
        keyword_rule = _TAG_RE.match(rule.match) is None
        if rule.match in tags or (keyword_rule and re.search(rf"\b{re.escape(rule.match)}\b", haystack)):
            found.setdefault(rule.key, rule)

    # 2. Any other tag shaped like "<action>-<N>h".
    for tag in tags:
        parsed = _TAG_RE.match(tag)
        if parsed:
            action = parsed.group("action").strip().replace(" ", "-")
            hours = _to_hours(float(parsed.group("hours")), parsed.group("unit"))
            found.setdefault(action, PrepRule(key=action, hours=hours))

    # 3. Recipe-specific lead times written in notes/extras win over generic hours.
    for text in texts:
        key, _, value = text.partition(":")
        bodies = [m.group("body") for m in _INLINE_MARKERS.finditer(text)]
        if key.strip().lower() in ("taskdragon", "taskdragon_prep", *LEGACY_PREP_MARKERS, "prep", "lead_time", "lead time"):
            bodies.append(value)
        for body in bodies:
            for inline in _INLINE_RE.finditer(body):
                action = inline.group("action").strip().lower().replace(" ", "-")
                hours = _to_hours(float(inline.group("hours")), inline.group("unit"))
                existing = found.get(action)
                found[action] = PrepRule(key=action, hours=hours, template=existing.template if existing else None)

    # 4. Configured rules supply the wording for actions discovered any other way.
    templates = {rule.key: rule.template for rule in configured if rule.template}
    merged = [
        PrepRule(key=key, hours=rule.hours, template=rule.template or templates.get(key), match=rule.match)
        for key, rule in found.items()
    ]
    return sorted(merged, key=lambda r: -r.hours)


def find_meat(recipe: Mapping[str, Any]) -> str | None:
    """Return the main protein of a recipe from its ingredients, if any."""
    candidates: list[str] = []
    for ingredient in recipe.get("recipeIngredient") or []:
        if not isinstance(ingredient, Mapping):
            continue
        food = ingredient.get("food")
        name = (food or {}).get("name") if isinstance(food, Mapping) else None
        text = str(name or ingredient.get("note") or ingredient.get("display") or ingredient.get("originalText") or "")
        if text:
            candidates.append(text.lower())
    candidates.append(str(recipe.get("name") or "").lower())
    for word in MEAT_WORDS:
        for text in candidates:
            if re.search(rf"\b{re.escape(word)}\b", text):
                return word
    for text in candidates[:-1]:
        if categorize(text) == AISLE_MEAT:
            return text
    return None


def render_template(template: str, *, recipe: str, meat: str | None, action: str) -> str:
    """Fill a prep template; unknown placeholders are left untouched."""
    values = {"recipe": recipe, "meat": meat or "the meat", "action": action.replace("-", " ")}
    result = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)
    return result[0].upper() + result[1:] if result else result


def build_prep_steps(
    recipe: Mapping[str, Any],
    meal_time: datetime,
    configured: Sequence[PrepRule],
) -> list[PrepStep]:
    """Turn a scheduled recipe into dated prep steps (T-minus N hours)."""
    name = str(recipe.get("name") or recipe.get("slug") or "dinner")
    meat = find_meat(recipe)
    steps: list[PrepStep] = []
    for rule in discover_rules(recipe, configured):
        if rule.hours <= 0:
            continue
        template = rule.template or DEFAULT_TEMPLATES.get(rule.key) or "{action} for {recipe}"
        steps.append(
            PrepStep(
                key=rule.key,
                hours=rule.hours,
                summary=render_template(template, recipe=name, meat=meat, action=rule.key),
                due=meal_time - timedelta(hours=rule.hours),
            )
        )
    return steps


def leftover_summary(recipe_name: str, cooked_on: date) -> str:
    """``Eat or freeze leftover Chili (Cooked Tuesday)``."""
    return f"Eat or freeze leftover {recipe_name} (Cooked {cooked_on.strftime('%A')})"


def ingredient_lines(recipe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten Mealie ``recipeIngredient`` entries into ``{name, quantity, note}`` dicts."""
    lines: list[dict[str, Any]] = []
    for ingredient in recipe.get("recipeIngredient") or []:
        if not isinstance(ingredient, Mapping):
            continue
        food = ingredient.get("food") if isinstance(ingredient.get("food"), Mapping) else None
        unit = ingredient.get("unit") if isinstance(ingredient.get("unit"), Mapping) else None
        name = (food or {}).get("name") or ""
        quantity = ingredient.get("quantity")
        unit_name = ((unit or {}).get("abbreviation") or (unit or {}).get("name") or "") if unit else ""
        note = str(ingredient.get("note") or "").strip()
        if not name:
            # Unparsed ingredient: the whole line lives in note/display.
            text = note or str(ingredient.get("display") or ingredient.get("originalText") or "").strip()
            if not text:
                continue
            lines.append({"name": text, "quantity": None, "note": None, "text": text})
            continue
        qty_text = None
        if quantity:
            qty_value = float(quantity)
            qty_text = (f"{qty_value:g} {unit_name}").strip()
        text = " ".join(part for part in (qty_text, name) if part)
        lines.append({"name": name, "quantity": qty_text, "note": note or None, "text": text})
    return lines


def ingredients_markdown(lines: Iterable[Mapping[str, Any]]) -> str:
    """Render an ingredient list for a task description."""
    out = []
    for line in lines:
        qty = f"{line['quantity']} " if line.get("quantity") else ""
        note = f" — {line['note']}" if line.get("note") else ""
        out.append(f"- {qty}{line['name']}{note}")
    return "\n".join(out)
