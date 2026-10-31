"""Barcode normalization and product-name helpers (no Home Assistant imports).

Phones disagree about how they report the same retail barcode: one camera
engine returns a UPC-A as 12 digits, another as the 13-digit EAN form with a
leading zero, a third reports the compressed 8-digit UPC-E printed on small
packs. :func:`normalize_barcode` turns every retail code into one canonical
GTIN (13 digits for UPC-A/UPC-E/EAN-13, 8 for EAN-8) so a mapping saved from
one phone matches a scan from another. Other symbologies (Code-128, Code-39,
QR) are kept as scanned, minus control characters and surrounding space.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .units import name_key

MAX_BARCODE_LENGTH = 256
RETAIL = ("upc_a", "upc_e", "ean_13", "ean_8")
_DIGITS_RE = re.compile(r"^\d+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_GS1_PREFIX_RE = re.compile(r"^\]([A-Za-z])(\d)")  # symbology identifier some scanners prepend
_STOP_WORDS = frozenset({"the", "and", "with", "for", "of", "a", "an", "in", "pack", "count", "ct", "oz", "lb", "lbs", "g", "kg", "ml", "l"})


class BarcodeError(ValueError):
    """The scanned text cannot be a barcode."""


def gtin_check_digit(body: str) -> int:
    """The GS1 check digit for ``body`` (every digit except the last)."""
    total = 0
    for position, char in enumerate(reversed(body)):
        total += int(char) * (3 if position % 2 == 0 else 1)
    return (10 - total % 10) % 10


def is_valid_gtin(code: str) -> bool:
    return bool(_DIGITS_RE.match(code or "")) and len(code) in (8, 12, 13, 14) and gtin_check_digit(code[:-1]) == int(code[-1])


def expand_upce(code: str) -> str | None:
    """UPC-E (8 digits: number system, 6 data digits, check) to its 12-digit UPC-A, or None."""
    if not _DIGITS_RE.match(code or ""):
        return None
    if len(code) == 6:
        system, data, check = "0", code, None
    elif len(code) == 7:
        system, data, check = code[0], code[1:], None
    elif len(code) == 8:
        system, data, check = code[0], code[1:7], code[7]
    else:
        return None
    if system not in "01":
        return None
    d1, d2, d3, d4, d5, d6 = data
    if d6 in "012":
        body = f"{system}{d1}{d2}{d6}0000{d3}{d4}{d5}"
    elif d6 == "3":
        body = f"{system}{d1}{d2}{d3}00000{d4}{d5}"
    elif d6 == "4":
        body = f"{system}{d1}{d2}{d3}{d4}00000{d5}"
    else:
        body = f"{system}{d1}{d2}{d3}{d4}{d5}0000{d6}"
    digit = gtin_check_digit(body)
    if check is not None and int(check) != digit:
        return None
    return f"{body}{digit}"


def normalize_barcode(raw: Any, symbology: str | None = None) -> tuple[str, str]:
    """Canonical ``(code, symbology)`` for a scan; raises :class:`BarcodeError` for junk.

    Retail codes become GTIN-13 (or EAN-8). A 12/13-digit string with a valid
    check digit is treated as retail even when the scanner did not say so, so
    typed-in codes match scanned ones.
    """
    text = _CONTROL_RE.sub("", str(raw or "")).strip()
    match = _GS1_PREFIX_RE.match(text)
    if match and _DIGITS_RE.match(text[3:]):
        text = text[3:]
    if not text:
        raise BarcodeError("Nothing was scanned")
    if len(text) > MAX_BARCODE_LENGTH:
        raise BarcodeError("That code is too long")
    kind = (symbology or "").lower().replace("-", "_")
    if kind == "upc_e":
        expanded = expand_upce(text)
        if expanded is None:
            raise BarcodeError("That UPC-E code has a bad check digit")
        return "0" + expanded, "upc_e"
    digits = text.replace(" ", "").replace("-", "")
    if _DIGITS_RE.match(digits):
        if kind in RETAIL or kind in ("", "unknown"):
            if len(digits) == 12 and is_valid_gtin(digits):
                return "0" + digits, kind if kind in RETAIL else "upc_a"
            if len(digits) == 13 and is_valid_gtin(digits):
                return digits, kind if kind in RETAIL else "ean_13"
            if len(digits) == 14 and is_valid_gtin(digits) and digits.startswith("0"):
                return digits[1:], kind if kind in RETAIL else "ean_13"
            if len(digits) == 8 and is_valid_gtin(digits):
                return digits, kind if kind in RETAIL else "ean_8"
            if kind in RETAIL:
                raise BarcodeError("That barcode has a bad check digit; scan it again")
    return text, kind if kind in ("code_128", "code_39", "qr_code") else "unknown"


def lookup_gtin(code: str) -> str | None:
    """The code to ask a product database about (retail codes only)."""
    if is_valid_gtin(code) and len(code) in (8, 13):
        return code
    return None


def display_barcode(code: str) -> str:
    """UPC-A codes print as 12 digits on the package; show them that way."""
    if len(code) == 13 and code.startswith("0") and is_valid_gtin(code):
        return code[1:]
    return code


def product_summary(payload: Any) -> dict[str, Any] | None:
    """Name, brand and size from an Open Food Facts v2 product response (None when unknown)."""
    if not isinstance(payload, Mapping) or payload.get("status") not in (1, "1", "success_with_warnings", "success"):
        return None
    product = payload.get("product")
    if not isinstance(product, Mapping):
        return None

    def text(key: str, limit: int) -> str | None:
        value = product.get(key)
        cleaned = " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""
        return cleaned[:limit] or None

    name = text("product_name", 120) or text("product_name_en", 120) or text("generic_name", 120) or text("abbreviated_product_name", 120)
    brand = text("brands", 120)
    if brand and "," in brand:
        brand = brand.split(",", 1)[0].strip() or None
    if not name and not brand:
        return None
    categories = []
    for tag in product.get("categories_tags") or []:
        if isinstance(tag, str) and ":" in tag:
            label = tag.split(":", 1)[1].replace("-", " ").strip()
            if label and label not in categories:
                categories.append(label[:60])
    return {
        "name": name or brand,
        "brand": brand,
        "quantity": text("quantity", 60),
        "categories": categories[-3:],
        "source": "openfoodfacts",
    }


def suggested_title(summary: Mapping[str, Any] | None) -> str | None:
    """A pantry/part name proposal: the product name without the brand repeated in front."""
    if not summary:
        return None
    name = str(summary.get("name") or "").strip()
    brand = str(summary.get("brand") or "").strip()
    if brand and name.lower().startswith(brand.lower() + " "):
        name = name[len(brand) + 1 :].strip()
    return name[:120] or brand[:120] or None


def _tokens(text: str) -> set[str]:
    return {word for word in name_key(text).split() if len(word) > 1 and word not in _STOP_WORDS}


def best_match(title: str | None, candidates: Iterable[Mapping[str, Any]], key: str = "name") -> Mapping[str, Any] | None:
    """The candidate whose name shares the most words with ``title`` (at least half of the candidate's words)."""
    wanted = _tokens(title or "")
    if not wanted:
        return None
    best: tuple[float, Mapping[str, Any]] | None = None
    for candidate in candidates:
        words = _tokens(str(candidate.get(key) or ""))
        if not words:
            continue
        shared = len(words & wanted)
        if shared == 0 or shared / len(words) < 0.5:
            continue
        score = shared / len(words) + shared / max(len(wanted), 1)
        if best is None or score > best[0]:
            best = (score, candidate)
    return best[1] if best else None
