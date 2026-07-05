import os

from services.ubereats_firecrawl import parse_menu_markdown, slug_matches_brand

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "ubereats_menu_sample.md")


def test_parse_real_markdown_fixture():
    md = open(FIXTURE).read()
    items = parse_menu_markdown(md)
    # Fixture holds 35 price lines; after cross-section dedup a healthy
    # majority must survive with valid prices.
    assert len(items) >= 20
    assert all(i["price"] > 0 for i in items)
    by_name = {i["name"]: i for i in items}
    assert "Medium French Fries" in by_name
    assert by_name["Medium French Fries"]["calories"] == 320


def test_parse_calorie_range_averages():
    md = "- [Combo Meal\\\\\n    \\\\\n    $14.89 • 900 - 1200 Cal.\\\\\n"
    items = parse_menu_markdown(md)
    assert items == [{"name": "Combo Meal", "price": 14.89, "calories": 1050, "protein_grams": None, "category": None}]


def test_parse_price_without_calories():
    md = "- [Mystery Box\\\\\n    \\\\\n    $5.00\\\\\n"
    items = parse_menu_markdown(md)
    assert items[0]["calories"] is None
    assert items[0]["price"] == 5.0


def test_parse_dedupes_repeated_items():
    block = "- [Big Mac\\\\\n    \\\\\n    $5.99 • 550 Cal.\\\\\n"
    items = parse_menu_markdown(block * 3)
    assert len(items) == 1


def test_slug_matches_brand():
    assert slug_matches_brand("https://www.ubereats.com/store/kfc-6101-greenbelt-rd/x", "KFC")
    assert slug_matches_brand("https://www.ubereats.com/store/chick-fil-a-7242-baltimore-avenue/x", "Chick-fil-A")
    assert slug_matches_brand("https://www.ubereats.com/store/popeyes-louisiana-chicken-7415/x", "Popeyes")
    assert not slug_matches_brand("https://www.ubereats.com/store/koite-grill-college-park/x", "KFC")
    assert not slug_matches_brand("https://www.ubereats.com/store/panda-express-university-of-maryland/x", "Subway")
