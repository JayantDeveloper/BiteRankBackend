"""Gemini nutrition estimation service."""
import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any, Tuple

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from config import get_settings
from services.rate_limiter import gemini_rate_limiter
from services.value_calculator import calculate_final_value_score

logger = logging.getLogger(__name__)
settings = get_settings()

if not settings.gemini_api_key:
    logger.warning("⚠️ GEMINI_API_KEY is not set. Gemini nutrition estimation will fail.")
else:
    genai.configure(api_key=settings.gemini_api_key)


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}", re.MULTILINE)


def _extract_json_object(text: str) -> Dict[str, Any]:
    """
    Extract a JSON object from Gemini output.

    Handles:
    - ```json ... ```
    - extra commentary around the JSON
    - whitespace/newlines
    """
    if not text:
        raise json.JSONDecodeError("empty response", "", 0)

    cleaned = text.strip()

    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    m = _JSON_OBJ_RE.search(cleaned)
    if not m:
        raise json.JSONDecodeError("no JSON object found", cleaned, 0)

    return json.loads(m.group(0))


def _coerce_positive_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        n = int(float(value))
        return n if n > 0 else None
    except (ValueError, TypeError):
        return None


def _coerce_nonneg_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)
        return f if f >= 0 else None
    except (ValueError, TypeError):
        return None


class GeminiService:
    def __init__(self):
        model_name = getattr(settings, "gemini_model", None) or "gemini-2.5-flash"
        self.model = genai.GenerativeModel(model_name)
        self._circuit_open_until: float = 0.0
        self._circuit_cooldown_seconds: int = getattr(settings, "gemini_circuit_cooldown_seconds", 60)
        self._cache_ttl_seconds: int = getattr(settings, "gemini_cache_ttl_seconds", 3600)
        self._nutrition_cache: Dict[Tuple[str, str], Tuple[float, Tuple[int, float]]] = {}
        self._lock = asyncio.Lock()

    def _is_circuit_open(self) -> bool:
        return time.time() < self._circuit_open_until

    def is_circuit_open(self) -> bool:
        return self._is_circuit_open()

    def _open_circuit(self) -> None:
        self._circuit_open_until = time.time() + max(1, self._circuit_cooldown_seconds)
        logger.warning(
            "🚫 Gemini circuit opened for %s seconds due to quota exhaustion",
            self._circuit_cooldown_seconds,
        )

    async def _get_cached_nutrition(self, restaurant: str, item: str) -> Optional[Tuple[int, float]]:
        key = (restaurant.lower().strip(), item.lower().strip())
        now = time.time()
        if key in self._nutrition_cache:
            expires_at, data = self._nutrition_cache[key]
            if expires_at > now:
                logger.info("🗂️ Using cached Gemini nutrition for %s | %s", restaurant, item)
                return data
            self._nutrition_cache.pop(key, None)
        return None

    async def _cache_nutrition(self, restaurant: str, item: str, calories: int, protein: float) -> None:
        key = (restaurant.lower().strip(), item.lower().strip())
        expires_at = time.time() + max(30, self._cache_ttl_seconds)
        self._nutrition_cache[key] = (expires_at, (calories, protein))

    async def _generate_content(self, prompt: str) -> str:
        """
        google.generativeai is sync; run in a thread so we don't block FastAPI's event loop.
        """
        def _call() -> str:
            resp = self.model.generate_content(prompt)
            return (getattr(resp, "text", None) or "").strip()

        return await asyncio.to_thread(_call)

    async def score_deal(
        self,
        item_name: str,
        restaurant_name: str,
        price: float,
        calories: Optional[int] = None,
        protein_grams: Optional[float] = None,
        description: str = "",
        portion_size: str = "",
        deal_type: str = "",
    ) -> dict:
        """
        Calculate value score from nutrition data.
        If nutrition data is missing, estimate using Gemini.

        Returns dict with:
          value_score, satiety_score, calories, protein_grams, price_per_calorie
        Returns None when estimation is unavailable (e.g., circuit open or errors).
        """
        if price is None or price <= 0:
            raise ValueError(f"Invalid price: {price}")

        item_name = (item_name or "").strip()
        restaurant_name = (restaurant_name or "").strip()

        logger.info("🤖 Scoring deal: %s | %s | $%.2f", restaurant_name, item_name, price)

        if calories is not None and calories > 0:
            protein_for_calc = protein_grams if protein_grams is not None else 0.0

            logger.info(
                "✅ Using provided nutrition (no Gemini call): %s cal, %sg protein",
                calories,
                protein_for_calc,
            )

            scores = calculate_final_value_score(calories, protein_for_calc, price)

            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": int(calories),
                "protein_grams": float(protein_for_calc),
            }

        logger.warning("⚠️ Missing calories; estimating with Gemini for %s (%s)", item_name, restaurant_name)

        cached = await self._get_cached_nutrition(restaurant_name, item_name)
        if cached:
            est_calories, est_protein = cached
            scores = calculate_final_value_score(est_calories, est_protein, price)
            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": est_calories,
                "protein_grams": est_protein,
            }

        if self._is_circuit_open():
            logger.warning(
                "⏩ Skipping Gemini call (circuit open due to prior quota exhaustion); using defaults",
            )
            return None

        await gemini_rate_limiter.acquire()

        prompt = f"""Estimate the nutritional content of this fast food item/deal.

Item: {item_name}
Restaurant: {restaurant_name}
Deal Type: {deal_type or "Unknown"}
Description: {description or "No additional description"}
Portion Size: {portion_size or "Standard"}

Estimate:
1) Total calories (integer)
2) Total protein in grams (number)

Return ONLY a JSON object with EXACT keys:
{{"calories": <number>, "protein": <number>}}

Examples:
{{"calories": 1200, "protein": 45}}
{{"calories": 650, "protein": 28}}
"""

        response_text = ""
        try:
            logger.info("📡 Calling Gemini API for nutrition estimation...")
            response_text = await self._generate_content(prompt)
            logger.info("✅ Gemini responded")

            nutrition = _extract_json_object(response_text)

            est_calories = _coerce_positive_int(nutrition.get("calories"))
            est_protein = _coerce_nonneg_float(nutrition.get("protein"))

            if est_calories is None:
                logger.error("❌ Gemini returned invalid calories. Raw=%s | text=%s", nutrition.get("calories"), response_text)
                est_calories = 600

            if est_protein is None:
                est_protein = 20.0

            logger.info("🍔 Estimated nutrition: %s cal, %sg protein", est_calories, est_protein)

            await self._cache_nutrition(restaurant_name, item_name, est_calories, est_protein)

            scores = calculate_final_value_score(est_calories, est_protein, price)

            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": est_calories,
                "protein_grams": est_protein,
            }

        except ResourceExhausted as e:
            retry_delay = None
            try:
                retry_delay = e.retry_delay.total_seconds() if hasattr(e, "retry_delay") else None
            except Exception:
                retry_delay = None
            logger.error(
                "❌ Gemini quota exhausted (429). retry_delay=%s; opening circuit breaker",
                retry_delay,
            )
            self._open_circuit()
        except json.JSONDecodeError as e:
            logger.error("❌ Failed to parse Gemini JSON response: %s", e)
            logger.error("Gemini raw text was: %s", response_text)

        except Exception as e:
            logger.error("❌ Error scoring deal with Gemini: %s", e, exc_info=True)

        return None


gemini_service = GeminiService()
