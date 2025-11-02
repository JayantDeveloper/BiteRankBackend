from pydantic import BaseModel, Field, HttpUrl
from datetime import datetime
from typing import Optional, List


class DealBase(BaseModel):
    restaurant_name: str = Field(..., min_length=1, max_length=100)
    item_name: str = Field(..., min_length=1, max_length=200)
    price: float = Field(..., gt=0)
    description: Optional[str] = None
    image_url: Optional[str] = None
    portion_size: Optional[str] = None
    category: Optional[str] = None
    deal_type: Optional[str] = None
    # Nutrition data (required for accurate value scoring)
    calories: Optional[int] = Field(None, gt=0, description="Total calories")
    protein_grams: Optional[float] = Field(None, ge=0, description="Protein in grams")


class DealCreate(DealBase):
    pass


class DealUpdate(BaseModel):
    restaurant_name: Optional[str] = Field(None, min_length=1, max_length=100)
    item_name: Optional[str] = Field(None, min_length=1, max_length=200)
    price: Optional[float] = Field(None, gt=0)
    description: Optional[str] = None
    image_url: Optional[str] = None
    portion_size: Optional[str] = None
    category: Optional[str] = None
    deal_type: Optional[str] = None
    is_active: Optional[bool] = None


class DealResponse(DealBase):
    id: int
    value_score: float
    satiety_score: float
    price_per_calorie: float
    calories: Optional[int]
    protein_grams: Optional[float]
    source_price_vendor: Optional[str] = None
    store_external_id: Optional[str] = None
    price_retrieved_at: Optional[datetime] = None
    location: Optional[str] = None
    is_active: bool
    created_at: datetime
    last_ranked_at: Optional[datetime]

    class Config:
        from_attributes = True


class RankingResponse(BaseModel):
    deal_id: int
    item_name: str
    previous_score: float
    new_score: float
    success: bool
    error: Optional[str] = None


class UberEatsImportRequest(BaseModel):
    location: str = Field(..., min_length=2, description="User-provided location (ZIP or city)")
    restaurants: Optional[List[str]] = Field(
        None,
        description="Optional list of restaurants to import. Defaults to supported chains.",
    )
    store_urls: Optional[List[HttpUrl]] = Field(
        None,
        description="Optional list of explicit store URLs to import in addition to discovered stores.",
    )
    auto_rank: bool = True
