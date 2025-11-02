from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from typing import List, Optional
from datetime import datetime
import logging
import json
import base64
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from database import get_db
from models import Deal
from schemas import (
    DealCreate,
    DealUpdate,
    DealResponse,
    RankingResponse,
    UberEatsImportRequest,
)
from services.gemini_service import gemini_service
from services.menu_scraper import menu_scraper
from services.ubereats_scraper import ubereats_scraper
from services.ubereats_store_search import ubereats_store_search

logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_UBER_EATS_RESTAURANTS = [
    "McDonald's",
    "KFC",
    "Taco Bell",
    "Wendy's",
    "Burger King",
    "Chick-fil-A",
    "Subway",
    "Popeyes",
]


def _build_pl_payload(location: str, lat: float, lon: float) -> str:
    payload = {
        "address": location,
        "latitude": round(lat, 6),
        "longitude": round(lon, 6),
        "reference": f"manual:{round(lat,6)},{round(lon,6)}",
        "referenceType": "manual",
    }
    json_payload = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(json_payload.encode("utf-8")).decode("utf-8").rstrip("=")


def _with_query_params(url: str, **params: str) -> str:
    parts = list(urlparse(url))
    query = dict(parse_qsl(parts[4]))
    query.update({k: v for k, v in params.items() if v is not None})
    parts[4] = urlencode(query)
    return urlunparse(parts)


def _extract_store_id_from_url(url: str) -> str:
    path = urlparse(url).path
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 3 and segments[0] == "store":
        return segments[2]
    if len(segments) >= 2 and segments[0] == "store":
        return segments[1]
    return url


def _apply_score_results(
    deal: Deal,
    scores: dict,
    *,
    provided_calories: Optional[int] = None,
    provided_protein: Optional[float] = None,
) -> None:
    """Persist scoring output while preferring user-supplied nutrition."""
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


def _resolve_scraper_slugs(inputs: Optional[List[str]]) -> List[str]:
    """Normalize restaurant identifiers for scraper endpoints."""
    if not inputs:
        return list(menu_scraper.TARGETS.keys())

    normalized: List[str] = []
    unknown: List[str] = []

    for item in inputs:
        slug = item.strip().lower().replace(" ", "-")
        if slug in menu_scraper.TARGETS:
            normalized.append(slug)
        else:
            unknown.append(item)

    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported restaurants requested: {', '.join(unknown)}",
        )

    return normalized


