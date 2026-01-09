# services/value_calculator.py
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# -----------------------------
# Baseline values (keep these)
# -----------------------------
TYPICAL_MEAL_CALORIES = 800
TYPICAL_MEAL_PROTEIN = 30
TYPICAL_MEAL_PRICE = 9.0
NUGGET_CAL_PER_PIECE = 50
NUGGET_PROTEIN_PER_PIECE = 3.0
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

# Typical price-per-calorie (~0.01125)
TYPICAL_PRICE_PER_CALORIE = TYPICAL_MEAL_PRICE / TYPICAL_MEAL_CALORIES

MERCH_KEYWORDS = ("sock", "tote", "toy", "shirt", "gift", "merch", "hoodie", "cap")
SAUCE_KEYWORDS = ("sauce", "dip", "packet", "syrup")
DRINK_KEYWORDS = (
    "coke",
    "cola",
    "tea",
    "coffee",
    "lemonade",
    "shake",
    "smoothie",
    "frappe",
    "frappé",
    "water",
    "sprite",
)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _to_int(value: Any) -> Optional[int]:
    """
    Best-effort coercion:
      - int/float -> int
      - numeric string -> int
      - None/invalid -> None
    """
    if value is None:
        return None
    try:
        # Handles "650", "650.0", 650.2
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
    """
    Calories must be a positive int.
    Invalid/non-positive -> 0 (signals "unknown" in our scoring).
    """
    c = _to_int(calories)
    if c is None or c <= 0:
        return 0
    return c


def _coerce_protein(protein_grams: Any) -> float:
    """
    Protein must be >= 0.
    Invalid/None -> 0.0
    """
    p = _to_float(protein_grams)
    if p is None or p < 0:
        return 0.0
    return p


def _coerce_price(price: Any) -> float:
    """
    Price must be > 0 to contribute.
    Invalid/non-positive -> 0.0
    """
    p = _to_float(price)
    if p is None or p <= 0:
        return 0.0
    return p


def parse_piece_quantity(text: str) -> Optional[int]:
    """
    Extract quantities like "20 pc", "40pcs" etc.
    """
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
    """
    Lightweight classifier for non-food items.
    """
    n = (name or "").lower()
    if any(k in n for k in MERCH_KEYWORDS):
        return "merch"
    if any(k in n for k in SAUCE_KEYWORDS):
        return "sauce"
    if any(k in n for k in DRINK_KEYWORDS):
        return "drink"
    if any(
        k in n
        for k in (
            "nugget",
            "burger",
            "chicken",
            "sandwich",
            "wrap",
            "combo",
            "meal",
            "wing",
            "fries",
            "taco",
            "burrito",
            "pizza",
            "salad",
            "rice",
            "bowl",
            "tenders",
            "nuggets",
        )
    ):
        return "food"
    return "unknown"


def estimate_nugget_nutrition(name: str) -> Optional[Dict[str, float]]:
    qty = parse_piece_quantity(name)
    if qty is None or qty <= 0:
        return None
    if "nugget" not in (name or "").lower():
        return None
    calories = qty * NUGGET_CAL_PER_PIECE
    protein = qty * NUGGET_PROTEIN_PER_PIECE
    return {"calories": calories, "protein_grams": protein}


def estimate_nutrition_heuristic(name: str, category: Optional[str] = None, description: str = "") -> Optional[Dict[str, float]]:
    """
    Deterministic nutrition estimates for common fast-food items.
    Returns None when not confident.
    """
    n = (name or "").lower()
    cat = (category or "").lower()
    desc = (description or "").lower()
    # Merch/sauce/drink handled upstream
    est = estimate_nugget_nutrition(name)
    if est:
        return est

    if "burger" in n or "cheeseburger" in n or "sandwich" in n or "chicken" in n or "patty" in n:
        return {"calories": BURGER_CAL, "protein_grams": BURGER_PROT}
    if "wrap" in n:
        return {"calories": WRAP_CAL, "protein_grams": WRAP_PROT}
    if "burrito" in n:
        return {"calories": BURRITO_CAL, "protein_grams": BURRITO_PROT}
    if "taco" in n:
        return {"calories": TACO_CAL, "protein_grams": TACO_PROT}
    if "fries" in n or "fry" in n:
        return {"calories": FRIES_CAL, "protein_grams": FRIES_PROT}
    if "salad" in n:
        return {"calories": SALAD_CAL, "protein_grams": SALAD_PROT}

    if "combo" in n or "meal" in n:
        return {"calories": TYPICAL_MEAL_CALORIES, "protein_grams": TYPICAL_MEAL_PROTEIN}

    # fallback: no confident estimate
    return None


