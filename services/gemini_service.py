import google.generativeai as genai
from config import get_settings
import logging
import re
import json
from services.rate_limiter import gemini_rate_limiter
from services.value_calculator import calculate_final_value_score

logger = logging.getLogger(__name__)
settings = get_settings()

# Configure Gemini
logger.info(f"Configuring Gemini with API key: {settings.gemini_api_key[:20] if settings.gemini_api_key else 'NOT SET'}...")
genai.configure(api_key=settings.gemini_api_key)


class GeminiService:
    def __init__(self):
        self.model = genai.GenerativeModel('gemini-2.5-flash')

    async def score_deal(
        self,
        item_name: str,
        restaurant_name: str,
        price: float,
        calories: int = None,
        protein_grams: float = None,
        description: str = "",
        portion_size: str = "",
        deal_type: str = ""
    ) -> dict:
        """
        Calculate value score from nutrition data.
        If nutrition data is missing, estimate using Gemini.

        Returns:
            dict with value_score, satiety_score, calories, protein_grams, price_per_calorie
        """
        logger.info(f"🤖 Analyzing deal: {item_name} from {restaurant_name} at ${price}")

        # If nutrition data is provided, use it directly (no API call!)
        if calories and calories > 0:
            if protein_grams is None:
                protein_grams = 0  # Default to 0 if not provided

            logger.info(f"✅ Using provided nutrition: {calories} cal, {protein_grams}g protein")

            # Calculate value score directly
            scores = calculate_final_value_score(calories, protein_grams, price)

            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": calories,
                "protein_grams": protein_grams
            }

        # Otherwise, estimate using Gemini
        logger.warning(f"⚠️ No nutrition data provided, estimating with Gemini...")

        try:
            # Respect rate limits
            await gemini_rate_limiter.acquire()

            prompt = f"""Estimate the nutritional content of this fast food item/deal:

Item: {item_name}
Restaurant: {restaurant_name}
Description: {description or 'No additional description'}
Portion Size: {portion_size or 'Standard'}

Based on typical {restaurant_name} items and the description, estimate:
1. Total calories (all items combined if it's a meal/combo)
2. Total protein in grams (all items combined)

Be realistic based on actual fast food nutrition data.

Return ONLY a JSON object with this exact format:
{{"calories": <number>, "protein": <number>}}

Example responses:
{{"calories": 1200, "protein": 45}}
{{"calories": 650, "protein": 28}}"""

            logger.info(f"📡 Calling Gemini API for nutrition estimation...")
            response = self.model.generate_content(prompt)
            logger.info(f"✅ Gemini responded!")

            # Extract JSON from response
            response_text = response.text.strip()
            logger.info(f"📝 Gemini response: {response_text}")

            # Try to parse JSON
            # Remove markdown code blocks if present
            response_text = re.sub(r'```json\s*|\s*```', '', response_text)

            nutrition_data = json.loads(response_text)

            calories = int(nutrition_data.get('calories', 0))
            protein = float(nutrition_data.get('protein', 0))

            if calories <= 0:
                logger.error(f"❌ Invalid calories: {calories}")
                # Use reasonable defaults
                calories = 600
                protein = 20

            logger.info(f"🍔 Estimated nutrition: {calories} cal, {protein}g protein")

            # Calculate value score using our formula
            scores = calculate_final_value_score(calories, protein, price)

            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": calories,
                "protein_grams": protein
            }

        except json.JSONDecodeError as e:
            logger.error(f"❌ Failed to parse Gemini JSON response: {e}")
            logger.error(f"Response was: {response_text}")
            # Use defaults
            calories, protein = 600, 20
            scores = calculate_final_value_score(calories, protein, price)
            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": calories,
                "protein_grams": protein
            }

        except Exception as e:
            logger.error(f"Error scoring deal with Gemini: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Full error details:", exc_info=True)
            # Return defaults
            calories, protein = 600, 20
            scores = calculate_final_value_score(calories, protein, price)
            return {
                "value_score": scores["value_score"],
                "satiety_score": scores["satiety_score"],
                "price_per_calorie": scores["price_per_calorie"],
                "calories": calories,
                "protein_grams": protein
            }


# Singleton instance
gemini_service = GeminiService()
