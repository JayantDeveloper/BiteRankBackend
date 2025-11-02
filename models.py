from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.sql import func
from database import Base
from datetime import datetime


class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_name = Column(String(100), nullable=False, index=True)
    item_name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)
    portion_size = Column(String(50), nullable=True)  # e.g., "Large", "Combo", "Single"
    category = Column(String(50), nullable=True)  # e.g., "Burger", "Chicken", "Tacos"
    deal_type = Column(String(50), nullable=True)  # e.g., "App Exclusive", "Regular Menu"

    # Nutrition data (estimated by Gemini)
    calories = Column(Integer, nullable=True)  # Total calories
    protein_grams = Column(Float, nullable=True)  # Protein in grams

    # Value metrics
    value_score = Column(Float, default=0.0, index=True)  # 0-100 final score
    satiety_score = Column(Float, default=0.0)  # Satiety component score
    price_per_calorie = Column(Float, default=0.0)  # $/calorie (lower is better)

    # Pricing metadata
    source_price_vendor = Column(String(50), nullable=True)
    store_external_id = Column(String(100), nullable=True)
    price_retrieved_at = Column(DateTime(timezone=True), nullable=True)
    location = Column(String(200), nullable=True)  # Location where price was retrieved (ZIP, city, etc.)

    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_ranked_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<Deal {self.restaurant_name} - {self.item_name} (${self.price})>"
