import pytest

from services.value_calculator import parse_piece_quantity, classify_item_category, estimate_nugget_nutrition


@pytest.mark.parametrize(
    "text,expected",
    [
        ("40 pc nuggets", 40),
        ("20 pcs", 20),
        ("5 piece meal", 5),
        ("no quantity", None),
    ],
)
def test_parse_piece_quantity(text, expected):
    assert parse_piece_quantity(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Cool socks", "merch"),
        ("Maple syrup packet", "sauce"),
        ("Diet Coke bottle", "drink"),
        ("Chicken nuggets", "food"),
    ],
)
def test_classify_item_category(text, expected):
    assert classify_item_category(text) == expected


def test_estimate_nugget_nutrition():
    est = estimate_nugget_nutrition("20 pc chicken nuggets")
    assert est is not None
    assert est["calories"] == 20 * 50
    assert est["protein_grams"] == 20 * 3.0
