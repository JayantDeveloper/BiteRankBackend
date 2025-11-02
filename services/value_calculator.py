import logging
logger = logging.getLogger(__name__)

# Baseline values (keep these)
TYPICAL_MEAL_CALORIES = 800
TYPICAL_MEAL_PROTEIN = 30
TYPICAL_MEAL_PRICE = 9.0
TYPICAL_PRICE_PER_CALORIE = TYPICAL_MEAL_PRICE / TYPICAL_MEAL_CALORIES  # ~0.01125

def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

def calculate_satiety_score(calories: int, protein_grams: float) -> float:
    """
    Saturating satiety score with diminishing returns.

    We weight calories more than protein (70/30), scale by typical-meal anchors,
    then pass through 1 - exp(-x) to avoid instant 100s for ~1000–1600 kcal meals.
    """
    if calories is None or calories <= 0:
        return 0.0
    if protein_grams is None or protein_grams < 0:
        protein_grams = 0.0

    # Weighted normalized inputs
    cal_component = (calories / TYPICAL_MEAL_CALORIES) * 0.7
    protein_component = (protein_grams / TYPICAL_MEAL_PROTEIN) * 0.3
    raw = cal_component + protein_component

    # Saturation: 0 -> 0, ~1 -> ~63, 2 -> ~86, 3 -> ~95
    score = (1.0 - pow(2.718281828, -raw)) * 100.0
    score = _clamp(score)

    logger.info(f"📊 Satiety: cal_norm={cal_component:.2f}, prot_norm={protein_component:.2f}, score={score:.1f}/100")
    return round(score, 1)

def calculate_price_efficiency_score(price: float, calories: int) -> float:
    """
    Soft map of price-per-calorie ratio to 0–100 without hard-capping at typical.

    r = typical_ppc / deal_ppc
    - r = 1.0 (typical)  -> 50
    - r = 2.0 (half ppc) -> 100
    - r = 0.5 (double)   -> 25
    """
    if price is None or price <= 0 or calories is None or calories <= 0:
        return 0.0

    deal_ppc = price / calories
    if deal_ppc <= 0:
        return 0.0

    r = TYPICAL_PRICE_PER_CALORIE / deal_ppc
    score = r * 50.0  # linear mapping; equal to typical -> 50
    score = _clamp(score)

    logger.info(f"💰 Price efficiency: deal_ppc=${deal_ppc:.4f}/cal, r={r:.2f}, score={score:.1f}/100")
    return round(score, 1)

def calculate_final_value_score(calories: int, protein_grams: float, price: float) -> dict:
    """
    Final = 40% satiety + 60% price-efficiency (same weights as before).
    """
    satiety = calculate_satiety_score(calories, protein_grams)
    price_eff = calculate_price_efficiency_score(price, calories)
    final_score = satiety * 0.4 + price_eff * 0.6

    logger.info(f"⭐ Final score: {satiety:.1f}×0.4 + {price_eff:.1f}×0.6 = {final_score:.1f}/100")

    return {
        "value_score": round(final_score, 1),
        "satiety_score": round(satiety, 1),
        "price_per_calorie": round(price / calories, 4) if calories and calories > 0 else 0.0,
        "price_efficiency_score": round(price_eff, 1),
    }