def calculate_satiety_score(calories: Any, protein_grams: Any) -> float:
    """
    Saturating satiety score with diminishing returns.

    We weight calories more than protein (70/30), scale by typical-meal anchors,
    then pass through 1 - exp(-x) to avoid instant 100s for ~1000–1600 kcal meals.

    Returns: 0.0–100.0
    """
    cals = _coerce_calories(calories)
    prot = _coerce_protein(protein_grams)

    if cals <= 0:
        return 0.0

    # Weighted normalized inputs
    cal_component = (cals / float(TYPICAL_MEAL_CALORIES)) * 0.7
    protein_component = (prot / float(TYPICAL_MEAL_PROTEIN)) * 0.3
    raw = cal_component + protein_component

    # Saturation: 0 -> 0, ~1 -> ~63, 2 -> ~86, 3 -> ~95
    score = (1.0 - math.exp(-raw)) * 100.0
    score = _clamp(score)

    logger.info(
        "📊 Satiety: cals=%s prot=%s cal_norm=%.2f prot_norm=%.2f raw=%.2f score=%.1f/100",
        cals,
        prot,
        cal_component,
        protein_component,
        raw,
        score,
    )
    return round(score, 1)


def calculate_price_efficiency_score(price: Any, calories: Any) -> float:
    """
    Soft map of price-per-calorie ratio to 0–100 without hard-capping at typical.

    r = typical_ppc / deal_ppc
    - r = 1.0 (typical)  -> 50
    - r = 2.0 (half ppc) -> 100
    - r = 0.5 (double)   -> 25

    Returns: 0.0–100.0
    """
    p = _coerce_price(price)
    cals = _coerce_calories(calories)

    if p <= 0 or cals <= 0:
        return 0.0

    deal_ppc = p / float(cals)
    if deal_ppc <= 0:
        return 0.0

    r = TYPICAL_PRICE_PER_CALORIE / deal_ppc
    score = _clamp(r * 50.0)

    logger.info(
        "💰 Price efficiency: price=%.2f cals=%s deal_ppc=%.6f r=%.2f score=%.1f/100",
        p,
        cals,
        deal_ppc,
        r,
        score,
    )
    return round(score, 1)


def calculate_final_value_score(calories: Any, protein_grams: Any, price: Any) -> Dict[str, float]:
    """
    Final = 40% satiety + 60% price-efficiency

    Always returns a dict with stable numeric fields.
    """
    cals = _coerce_calories(calories)
    prot = _coerce_protein(protein_grams)
    p = _coerce_price(price)

    satiety = calculate_satiety_score(cals, prot)
    price_eff = calculate_price_efficiency_score(p, cals)

    final_score = satiety * 0.4 + price_eff * 0.6

    price_per_calorie = (p / float(cals)) if (p > 0 and cals > 0) else 0.0

    logger.info(
        "⭐ Final score: satiety=%.1f×0.4 + price_eff=%.1f×0.6 = %.1f/100 (ppc=%.6f)",
        satiety,
        price_eff,
        final_score,
        price_per_calorie,
    )

    return {
        "value_score": round(final_score, 1),
        "satiety_score": round(satiety, 1),
        "price_per_calorie": round(price_per_calorie, 6),
        "price_efficiency_score": round(price_eff, 1),
    }
