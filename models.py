from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.sql import func
from database import Base
from datetime import datetime
import uuid


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

    # Nutrition data (estimated by heuristics)
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


class ScrapeJob(Base):
    __tablename__ = "scrape_jobs"

    id = Column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(String(20), nullable=False, default="queued")  # queued|running|partial|completed|failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    request_json = Column(Text, nullable=True)
    progress_json = Column(Text, nullable=True)
    result_json = Column(Text, nullable=True)

    def __repr__(self):
        return f"<ScrapeJob {self.id} status={self.status}>"
