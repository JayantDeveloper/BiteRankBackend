"""Scoring utilities for BiteRank deals."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TYPICAL_MEAL_CALORIES = 800
TYPICAL_MEAL_PROTEIN = 30
TYPICAL_MEAL_PRICE = 9.0
NUGGET_CAL_PER_PIECE = 50
NUGGET_PROTEIN_PER_PIECE = 3.0

# Heuristic nutrition anchors
BURGER_CAL = 650
BURGER_PROT = 30
WRAP_CAL = 550
WRAP_PROT = 25
BURRITO_CAL = 750
BURRITO_PROT = 28
FRIES_CAL = 400
FRIES_PROT = 5
SALAD_CAL = 400
SALAD_PROT = 25
TACO_CAL = 250
TACO_PROT = 12
SANDWICH_CAL = 450
SANDWICH_PROT = 22
BOWL_CAL = 650
BOWL_PROT = 28
NACHOS_CAL = 700
NACHOS_PROT = 18
PIZZA_CAL = 700
PIZZA_PROT = 30

TYPICAL_PRICE_PER_CALORIE = TYPICAL_MEAL_PRICE / TYPICAL_MEAL_CALORIES

MERCH_KEYWORDS = ("sock", "tote", "toy", "shirt", "gift", "merch", "hoodie", "cap", "hat", "mug")
SAUCE_KEYWORDS = ("sauce", "dip", "packet", "syrup", "ranch", "honey mustard", "bbq sauce")
DRINK_KEYWORDS = (
    "coke", "cola", "tea", "coffee", "lemonade", "shake", "smoothie",
    "frappe", "frappé", "water", "sprite", "juice", "drink", "beverage",
    "slurpee", "icee", "soda", "milk", "chocolate milk",
)

FOOD_KEYWORDS = (
    "burger", "sandwich", "chicken", "nugget", "tender", "strip", "wing",
    "taco", "burrito", "wrap", "bowl", "fries", "salad", "pizza", "rice",
    "meal", "combo", "platter", "box", "value", "thigh", "breast",
    "original recipe", "crispy", "spicy", "grilled", "pot pie",
    "quesadilla", "chalupa", "gordita", "tostada", "nachos",
    "fish", "shrimp", "steak", "beef", "pork", "bbq",
)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_calories(calories: Any) -> int:
    c = _to_int(calories)
    return 0 if (c is None or c <= 0) else c


def _coerce_protein(protein_grams: Any) -> float:
    p = _to_float(protein_grams)
    return 0.0 if (p is None or p < 0) else p


def _coerce_price(price: Any) -> float:
    p = _to_float(price)
    return 0.0 if (p is None or p <= 0) else p


def parse_piece_quantity(text: str) -> Optional[int]:
    if not text:
        return None
    import re
    m = re.search(r"(\d+)\s*(?:pc|pcs|piece|pieces)\b", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def classify_item_category(name: str) -> str:
    n = (name or "").lower()
    if any(k in n for k in MERCH_KEYWORDS):
        return "merch"
    if any(k in n for k in SAUCE_KEYWORDS):
        return "sauce"
    if any(k in n for k in DRINK_KEYWORDS):
        return "drink"
    if any(k in n for k in FOOD_KEYWORDS):
        return "food"
    return "unknown"


def estimate_nugget_nutrition(name: str) -> Optional[Dict[str, float]]:
    qty = parse_piece_quantity(name)
    if qty is None or qty <= 0:
        return None
    n = (name or "").lower()
    if not any(k in n for k in ("nugget", "tender", "strip", "wing", "piece", "pc")):
        return None
    calories = qty * NUGGET_CAL_PER_PIECE
    protein = qty * NUGGET_PROTEIN_PER_PIECE
    return {"calories": calories, "protein_grams": protein}


def estimate_nutrition_heuristic(
    name: str,
    category: Optional[str] = None,
    description: str = "",
) -> Optional[Dict[str, float]]:
    n = (name or "").lower()
    desc = (description or "").lower()

    # Try piece-count estimation first (nuggets, tenders, wings)
    est = estimate_nugget_nutrition(name)
    if est:
        return est

    # Specific well-known items
    if "mcdouble" in n:
        return {"calories": 390, "protein_grams": 22}
    if "mcchicken" in n or "mc chicken" in n:
        return {"calories": 400, "protein_grams": 14}
    if "mcrib" in n:
        return {"calories": 520, "protein_grams": 22}
    if "big mac" in n:
        return {"calories": 550, "protein_grams": 25}
    if "quarter pounder" in n or "qpc" in n:
        return {"calories": 530, "protein_grams": 30}
    if "whopper" in n:
        return {"calories": 660, "protein_grams": 28}
    if "baconator" in n:
        return {"calories": 940, "protein_grams": 57}
    if "famous bowl" in n:
        return {"calories": 710, "protein_grams": 26}
    if "pot pie" in n:
        return {"calories": 790, "protein_grams": 26}
    if "crunchwrap" in n:
        return {"calories": 520, "protein_grams": 17}
    if "beefy 5-layer" in n or "beefy five" in n:
        return {"calories": 500, "protein_grams": 19}
    if "chalupa" in n:
        return {"calories": 350, "protein_grams": 14}
    if "nachos bellgrande" in n or "nachos bell grande" in n:
        return {"calories": 740, "protein_grams": 20}
    if "gordita" in n:
        return {"calories": 300, "protein_grams": 13}
    if "dave's single" in n:
        return {"calories": 590, "protein_grams": 30}
    if "dave's double" in n or "daves double" in n:
        return {"calories": 820, "protein_grams": 50}

    # Category-level patterns
    if "bowl" in n:
        return {"calories": BOWL_CAL, "protein_grams": BOWL_PROT}
    if "nachos" in n:
        return {"calories": NACHOS_CAL, "protein_grams": NACHOS_PROT}
    if "pizza" in n:
        return {"calories": PIZZA_CAL, "protein_grams": PIZZA_PROT}
    if "burrito" in n or "quesarito" in n:
        return {"calories": BURRITO_CAL, "protein_grams": BURRITO_PROT}
    if "taco" in n:
        return {"calories": TACO_CAL, "protein_grams": TACO_PROT}
    if "wrap" in n:
        return {"calories": WRAP_CAL, "protein_grams": WRAP_PROT}
    if "sandwich" in n or "sub" in n or "hoagie" in n or "footlong" in n:
        return {"calories": SANDWICH_CAL, "protein_grams": SANDWICH_PROT}
    if "burger" in n or "cheeseburger" in n or "patty" in n:
        return {"calories": BURGER_CAL, "protein_grams": BURGER_PROT}
    if "chicken" in n or "crispy" in n or "grilled" in n or "spicy" in n:
        return {"calories": 480, "protein_grams": 28}
    if "fries" in n or "fry" in n or "potato" in n:
        return {"calories": FRIES_CAL, "protein_grams": FRIES_PROT}
    if "salad" in n:
        return {"calories": SALAD_CAL, "protein_grams": SALAD_PROT}
    if "combo" in n or "meal" in n or "box" in n or "platter" in n:
        return {"calories": TYPICAL_MEAL_CALORIES, "protein_grams": TYPICAL_MEAL_PROTEIN}

    # Final fallback: any item that looks like food gets a generic estimate
    if classify_item_category(n) == "food":
        return {"calories": 550, "protein_grams": 22}

    return None


def calculate_satiety_score(calories: Any, protein_grams: Any) -> float:
    cals = _coerce_calories(calories)
    prot = _coerce_protein(protein_grams)

    if cals <= 0:
        return 0.0

    cal_component = (cals / float(TYPICAL_MEAL_CALORIES)) * 0.7
    protein_component = (prot / float(TYPICAL_MEAL_PROTEIN)) * 0.3
    raw = cal_component + protein_component

    score = (1.0 - math.exp(-raw)) * 100.0
    return round(_clamp(score), 1)


def calculate_price_efficiency_score(price: Any, calories: Any) -> float:
    p = _coerce_price(price)
    cals = _coerce_calories(calories)

    if p <= 0 or cals <= 0:
        return 0.0

    deal_ppc = p / float(cals)
    r = TYPICAL_PRICE_PER_CALORIE / deal_ppc
    return round(_clamp(r * 50.0), 1)


def calculate_final_value_score(calories: Any, protein_grams: Any, price: Any) -> Dict[str, float]:
    """Final = 40% satiety + 60% price-efficiency. Always returns stable numeric dict."""
    cals = _coerce_calories(calories)
    prot = _coerce_protein(protein_grams)
    p = _coerce_price(price)

    satiety = calculate_satiety_score(cals, prot)
    price_eff = calculate_price_efficiency_score(p, cals)
    final_score = satiety * 0.4 + price_eff * 0.6
    price_per_calorie = (p / float(cals)) if (p > 0 and cals > 0) else 0.0

    return {
        "value_score": round(final_score, 1),
        "satiety_score": round(satiety, 1),
        "price_per_calorie": round(price_per_calorie, 6),
        "price_efficiency_score": round(price_eff, 1),
    }
