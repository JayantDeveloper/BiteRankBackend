"""Location suggestion and metadata endpoints."""
from __future__ import annotations

import logging
from typing import List

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Deal
from schemas import LocationSuggestionResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/locations/suggest", response_model=List[LocationSuggestionResponse])
async def suggest_locations(query: str = Query(..., min_length=2)):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "addressdetails": 1, "limit": 6, "countrycodes": "us"}
    headers = {"User-Agent": "BiteRank/1.0"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            results = resp.json()
    except Exception as exc:
        logger.warning("Nominatim suggest failed for %r: %s", query, exc)
        return []

    suggestions: List[LocationSuggestionResponse] = []
    seen: set = set()
    for r in results:
        addr = r.get("address", {})
        zip_code = addr.get("postcode")
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")
        state = addr.get("state")
        if zip_code:
            label = zip_code
        elif city and state:
            label = f"{city}, {state}"
        else:
            label = r.get("display_name", "").split(",")[0].strip()
        if not label or label in seen:
            continue
        seen.add(label)
        try:
            suggestions.append(LocationSuggestionResponse(label=label, latitude=float(r["lat"]), longitude=float(r["lon"])))
        except (KeyError, ValueError):
            continue
    return suggestions


@router.get("/restaurants", response_model=List[str])
async def get_restaurants(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal.restaurant_name).where(Deal.is_active == True).distinct())
    return [row[0] for row in result.all()]


@router.get("/categories", response_model=List[str])
async def get_categories(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deal.category).where(Deal.category.isnot(None), Deal.is_active == True).distinct())
    return [row[0] for row in result.all()]