@router.get("/deals", response_model=List[DealResponse])
async def get_deals(
    restaurant: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(default=10, le=100),
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Get deals with optional filtering"""
    query = select(Deal)

    filters = []
    if active_only:
        filters.append(Deal.is_active == True)
    if restaurant:
        filters.append(Deal.restaurant_name == restaurant)
    if category:
        filters.append(Deal.category == category)

    if filters:
        query = query.where(and_(*filters))

    query = query.order_by(desc(Deal.value_score)).limit(limit)

    result = await db.execute(query)
    deals = result.scalars().all()

    return deals


@router.get("/deals/top", response_model=List[DealResponse])
async def get_top_deals(
    limit: int = Query(default=10, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get top N deals by value score"""
    query = (
        select(Deal)
        .where(Deal.is_active == True)
        .order_by(desc(Deal.value_score))
        .limit(limit)
    )

    result = await db.execute(query)
    deals = result.scalars().all()

    return deals


@router.get("/deals/{deal_id}", response_model=DealResponse)
async def get_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    """Get a specific deal by ID"""
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
    """Create a new deal"""
    deal = Deal(**deal_data.model_dump())

    logger.info(
        "Creating deal: %s at $%.2f, auto_rank=%s, provided cal=%s (%s), protein=%s (%s)",
        deal.item_name,
        deal.price,
        auto_rank,
        deal_data.calories,
        type(deal_data.calories).__name__,
        deal_data.protein_grams,
        type(deal_data.protein_grams).__name__,
    )

    # Optionally rank immediately
    if auto_rank:
        try:
            logger.info("Calling Gemini to rank: %s", deal.item_name)
            provided_calories = deal.calories
            provided_protein = deal.protein_grams
            result = await gemini_service.score_deal(
                item_name=deal.item_name,
                restaurant_name=deal.restaurant_name,
                price=deal.price,
                calories=provided_calories,
                protein_grams=provided_protein,
                description=deal.description or "",
                portion_size=deal.portion_size or "",
                deal_type=deal.deal_type or "",
            )
            _apply_score_results(
                deal,
                result,
                provided_calories=provided_calories,
                provided_protein=provided_protein,
            )
            logger.info("Deal auto-ranked: %s = %s", deal.item_name, result["value_score"])
        except Exception as e:
            logger.error("Error auto-ranking deal: %s", e)

    db.add(deal)
    await db.commit()
    await db.refresh(deal)

    return deal


@router.put("/deals/{deal_id}", response_model=DealResponse)
async def update_deal(
    deal_id: int,
    deal_data: DealUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a deal"""
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    # Update fields
    for field, value in deal_data.model_dump(exclude_unset=True).items():
        setattr(deal, field, value)

    await db.commit()
    await db.refresh(deal)

    return deal


@router.delete("/deals/{deal_id}", status_code=204)
async def delete_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a deal"""
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    await db.delete(deal)
    await db.commit()

    return None


@router.post("/deals/{deal_id}/rank", response_model=RankingResponse)
async def rank_deal(deal_id: int, db: AsyncSession = Depends(get_db)):
    """Rank a single deal using Gemini AI"""
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    previous_score = deal.value_score

    try:
        provided_calories = deal.calories
        provided_protein = deal.protein_grams
        result = await gemini_service.score_deal(
            item_name=deal.item_name,
            restaurant_name=deal.restaurant_name,
            price=deal.price,
            calories=provided_calories,
            protein_grams=provided_protein,
            description=deal.description or "",
            portion_size=deal.portion_size or "",
            deal_type=deal.deal_type or "",
        )

        new_score = result["value_score"]
        _apply_score_results(
            deal,
            result,
            provided_calories=provided_calories,
            provided_protein=provided_protein,
        )

        await db.commit()

        logger.info("Ranked deal %s: %s -> %s", deal_id, previous_score, new_score)

        return RankingResponse(
            deal_id=deal_id,
            item_name=deal.item_name,
            previous_score=previous_score,
            new_score=new_score,
            success=True,
        )
    except Exception as e:
        logger.error("Error ranking deal %s: %s", deal_id, e)
        return RankingResponse(
            deal_id=deal_id,
            item_name=deal.item_name,
            previous_score=previous_score,
            new_score=previous_score,
            success=False,
            error=str(e),
        )


@router.post("/deals/rank-all", response_model=List[RankingResponse])
async def rank_all_deals(
    active_only: bool = True,
    batch_size: int = Query(
        default=10, le=20, description="Number of deals to rank in parallel"
    ),
    db: AsyncSession = Depends(get_db),
):
    """Rank all deals using Gemini AI with parallel processing"""
    import asyncio

    query = select(Deal)
    if active_only:
        query = query.where(Deal.is_active == True)

    result = await db.execute(query)
    deals = result.scalars().all()

    if not deals:
        logger.info("No deals to rank")
        return []

    logger.info(
        "🚀 Starting parallel ranking of %d deals (batch size: %d)",
        len(deals),
        batch_size,
    )

    async def rank_single_deal(d: Deal) -> RankingResponse:
        """Rank a single deal and return result"""
        previous_score = d.value_score

        try:
            provided_calories = d.calories
            provided_protein = d.protein_grams
            result = await gemini_service.score_deal(
                item_name=d.item_name,
                restaurant_name=d.restaurant_name,
                price=d.price,
                calories=provided_calories,
                protein_grams=provided_protein,
                description=d.description or "",
                portion_size=d.portion_size or "",
                deal_type=d.deal_type or "",
            )

            new_score = result["value_score"]
            _apply_score_results(
                d,
                result,
                provided_calories=provided_calories,
                provided_protein=provided_protein,
            )

            logger.info("✅ Ranked deal %s: %s -> %s", d.id, previous_score, new_score)

            return RankingResponse(
                deal_id=d.id,
                item_name=d.item_name,
                previous_score=previous_score,
                new_score=new_score,
                success=True,
            )
        except Exception as e:
            logger.error("❌ Error ranking deal %s: %s", d.id, e)
            return RankingResponse(
                deal_id=d.id,
                item_name=d.item_name,
                previous_score=previous_score,
                new_score=previous_score,
                success=False,
                error=str(e),
            )

    # Process deals in batches for parallel ranking
    results: List[RankingResponse] = []
    for i in range(0, len(deals), batch_size):
        batch = deals[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(deals) + batch_size - 1) // batch_size

        logger.info("📦 Processing batch %d/%d (%d deals)", batch_num, total_batches, len(batch))

        # Rank all deals in this batch in parallel
        batch_results = await asyncio.gather(*[rank_single_deal(d) for d in batch])
        results.extend(batch_results)

        logger.info("✅ Batch %d/%d complete", batch_num, total_batches)

    # Commit all changes at once
    await db.commit()

    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful

    logger.info("🎉 Ranking complete! %d succeeded, %d failed", successful, failed)

    return results


@router.get("/restaurants", response_model=List[str])
async def get_restaurants(db: AsyncSession = Depends(get_db)):
    """Get list of unique restaurant names"""
    query = select(Deal.restaurant_name).distinct()
    result = await db.execute(query)
    restaurants = [row[0] for row in result.all()]

    return restaurants


@router.get("/categories", response_model=List[str])
async def get_categories(db: AsyncSession = Depends(get_db)):
    """Get list of unique categories"""
    query = select(Deal.category).where(Deal.category.isnot(None)).distinct()
    result = await db.execute(query)
    categories = [row[0] for row in result.all()]

    return categories


@router.get("/scrape/menus")
async def scrape_menus(restaurants: Optional[List[str]] = Query(None)):
    """
    Scrape public menu pages for supported restaurants.
    Optionally filter to a subset by providing slugs or restaurant names.
    """
    slugs = _resolve_scraper_slugs(restaurants)

    results = await menu_scraper.scrape_all(slugs)
    return {
        "requested": slugs,
        "available": list(menu_scraper.TARGETS.keys()),
        "results": results,
    }


@router.post("/scrape/import")
async def import_scraped_menus(
    restaurants: Optional[List[str]] = Query(None),
    auto_rank: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """
    Scrape menus and persist results as Deal records.
    Only items with a price are imported. Existing deals are updated in-place.
    """
    slugs = _resolve_scraper_slugs(restaurants)
    scrape_results = await menu_scraper.scrape_all(slugs)

    restaurant_names = [menu_scraper.TARGETS[slug]["restaurant"] for slug in slugs]
    existing_map = {}
    if restaurant_names:
        stmt = select(Deal).where(Deal.restaurant_name.in_(restaurant_names))
        result = await db.execute(stmt)
        for deal in result.scalars().all():
            key = (deal.restaurant_name.lower(), deal.item_name.lower())
            existing_map[key] = deal

    created = 0
    updated = 0
    ranked = 0
    skipped: List[dict] = []

    for slug in slugs:
        items = scrape_results.get(slug, [])
        restaurant_name = menu_scraper.TARGETS[slug]["restaurant"]

        for item in items:
            name = item.get("name")
            price = item.get("price")

            if not name:
                skipped.append(
                    {"restaurant": restaurant_name, "item": None, "reason": "missing name"}
                )
                continue

            if price is None or price <= 0:
                skipped.append(
                    {
                        "restaurant": restaurant_name,
                        "item": name,
                        "reason": "missing or invalid price",
                    }
                )
                continue

            key = (restaurant_name.lower(), name.lower())
            provided_calories = item.get("calories")
            provided_protein = item.get("protein_grams")
            category = item.get("category")

            deal = existing_map.get(key)
            if deal is None:
                deal = Deal(
                    restaurant_name=restaurant_name,
                    item_name=name,
                    price=price,
                    category=category,
                    deal_type="Menu Item",
                    calories=provided_calories,
                    protein_grams=provided_protein,
                    is_active=True,
                )
                db.add(deal)
                existing_map[key] = deal
                created += 1
            else:
                changed = False
                if deal.price != price:
                    deal.price = price
                    changed = True
                if category and deal.category != category:
                    deal.category = category
                    changed = True
                if provided_calories is not None and deal.calories != provided_calories:
                    deal.calories = provided_calories
                    changed = True
                if provided_protein is not None and deal.protein_grams != provided_protein:
                    deal.protein_grams = provided_protein
                    changed = True
                if not deal.is_active:
                    deal.is_active = True
                    changed = True
                if changed:
                    updated += 1

            if auto_rank:
                try:
                    # Prefer freshly provided nutrition when available
                    calories_arg = (
                        provided_calories if provided_calories is not None else deal.calories
                    )
                    protein_arg = (
                        provided_protein
                        if provided_protein is not None
                        else deal.protein_grams
                    )

                    scores = await gemini_service.score_deal(
                        item_name=deal.item_name,
                        restaurant_name=deal.restaurant_name,
                        price=deal.price,
                        calories=calories_arg,
                        protein_grams=protein_arg,
                        description=deal.description or "",
                        portion_size=deal.portion_size or "",
                        deal_type=deal.deal_type or "",
                    )
                    _apply_score_results(
                        deal,
                        scores,
                        provided_calories=calories_arg,
                        provided_protein=protein_arg,
                    )
                    ranked += 1
                except Exception as exc:
                    logger.error(
                        "Failed to auto-rank scraped deal %s - %s: %s",
                        restaurant_name,
                        name,
                        exc,
                    )
                    skipped.append(
                        {
                            "restaurant": restaurant_name,
                            "item": name,
                            "reason": f"auto-rank failed: {exc}",
                        }
                    )

    await db.commit()

    return {
        "requested": slugs,
        "created": created,
        "updated": updated,
        "ranked": ranked,
        "skipped": skipped,
        "total": created + updated,
    }


@router.post("/scrape/ubereats")
async def import_ubereats_menus(
    payload: UberEatsImportRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Import store-specific pricing from Uber Eats.
    We discover nearby stores for the supported chains using the provided location,
    then scrape each store page for pricing.
    """
    if not payload.location and not payload.store_urls:
        raise HTTPException(
            status_code=400,
            detail="Provide either a location or explicit store URLs to import.",
        )

    created = 0
    updated = 0
    ranked = 0
    skipped: List[dict] = []
    stores_processed = []

    restaurants_to_fetch = (
        payload.restaurants if payload.restaurants else SUPPORTED_UBER_EATS_RESTAURANTS
    )

    store_targets: List[dict] = []

    lat = lon = None
    pl_param: Optional[str] = None

    if payload.location:
        lat, lon = await ubereats_store_search.geocode_location(payload.location)
        if lat is None or lon is None:
            raise HTTPException(
                status_code=400,
                detail=f"Could not geocode location '{payload.location}'. Please provide a more precise address or ZIP code.",
            )
        pl_param = _build_pl_payload(payload.location, lat, lon)

    # Add manually supplied store URLs (if any)
    if payload.store_urls:
        for idx, store_url in enumerate(payload.store_urls):
            restaurant_name = (
                restaurants_to_fetch[idx]
                if idx < len(restaurants_to_fetch)
                else "Uber Eats Store"
            )
            enriched_url = str(store_url)
            if pl_param:
                enriched_url = _with_query_params(enriched_url, diningMode="DELIVERY", pl=pl_param)
            store_targets.append(
                {
                    "restaurant": restaurant_name,
                    "store_url": enriched_url,
                    "store_external_id": _extract_store_id_from_url(enriched_url),
                    "store_name": restaurant_name,
                    "latitude": lat,
                    "longitude": lon,
                    "source": "manual",
                }
            )

    # Auto-discover stores based on location
    if payload.location:
        for restaurant in restaurants_to_fetch:
            try:
                stores = await ubereats_store_search.search_stores(
                    restaurant,
                    payload.location,
                    lat=lat,
                    lon=lon,
                )
            except Exception as exc:
                logger.error("Failed to search stores for %s: %s", restaurant, exc)
                skipped.append(
                    {
                        "restaurant": restaurant,
                        "reason": f"store search failed: {exc}",
                    }
                )
                continue

            if not stores:
                skipped.append(
                    {
                        "restaurant": restaurant,
                        "reason": f"no stores found near {payload.location}",
                    }
                )
                continue

            store = stores[0]
            store_url = store.store_url
            if pl_param:
                store_url = _with_query_params(store.store_url, diningMode="DELIVERY", pl=pl_param)
            store_targets.append(
                {
                    "restaurant": restaurant,
                    "store_url": store_url,
                    "store_external_id": store.store_id,
                    "store_name": store.name,
                    "latitude": store.latitude or lat,
                    "longitude": store.longitude or lon,
                    "source": "auto",
                }
            )

    if not store_targets:
        raise HTTPException(
            status_code=404,
            detail="No Uber Eats stores found to import. Check the location and try again.",
        )

    seen_store_keys = set()

    for target in store_targets:
        store_url = target["store_url"]
        restaurant_name = target["restaurant"]

        try:
            items = await ubereats_scraper.fetch_menu(
                store_url,
                restaurant_name=restaurant_name,
                location=payload.location,
            )
        except Exception as exc:
            logger.error("Failed to scrape Uber Eats store %s: %s", store_url, exc)
            skipped.append(
                {
                    "restaurant": restaurant_name,
                    "store_url": store_url,
                    "reason": f"scrape failed: {exc}",
                }
            )
            continue

        if not items:
            skipped.append(
                {
                    "restaurant": restaurant_name,
                    "store_url": store_url,
                    "reason": "no items found",
                }
            )
            continue

        target_store_id = target.get("store_external_id")
        store_external_id = items[0].store_external_id or target_store_id or store_url
        store_key = (restaurant_name.lower(), store_external_id)
        if store_key in seen_store_keys:
            logger.info(
                "Skipping duplicate store import for %s (%s)", restaurant_name, store_external_id
            )
            continue
        seen_store_keys.add(store_key)
        stores_processed.append(
            {
                "restaurant": restaurant_name,
                "store_external_id": store_external_id,
                "store_url": store_url,
                "source": target.get("source", "auto"),
                "store_name": target.get("store_name"),
                "location": payload.location,
                "latitude": target.get("latitude"),
                "longitude": target.get("longitude"),
            }
        )

        existing_stmt = select(Deal).where(
            (Deal.restaurant_name == restaurant_name)
            & (Deal.store_external_id == store_external_id)
        )
        result = await db.execute(existing_stmt)
        existing_deals = {deal.item_name.lower(): deal for deal in result.scalars().all()}

        for item in items:
            key = item.name.lower()
            existing = existing_deals.get(key)
            if existing is None:
                deal = Deal(
                    restaurant_name=restaurant_name,
                    item_name=item.name,
                    price=item.price,
                    category=item.category,
                    deal_type="Uber Eats Menu",
                    source_price_vendor=item.source_price_vendor,
                    store_external_id=item.store_external_id,
                    price_retrieved_at=item.price_retrieved_at,
                    location=item.location or payload.location,
                    is_active=True,
                )
                db.add(deal)
                existing_deals[key] = deal
                existing = deal
                created += 1
            else:
                changed = False
                if existing.price != item.price:
                    existing.price = item.price
                    changed = True
                if item.category and existing.category != item.category:
                    existing.category = item.category
                    changed = True
                if existing.source_price_vendor != item.source_price_vendor:
                    existing.source_price_vendor = item.source_price_vendor
                    changed = True
                if existing.store_external_id != (item.store_external_id or store_external_id):
                    existing.store_external_id = item.store_external_id or store_external_id
                    changed = True
                if existing.price_retrieved_at != item.price_retrieved_at:
                    existing.price_retrieved_at = item.price_retrieved_at
                    changed = True
                if (item.location or payload.location) and existing.location != (
                    item.location or payload.location
                ):
                    existing.location = item.location or payload.location
                    changed = True
                if not existing.is_active:
                    existing.is_active = True
                    changed = True
                if changed:
                    updated += 1

            if payload.auto_rank:
                try:
                    scores = await gemini_service.score_deal(
                        item_name=existing.item_name,
                        restaurant_name=existing.restaurant_name,
                        price=existing.price,
                        calories=existing.calories,
                        protein_grams=existing.protein_grams,
                        description=existing.description or "",
                        portion_size=existing.portion_size or "",
                        deal_type=existing.deal_type or "",
                    )
                    _apply_score_results(
                        existing,
                        scores,
                        provided_calories=existing.calories,
                        provided_protein=existing.protein_grams,
                    )
                    existing.source_price_vendor = item.source_price_vendor
                    existing.store_external_id = item.store_external_id or store_external_id
                    existing.price_retrieved_at = item.price_retrieved_at
                    existing.location = item.location or payload.location
                    ranked += 1
                except Exception as exc:
                    logger.error(
                        "Failed to rank Uber Eats item %s: %s",
                        existing.item_name,
                        exc,
                    )
                    skipped.append(
                        {
                            "restaurant": restaurant_name,
                            "store_url": store_url,
                            "item": item.name,
                            "reason": f"rank failed: {exc}",
                        }
                    )

    await db.commit()

    return {
        "created": created,
        "updated": updated,
        "ranked": ranked,
        "skipped": skipped,
        "stores_processed": stores_processed,
    }
