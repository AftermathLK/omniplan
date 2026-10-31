"""Barcode normalization and Open Food Facts response parsing."""

from __future__ import annotations

import pytest

from taskdragon.barcodes import (
    BarcodeError,
    best_match,
    display_barcode,
    expand_upce,
    gtin_check_digit,
    is_valid_gtin,
    lookup_gtin,
    normalize_barcode,
    product_summary,
    suggested_title,
)

UPC_A = "036000291452"  # a classic valid UPC-A
EAN_13 = "4006381333931"


def test_check_digits() -> None:
    assert gtin_check_digit(UPC_A[:-1]) == 2
    assert is_valid_gtin(UPC_A) and is_valid_gtin(EAN_13) and is_valid_gtin("96385074")
    assert not is_valid_gtin("036000291453") and not is_valid_gtin("abc") and not is_valid_gtin("123")


def test_upc_a_and_ean13_forms_normalize_to_one_code() -> None:
    assert normalize_barcode(UPC_A, "upc_a") == ("0" + UPC_A, "upc_a")
    assert normalize_barcode("0" + UPC_A, "ean_13") == ("0" + UPC_A, "ean_13")
    # Typed in (no format) and GTIN-14 with a packaging zero land on the same code.
    assert normalize_barcode(f" {UPC_A}\n")[0] == "0" + UPC_A
    assert normalize_barcode("00" + UPC_A)[0] == "0" + UPC_A
    assert normalize_barcode("0-36000-29145-2")[0] == "0" + UPC_A
    assert display_barcode("0" + UPC_A) == UPC_A and display_barcode(EAN_13) == EAN_13
    assert lookup_gtin("0" + UPC_A) == "0" + UPC_A and lookup_gtin("ABC-123") is None


def test_upc_e_expands_to_upc_a() -> None:
    assert expand_upce("04252614") == "042100005264"
    assert normalize_barcode("04252614", "upc_e") == ("0042100005264", "upc_e")
    assert expand_upce("04252615") is None  # wrong check digit
    with pytest.raises(BarcodeError):
        normalize_barcode("04252615", "upc_e")


def test_other_symbologies_and_junk() -> None:
    assert normalize_barcode("96385074", "ean_8") == ("96385074", "ean_8")
    assert normalize_barcode("FLTR-16x25x4", "code_128") == ("FLTR-16x25x4", "code_128")
    assert normalize_barcode("https://example.com/x?y=1", "qr_code") == ("https://example.com/x?y=1", "qr_code")
    # A numeric Code-128 that happens not to be a GTIN stays as scanned.
    assert normalize_barcode("12345", "code_128") == ("12345", "code_128")
    assert normalize_barcode("]E0" + EAN_13)[0] == EAN_13  # symbology identifier prefix
    assert normalize_barcode("AB\x00C\x1d")[0] == "ABC"
    with pytest.raises(BarcodeError):
        normalize_barcode("036000291453", "upc_a")  # misread digit
    with pytest.raises(BarcodeError):
        normalize_barcode("   ")
    with pytest.raises(BarcodeError):
        normalize_barcode("x" * 300)


def test_product_summary_and_titles() -> None:
    payload = {
        "status": 1,
        "product": {
            "product_name": "Morton  System Saver Pellets",
            "brands": "Morton, Morton Salt Inc",
            "quantity": "40 lb",
            "categories_tags": ["en:salts", "en:water-softener-salts"],
        },
    }
    summary = product_summary(payload)
    assert summary == {
        "name": "Morton System Saver Pellets",
        "brand": "Morton",
        "quantity": "40 lb",
        "categories": ["salts", "water softener salts"],
        "source": "openfoodfacts",
    }
    assert suggested_title(summary) == "System Saver Pellets"
    assert product_summary({"status": 0, "status_verbose": "product not found"}) is None
    assert product_summary({"status": 1, "product": {"product_name": "", "brands": ""}}) is None
    assert product_summary("nope") is None
    assert suggested_title(None) is None


def test_best_match_needs_real_overlap() -> None:
    parts = [{"id": "p1", "part_name": "Furnace filter 16x25x4"}, {"id": "p2", "part_name": "Softener salt pellets"}]
    assert best_match("System Saver Salt Pellets", parts, "part_name")["id"] == "p2"
    assert best_match("Filtrete 16x25x4 furnace filter, 2 pack", parts, "part_name")["id"] == "p1"
    assert best_match("Organic peanut butter", parts, "part_name") is None
    assert best_match(None, parts, "part_name") is None
