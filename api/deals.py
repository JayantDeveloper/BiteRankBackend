"""Deal CRUD endpoints."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, asc, case, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Deal
from schemas import DealCreate, DealResponse, DealUpdate, RankingResponse
from services.value_calculator import (
    calculate_final_value_score,
    estimate_nutrition_heuristic,
    estimate_nugget_nutrition,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _normalize_item_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


def _compute_score(
    *,
    item_name: str,
    restaurant_name: str,
    price: float,
    calories: Optional[int],
    protein_grams: Optional[float],
    category: str = "",
    description: str = "",
) -> Optional[dict]:
    """Deterministic scoring with heuristic fallback. Returns None only when price is invalid."""
    if not price or price <= 0:
        return None

    cal = calories if calories and calories > 0 else None
    protein = protein_grams if protein_grams is not None else 0.0

    if cal is None:
        est = estimate_nugget_nutrition(item_name)
        if not est:
            est = estimate_nutrition_heuristic(item_name, category=category, description=description)
        if est and est.get("calories"):
            cal = int(est["calories"])
            protein = float(est.get("protein_grams", protein))

    if not cal or cal <= 0:
        return None

    scores = calculate_final_value_score(cal, protein or 0.0, price)
    scores["calories"] = cal
    scores["protein_grams"] = protein or 0.0
    return scores


def _apply_scores(
    deal: Deal,
    scores: dict,
    *,
    provided_calories: Optional[int] = None,
    provided_protein: Optional[float] = None,
) -> None:
    deal.value_score = scores["value_score"]
    deal.satiety_score = scores["satiety_score"]
    deal.price_per_calorie = scores["price_per_calorie"]
    if provided_calories is not None:
        deal.calories = provided_calories
    elif scores.get("calories") is not None:
        deal.calories = scores["calories"]
    if provided_protein is not None:
        deal.protein_grams = provided_protein
    elif scores.get("protein_grams") is not None:
        deal.protein_grams = scores["protein_grams"]
    deal.last_ranked_at = datetime.utcnow()


@router.get("/deals", response_model=List[DealResponse])
async def get_deals(
    restaurant: Optional[str] = None,
    category: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = Query(default=20, le=500),
    sort_by: str = Query(default="value_score"),
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
):
    query = select(Deal)

    filters = [Deal.price > 0]
    if active_only:
        filters.append(Deal.is_active == True)
    if restaurant:
        filters.append(Deal.restaurant_name == restaurant)
    if category:
        filters.append(Deal.category == category)
    if location:
        filters.append(Deal.location == location)

    query = query.where(and_(*filters))

    price_per_protein = case(
        (Deal.protein_grams.is_(None), 9999999),
        (Deal.protein_grams <= 0, 9999999),
        else_=Deal.price / Deal.protein_grams,
    )
    sort_options = {
        "value_score": desc(Deal.value_score),
        "price": asc(Deal.price),
        "price_per_calorie": asc(Deal.price_per_calorie),
        "price_per_protein": asc(price_per_protein),
        "protein_grams": desc(Deal.protein_grams),
        "calories": asc(Deal.calories),
    }
    if sort_by not in sort_options:
        raise HTTPException(status_code=400, detail=f"Invalid sort_by. Use: {', '.join(sort_options)}")

    query = query.order_by(sort_options[sort_by]).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/deals/top", response_model=List[DealResponse])
async def get_top_deals(limit: int = Query(default=10, le=500), db: AsyncSession = Depends(get_db)):
    query = (
        select(Deal)
        .where(Deal.is_active == True, Deal.price > 0)
        .order_by(desc(Deal.value_score))
        .limit(limit)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/deals/{deal_id}", response_model=DealResponse)
async def get_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deal


@router.post("/deals", response_model=DealResponse, status_code=201)
async def create_deal(
    deal_data: DealCreate,
    auto_rank: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    deal = Deal(**deal_data.model_dump())
    if auto_rank:
        scores = _compute_score(
            item_name=deal.item_name,
            restaurant_name=deal.restaurant_name,
            price=deal.price,
            calories=deal_data.calories,
            protein_grams=deal_data.protein_grams,
            category=deal.category or "",
            description=deal.description or "",
        )
        if scores:
            _apply_scores(deal, scores, provided_calories=deal_data.calories, provided_protein=deal_data.protein_grams)
    db.add(deal)
    await db.commit()
    await db.refresh(deal)
    return deal


@router.put("/deals/{deal_id}", response_model=DealResponse)
async def update_deal(deal_id: int, deal_data: DealUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    for field, value in deal_data.model_dump(exclude_unset=True).items():
        setattr(deal, field, value)
    await db.commit()
    await db.refresh(deal)
    return deal


@router.delete("/deals/{deal_id}", status_code=204)
async def delete_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    await db.delete(deal)
    await db.commit()
