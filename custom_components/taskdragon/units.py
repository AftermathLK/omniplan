"""Ingredient quantity parsing, unit normalization, merging and aisle categorization.

Volumes are normalized to millilitres, weights to grams and counts to "each" (or
a named count unit such as "clove" or "can"). Compatible quantities are summed
and rendered in the most natural unit, with a metric equivalent when US units
were involved, e.g. ``0.5 cup + 120 ml -> "1 cup / 240 ml"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

from .const import (
    AISLE_DAIRY,
    AISLE_HARDWARE,
    AISLE_MEAT,
    AISLE_OTHER,
    AISLE_PANTRY,
    AISLE_PRODUCE,
)

VOLUME = "volume"
MASS = "mass"
COUNT = "count"

US = "us"
METRIC = "metric"


@dataclass(frozen=True)
class UnitDef:
    """A known unit."""

    key: str
    dimension: str
    factor: float  # base units (ml / g / 1) per one of this unit
    system: str
    singular: str
    plural: str


_UNITS: dict[str, UnitDef] = {}
_ALIASES: dict[str, str] = {}


def _unit(key: str, dimension: str, factor: float, system: str, singular: str, plural: str, *aliases: str) -> None:
    _UNITS[key] = UnitDef(key, dimension, factor, system, singular, plural)
    for alias in (key, singular, plural, *aliases):
        _ALIASES[alias.lower()] = key


# Volume (base: millilitre)
_unit("tsp", VOLUME, 4.92892, US, "tsp", "tsp", "teaspoon", "teaspoons", "t", "tsps")
_unit("tbsp", VOLUME, 14.7868, US, "tbsp", "tbsp", "tablespoon", "tablespoons", "tbs", "tbl", "tbsps", "T")
_unit("floz", VOLUME, 29.5735, US, "fl oz", "fl oz", "fluid ounce", "fluid ounces", "fl. oz", "fl.oz", "fl-oz")
_unit("cup", VOLUME, 236.588, US, "cup", "cups", "c")
_unit("pint", VOLUME, 473.176, US, "pint", "pints", "pt", "pts")
_unit("quart", VOLUME, 946.353, US, "quart", "quarts", "qt", "qts")
_unit("gallon", VOLUME, 3785.41, US, "gallon", "gallons", "gal", "gals")
_unit("ml", VOLUME, 1.0, METRIC, "ml", "ml", "millilitre", "milliliter", "millilitres", "milliliters", "mls", "mL")
_unit("cl", VOLUME, 10.0, METRIC, "cl", "cl", "centilitre", "centiliter")
_unit("dl", VOLUME, 100.0, METRIC, "dl", "dl", "decilitre", "deciliter")
_unit("l", VOLUME, 1000.0, METRIC, "L", "L", "litre", "liter", "litres", "liters", "ltr")
# Mass (base: gram)
_unit("oz", MASS, 28.3495, US, "oz", "oz", "ounce", "ounces", "onz")
_unit("lb", MASS, 453.592, US, "lb", "lb", "pound", "pounds", "lbs", "#")
_unit("mg", MASS, 0.001, METRIC, "mg", "mg", "milligram", "milligrams")
_unit("g", MASS, 1.0, METRIC, "g", "g", "gram", "grams", "gr", "grs", "gm", "gms")
_unit("kg", MASS, 1000.0, METRIC, "kg", "kg", "kilogram", "kilograms", "kilo", "kilos", "kgs")

# Named count units: summed only with the same unit.
COUNT_UNITS: dict[str, tuple[str, str]] = {
    "clove": ("clove", "cloves"),
    "can": ("can", "cans"),
    "jar": ("jar", "jars"),
    "bottle": ("bottle", "bottles"),
    "box": ("box", "boxes"),
    "bag": ("bag", "bags"),
    "bunch": ("bunch", "bunches"),
    "head": ("head", "heads"),
    "package": ("package", "packages"),
    "pack": ("pack", "packs"),
    "piece": ("piece", "pieces"),
    "slice": ("slice", "slices"),
    "stick": ("stick", "sticks"),
    "sprig": ("sprig", "sprigs"),
    "pinch": ("pinch", "pinches"),
    "dash": ("dash", "dashes"),
    "loaf": ("loaf", "loaves"),
    "dozen": ("dozen", "dozen"),
}
_COUNT_ALIASES: dict[str, str] = {}
for _key, (_singular, _plural) in COUNT_UNITS.items():
    _COUNT_ALIASES[_singular] = _key
    _COUNT_ALIASES[_plural] = _key
_COUNT_ALIASES.update({"pkg": "package", "pkgs": "package", "pc": "piece", "pcs": "piece", "ct": "piece"})

_UNICODE_FRACTIONS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅖": "2/5",
    "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}

_NUMBER = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
_QTY_RE = re.compile(
    rf"^\s*(?P<qty>{_NUMBER})(?:\s*(?:-|to)\s*(?P<qty2>{_NUMBER}))?\s*(?:x\s+)?(?P<rest>.*)$",
    re.IGNORECASE,
)
_MULTIPLIER_RE = re.compile(r"^\s*(?P<mult>\d+)\s*[x×]\s+(?P<rest>.+)$", re.IGNORECASE)
_TRAILING_QTY_RE = re.compile(
    rf"^(?P<name>.+?)(?:\s*[,(-]\s*|\s+)(?:x\s*)?(?P<qty>{_NUMBER})\s*(?P<unit>[a-zA-Z. ]{{0,14}})\)?\s*$"
)

# Words describing preparation that do not change what you buy.
_PREP_WORDS = {
    "chopped", "diced", "minced", "sliced", "grated", "shredded", "crushed", "ground fresh",
    "fresh", "freshly", "finely", "roughly", "coarsely", "thinly", "large", "small", "medium",
    "peeled", "trimmed", "rinsed", "drained", "softened", "melted", "packed", "heaping",
    "to taste", "optional", "divided", "cubed", "halved", "quartered", "julienned",
}


@dataclass
class Quantity:
    """A parsed amount: value in base units for its dimension."""

    amount: float
    dimension: str  # VOLUME | MASS | COUNT
    unit_key: str  # unit key for VOLUME/MASS, count unit key (or "each") for COUNT
    system: str | None = None  # US | METRIC | None for counts

    @property
    def merge_key(self) -> tuple[str, str]:
        if self.dimension == COUNT:
            return (COUNT, self.unit_key)
        return (self.dimension, "")


@dataclass
class ParsedItem:
    """A free-text item line split into quantity and name."""

    name: str
    quantity: Quantity | None = None
    raw_quantity: str | None = None
    note: str | None = None


@dataclass
class MergedItem:
    """A shopping item whose quantities were combined."""

    name: str
    key: str
    quantities: list[Quantity] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    category: str = AISLE_OTHER

    @property
    def display_quantity(self) -> str:
        return format_quantities(self.quantities, self.unparsed)

    @property
    def display(self) -> str:
        qty = self.display_quantity
        return f"{self.name} ({qty})" if qty else self.name


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _replace_unicode_fractions(text: str) -> str:
    for char, frac in _UNICODE_FRACTIONS.items():
        # "1½" -> "1 1/2"
        text = re.sub(rf"(\d){char}", rf"\1 {frac}", text)
        text = text.replace(char, frac)
    return text


def parse_number(text: str) -> float | None:
    """Parse ``"1 1/2"``, ``"3/4"``, ``"0.5"`` or ``"½"`` into a float."""
    text = _replace_unicode_fractions(text.strip())
    if not text:
        return None
    try:
        total = Fraction(0)
        for part in text.split():
            total += Fraction(part)
        return float(total)
    except (ValueError, ZeroDivisionError):
        return None


def lookup_unit(token: str) -> tuple[str, str] | None:
    """Return ``(dimension, unit_key)`` for a unit token, or ``None``."""
    clean = token.strip().rstrip(".").strip()
    if not clean:
        return None
    if clean in ("T",):
        return (VOLUME, "tbsp")
    lower = clean.lower()
    if lower == "t" and clean == "t":
        return (VOLUME, "tsp")
    key = _ALIASES.get(lower)
    if key is not None and lower not in ("t", "c"):
        return (_UNITS[key].dimension, key)
    if lower in ("c",):
        return (VOLUME, "cup")
    count = _COUNT_ALIASES.get(lower)
    if count is not None:
        return (COUNT, count)
    return None


def make_quantity(value: float, dimension: str, unit_key: str) -> Quantity:
    """Build a normalized :class:`Quantity`."""
    if dimension == COUNT:
        if unit_key == "dozen":
            return Quantity(value * 12, COUNT, "each")
        return Quantity(value, COUNT, unit_key)
    unit = _UNITS[unit_key]
    return Quantity(value * unit.factor, dimension, unit_key, unit.system)


def _split_unit(rest: str) -> tuple[tuple[str, str] | None, str]:
    """Split the leading unit token (one or two words) off ``rest``."""
    rest = rest.strip()
    if not rest:
        return None, ""
    # Parenthesised package size, e.g. "(14 oz) can tomatoes" -> keep as name.
    words = rest.split()
    for width in (2, 1):
        if len(words) >= width:
            candidate = " ".join(words[:width])
            found = lookup_unit(candidate)
            if found is not None:
                remaining = " ".join(words[width:])
                if remaining.lower().startswith("of "):
                    remaining = remaining[3:]
                return found, remaining
    return None, rest


def parse_item(text: str) -> ParsedItem:
    """Parse a free-text line such as ``"2 lbs ground beef"`` or ``"garlic powder"``."""
    original = (text or "").strip()
    working = _replace_unicode_fractions(original)
    if not working:
        return ParsedItem(name="")

    multiplier = _MULTIPLIER_RE.match(working)
    if multiplier:
        # "2x 2x4s" / "3 x bananas": a count multiplier.
        rest = multiplier.group("rest").strip()
        value = float(multiplier.group("mult"))
        return ParsedItem(
            name=clean_name(rest),
            quantity=Quantity(value, COUNT, "each"),
            raw_quantity=multiplier.group("mult"),
        )

    match = _QTY_RE.match(working)
    if match and not re.match(r"^\d+x\d+", working, re.IGNORECASE):
        value = parse_number(match.group("qty"))
        if match.group("qty2"):
            upper = parse_number(match.group("qty2"))
            if upper is not None:
                value = upper  # buy for the upper end of a range
        rest = match.group("rest")
        unit, name = _split_unit(rest)
        # Drop a metric equivalent we generated ourselves: "1 cup / 240 ml Milk".
        name = re.sub(rf"^/\s*{_NUMBER}\s*[a-zA-Z]+\.?\s+", "", name)
        if value is not None:
            raw = match.group(0)[: len(match.group(0)) - len(name)].strip() if name else match.group(0).strip()
            raw = re.sub(r"\s+of$", "", raw)
            if unit is None:
                return ParsedItem(name=clean_name(name), quantity=Quantity(value, COUNT, "each"), raw_quantity=raw)
            return ParsedItem(name=clean_name(name), quantity=make_quantity(value, *unit), raw_quantity=raw)

    trailing = _TRAILING_QTY_RE.match(working)
    if trailing and not re.search(r"\d+x\d+$", working, re.IGNORECASE):
        value = parse_number(trailing.group("qty"))
        unit_text = trailing.group("unit").strip()
        unit = lookup_unit(unit_text) if unit_text else None
        if value is not None and (unit is not None or not unit_text):
            name = trailing.group("name").strip().rstrip(",(-").strip()
            quantity = make_quantity(value, *unit) if unit else Quantity(value, COUNT, "each")
            return ParsedItem(name=clean_name(name), quantity=quantity, raw_quantity=trailing.group(0)[len(trailing.group("name")):].strip())

    return ParsedItem(name=clean_name(working))


def clean_name(name: str) -> str:
    """Tidy an item name for display (keeps the user's wording)."""
    name = re.sub(r"\s+", " ", (name or "").strip()).strip(" ,.;:-")
    if not name:
        return ""
    return name[0].upper() + name[1:]


_SINGULAR_EXCEPTIONS = {
    "asparagus", "hummus", "couscous", "molasses", "swiss", "brussels", "grits", "oats",
    "series", "species", "lentils", "peas", "greens", "chips", "nuts", "glass", "bass",
    "citrus", "octopus", "cactus", "noodles", "sprinkles", "beans",
}


def singularize(word: str) -> str:
    """Very small English singularizer good enough for grocery keys."""
    lower = word.lower()
    if len(lower) <= 3 or lower in _SINGULAR_EXCEPTIONS or lower.endswith("ss"):
        return lower
    if lower.endswith("ies"):
        return lower[:-3] + "y"
    if lower.endswith("oes"):
        return lower[:-2]
    if lower.endswith(("ches", "shes", "xes", "zes")):
        return lower[:-2]
    if lower.endswith("ves") and lower not in ("chives", "olives", "cloves"):
        return lower[:-3] + "f"
    if lower.endswith("s") and not lower.endswith("us"):
        return lower[:-1]
    return lower


def name_key(name: str) -> str:
    """Return a normalized key used to detect duplicate items."""
    text = (name or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.split(",")[0]
    for phrase in sorted(_PREP_WORDS, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in ("of", "the", "a", "an", "some")]
    return " ".join(singularize(w) for w in words)


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
def merge_quantities(quantities: list[Quantity]) -> list[Quantity]:
    """Sum compatible quantities (same dimension, or same named count unit)."""
    merged: dict[tuple[str, str], Quantity] = {}
    systems: dict[tuple[str, str], set[str]] = {}
    for qty in quantities:
        key = qty.merge_key
        if key not in merged:
            merged[key] = Quantity(qty.amount, qty.dimension, qty.unit_key, qty.system)
            systems[key] = {qty.system} if qty.system else set()
        else:
            merged[key].amount += qty.amount
            if qty.system:
                systems[key].add(qty.system)
    for key, qty in merged.items():
        if qty.dimension != COUNT:
            # US wins when any US unit was involved so the shopper sees familiar units.
            qty.system = US if US in systems[key] else METRIC
    return list(merged.values())


def merge_items(lines: list[str | ParsedItem], categorizer=None) -> list[MergedItem]:
    """Merge a list of item lines into deduplicated items, preserving first-seen order."""
    result: dict[str, MergedItem] = {}
    for line in lines:
        parsed = line if isinstance(line, ParsedItem) else parse_item(line)
        if not parsed.name:
            continue
        key = name_key(parsed.name)
        item = result.get(key)
        if item is None:
            category = categorizer(parsed.name) if categorizer else categorize_parsed(parsed)
            item = result[key] = MergedItem(name=parsed.name, key=key, category=category)
        if parsed.quantity is not None:
            item.quantities.append(parsed.quantity)
        elif parsed.raw_quantity:
            item.unparsed.append(parsed.raw_quantity)
    for item in result.values():
        item.quantities = merge_quantities(item.quantities)
    return list(result.values())


def combine_quantity_text(existing: str | None, addition: str | None) -> str | None:
    """Combine two stored quantity strings (e.g. ``"0.5 cup"`` and ``"120 ml"``)."""
    parts: list[Quantity] = []
    unparsed: list[str] = []
    for text in (existing, addition):
        if not text:
            continue
        for chunk in re.split(r"\s+\+\s+", text):
            qty = parse_quantity_text(chunk)
            if qty is None:
                if chunk.strip():
                    unparsed.append(chunk.strip())
            else:
                parts.append(qty)
    if not parts and not unparsed:
        return None
    return format_quantities(merge_quantities(parts), unparsed) or None


def parse_quantity_text(text: str) -> Quantity | None:
    """Parse a quantity-only string like ``"1 cup / 240 ml"`` or ``"3 cloves"``."""
    text = _replace_unicode_fractions((text or "").strip())
    if not text:
        return None
    primary = text.split("/ ")[0].strip() if " / " in text else text
    match = re.match(rf"^(?P<qty>{_NUMBER})\s*(?P<unit>.*)$", primary)
    if not match:
        return None
    value = parse_number(match.group("qty"))
    if value is None:
        return None
    unit_text = match.group("unit").strip()
    if not unit_text:
        return Quantity(value, COUNT, "each")
    unit = lookup_unit(unit_text)
    if unit is None:
        return None
    return make_quantity(value, *unit)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _nice_fraction(value: float, denominators: tuple[int, ...]) -> tuple[Fraction, float]:
    best: Fraction | None = None
    best_err = float("inf")
    for denominator in denominators:
        frac = Fraction(round(value * denominator), denominator)
        err = abs(float(frac) - value)
        if err < best_err - 1e-9:
            best, best_err = frac, err
    assert best is not None
    return best, best_err


def format_fraction(frac: Fraction) -> str:
    whole, remainder = divmod(frac.numerator, frac.denominator)
    if remainder == 0:
        return str(whole)
    part = f"{remainder}/{frac.denominator}"
    return f"{whole} {part}" if whole else part


def _format_decimal(value: float, places: int = 2) -> str:
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_us_volume(ml: float) -> str:
    cups = ml / _UNITS["cup"].factor
    if cups >= 16 * 0.97:
        return f"{_format_decimal(ml / _UNITS['gallon'].factor)} gal"
    if cups >= 4 * 0.97:
        return f"{_format_decimal(ml / _UNITS['quart'].factor)} qt"
    if cups >= 0.97:
        frac, err = _nice_fraction(cups, (1, 2, 3, 4, 8))
        if err / cups <= 0.06:
            unit = "cup" if frac == 1 else "cups"
            return f"{format_fraction(frac)} {unit}"
        return f"{_format_decimal(cups)} cups"
    if cups >= 0.25 - 1e-6:
        frac, err = _nice_fraction(cups, (2, 3, 4))
        if err / cups <= 0.04:
            return f"{format_fraction(frac)} cup"
    tbsp = ml / _UNITS["tbsp"].factor
    if tbsp >= 0.97:
        frac, err = _nice_fraction(tbsp, (1, 2))
        if err / tbsp <= 0.08:
            return f"{format_fraction(frac)} tbsp"
        return f"{_format_decimal(tbsp, 1)} tbsp"
    tsp = ml / _UNITS["tsp"].factor
    frac, err = _nice_fraction(tsp, (1, 2, 4, 8))
    if frac > 0 and err / max(tsp, 1e-9) <= 0.15:
        return f"{format_fraction(frac)} tsp"
    return f"{_format_decimal(tsp, 2)} tsp"


def _round_metric(value: float) -> float:
    if value >= 1000:
        return round(value / 10) * 10
    if value >= 100:
        return round(value / 5) * 5
    if value >= 10:
        return round(value)
    return round(value, 1)


def _format_metric_volume(ml: float) -> str:
    rounded = _round_metric(ml)
    if rounded >= 1000:
        return f"{_format_decimal(rounded / 1000)} L"
    return f"{_format_decimal(rounded, 1)} ml"


def _format_us_mass(grams: float) -> str:
    pounds = grams / _UNITS["lb"].factor
    if pounds >= 0.97:
        frac, err = _nice_fraction(pounds, (1, 2, 4))
        if err / pounds <= 0.04:
            return f"{format_fraction(frac)} lb"
        return f"{_format_decimal(pounds)} lb"
    ounces = grams / _UNITS["oz"].factor
    frac, err = _nice_fraction(ounces, (1, 2, 4))
    if frac > 0 and err / ounces <= 0.06:
        return f"{format_fraction(frac)} oz"
    return f"{_format_decimal(ounces, 1)} oz"


def _format_metric_mass(grams: float) -> str:
    rounded = _round_metric(grams)
    if rounded >= 1000:
        return f"{_format_decimal(rounded / 1000)} kg"
    return f"{_format_decimal(rounded, 1)} g"


def format_quantity(qty: Quantity) -> str:
    """Render a single (merged) quantity."""
    if qty.dimension == COUNT:
        value = _format_decimal(qty.amount)
        if qty.unit_key == "each":
            return value
        singular, plural = COUNT_UNITS.get(qty.unit_key, (qty.unit_key, qty.unit_key + "s"))
        return f"{value} {singular if abs(qty.amount - 1) < 1e-9 else plural}"
    if qty.dimension == VOLUME:
        if qty.system == US:
            return f"{_format_us_volume(qty.amount)} / {_format_metric_volume(qty.amount)}"
        return _format_metric_volume(qty.amount)
    if qty.system == US:
        return f"{_format_us_mass(qty.amount)} / {_format_metric_mass(qty.amount)}"
    return _format_metric_mass(qty.amount)


def format_quantities(quantities: list[Quantity], unparsed: list[str] | None = None) -> str:
    """Render several incompatible quantities joined with ``+``."""
    parts = [format_quantity(qty) for qty in quantities]
    parts.extend(unparsed or [])
    return " + ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Aisle categorization
# ---------------------------------------------------------------------------
_AISLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    AISLE_HARDWARE: (
        "2x4", "2x6", "2x8", "2x10", "4x4", "lumber", "plywood", "osb", "drywall", "screw", "nail",
        "bolt", "nut and bolt", "washer", "anchor", "hinge", "bracket", "caulk", "sealant", "paint",
        "primer", "brush", "roller", "sandpaper", "glue", "epoxy", "tape", "duct", "wire", "cable",
        "outlet", "switch", "bulb", "fuse", "pipe", "pvc", "fitting", "valve", "filter", "furnace",
        "battery", "batteries", "drill", "bit", "saw", "blade", "stain", "polyurethane", "grout",
        "tile", "mortar", "concrete", "rebar", "shim", "dowel", "board", "trim", "molding", "lag",
        "zip tie", "extension cord", "wd-40", "spackle", "hose", "fertilizer", "mulch", "soil",
    ),
    AISLE_PANTRY: (
        "powder", "salt", "pepper", "paprika", "cumin", "oregano", "basil dried", "thyme dried",
        "cinnamon", "nutmeg", "spice", "seasoning", "rub", "chili flake", "flakes", "bay lea",
        "flour", "sugar", "baking", "yeast", "rice", "pasta", "noodle", "oat", "cereal", "bean",
        "lentil", "broth", "stock", "bouillon", "oil", "vinegar", "soy sauce", "sauce", "ketchup",
        "mustard", "mayo", "mayonnaise", "honey", "syrup", "jam", "jelly", "peanut butter", "nut",
        "almond", "cracker", "chip", "can of", "canned", "tomato paste", "tomato sauce", "coffee",
        "tea", "cocoa", "chocolate", "vanilla", "extract", "cornstarch", "breadcrumb", "panko",
        "quinoa", "tortilla", "salsa", "dressing", "marinade", "molasses", "cornmeal", "sriracha",
        "worcestershire", "hot sauce", "bbq sauce", "spaghetti", "macaroni", "raisin",
    ),
    AISLE_MEAT: (
        "beef", "steak", "chicken", "pork", "bacon", "sausage", "ham", "turkey", "lamb", "brisket",
        "rib", "roast", "ground", "chop", "fillet", "filet", "salmon", "tuna", "shrimp", "fish",
        "cod", "tilapia", "crab", "lobster", "scallop", "venison", "bratwurst", "hot dog", "pepperoni",
        "salami", "prosciutto", "meatball", "wing", "thigh", "drumstick", "breast", "tenderloin",
        "shoulder", "butt", "loin", "chuck",
    ),
    AISLE_DAIRY: (
        "milk", "cheese", "cheddar", "mozzarella", "parmesan", "butter", "yogurt", "yoghurt", "cream",
        "sour cream", "half and half", "half-and-half", "egg", "cottage", "ricotta", "feta", "brie",
        "kefir", "creamer", "whipped", "ghee", "provolone", "gouda",
    ),
    AISLE_PRODUCE: (
        "apple", "banana", "orange", "lemon", "lime", "grape", "berry", "berries", "strawberr",
        "blueberr", "raspberr", "melon", "watermelon", "peach", "pear", "plum", "cherry", "mango",
        "pineapple", "avocado", "tomato", "potato", "onion", "garlic", "shallot", "scallion",
        "carrot", "celery", "lettuce", "spinach", "kale", "cabbage", "broccoli", "cauliflower",
        "pepper bell", "bell pepper", "jalapeno", "jalapeño", "zucchini", "squash", "cucumber",
        "mushroom", "corn", "pea", "green bean", "asparagus", "herb", "cilantro", "parsley",
        "basil", "mint", "dill", "rosemary", "thyme", "ginger", "beet", "radish", "sweet potato",
        "yam", "leek", "arugula", "eggplant", "salad", "sprout", "fruit", "vegetable", "veggie", "chive",
    ),
}
# Earlier entries win when several keywords match (e.g. "garlic powder" -> Spices/Pantry).
_AISLE_ORDER = (AISLE_HARDWARE, AISLE_PANTRY, AISLE_MEAT, AISLE_DAIRY, AISLE_PRODUCE)


def categorize(name: str) -> str:
    """Return the store aisle for an item name."""
    text = " " + re.sub(r"[^a-z0-9\- ]+", " ", (name or "").lower()) + " "
    best: tuple[int, int, str] | None = None
    for priority, aisle in enumerate(_AISLE_ORDER):
        for keyword in _AISLE_KEYWORDS[aisle]:
            if re.search(rf"(?<![a-z]){re.escape(keyword)}", text):
                # Longer keyword matches are more specific: "ground beef" beats "ground".
                score = (len(keyword), -priority, aisle)
                if best is None or score[:2] > best[:2]:
                    best = score
    if best is None:
        return AISLE_OTHER
    # Pantry descriptors ("powder", "dried", "sauce") override fresh produce/meat words.
    pantry_override = re.search(
        r"\b(powder|dried|seasoning|flakes|sauce|paste|canned|extract|rub|broth|stock|bouillon|soup)\b", text
    )
    if pantry_override and best[2] in (AISLE_PRODUCE, AISLE_MEAT, AISLE_DAIRY):
        return AISLE_PANTRY
    return best[2]


_PACKAGED_COUNT_UNITS = frozenset({"can", "jar", "bottle", "box", "bag", "package", "pack"})


def categorize_parsed(item: ParsedItem) -> str:
    """Categorize a parsed item, using packaging units as a hint ("2 cans tomatoes")."""
    category = categorize(item.name)
    if (
        item.quantity is not None
        and item.quantity.dimension == COUNT
        and item.quantity.unit_key in _PACKAGED_COUNT_UNITS
        and category in (AISLE_PRODUCE, AISLE_OTHER)
    ):
        return AISLE_PANTRY
    return category
