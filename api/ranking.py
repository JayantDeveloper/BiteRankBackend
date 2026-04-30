"""Ranking endpoints."""
from __future__ import annotations

import asyncio
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Deal
from schemas import RankingResponse
from api.deals import _compute_score, _apply_scores

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/deals/{deal_id}/rank", response_model=RankingResponse)
async def rank_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    previous = deal.value_score
    try:
        scores = _compute_score(
            item_name=deal.item_name,
            restaurant_name=deal.restaurant_name,
            price=deal.price,
            calories=deal.calories,
            protein_grams=deal.protein_grams,
            category=deal.category or "",
            description=deal.description or "",
        )
        if not scores:
            raise ValueError("Cannot score: missing nutrition and no heuristic match")
        _apply_scores(deal, scores, provided_calories=deal.calories, provided_protein=deal.protein_grams)
        await db.commit()
        return RankingResponse(deal_id=deal_id, item_name=deal.item_name, previous_score=previous, new_score=scores["value_score"], success=True)
    except Exception as exc:
        return RankingResponse(deal_id=deal_id, item_name=deal.item_name, previous_score=previous, new_score=previous, success=False, error=str(exc))


@router.post("/deals/rank-all", response_model=List[RankingResponse])
async def rank_all_deals(
    active_only: bool = True,
    batch_size: int = Query(default=10, le=20),
    db: AsyncSession = Depends(get_db),
):
    query = select(Deal)
    if active_only:
        query = query.where(Deal.is_active == True)
    result = await db.execute(query)
    deals = result.scalars().all()
    if not deals:
        return []

    async def rank_one(d: Deal) -> RankingResponse:
        prev = d.value_score
        try:
            scores = _compute_score(
                item_name=d.item_name,
                restaurant_name=d.restaurant_name,
                price=d.price,
                calories=d.calories,
                protein_grams=d.protein_grams,
                category=d.category or "",
                description=d.description or "",
            )
            if not scores:
                raise ValueError("No heuristic match")
            _apply_scores(d, scores, provided_calories=d.calories, provided_protein=d.protein_grams)
            return RankingResponse(deal_id=d.id, item_name=d.item_name, previous_score=prev, new_score=scores["value_score"], success=True)
        except Exception as exc:
            return RankingResponse(deal_id=d.id, item_name=d.item_name, previous_score=prev, new_score=prev, success=False, error=str(exc))

    results: List[RankingResponse] = []
    for i in range(0, len(deals), batch_size):
        batch_results = await asyncio.gather(*[rank_one(d) for d in deals[i:i + batch_size]])
        results.extend(batch_results)

    await db.commit()
    return results
