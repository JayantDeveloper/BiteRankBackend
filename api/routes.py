"""
api/routes.py: FastAPI endpoints for deals and Uber Eats scraping.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, desc, select, asc, case, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker, get_db
from models import Deal, ScrapeJob
from schemas import (
    DealCreate,
    DealUpdate,
    DealResponse,
    RankingResponse,
    UberEatsImportRequest,
    LocationSuggestionResponse,
    ScrapeJobResponse,
)
from services.menu_scraper import menu_scraper
from services.ubereats_scraper import ubereats_scraper
from services.ubereats_store_search import ubereats_store_search
from services.value_calculator import (
    calculate_final_value_score,
    classify_item_category,
    estimate_nugget_nutrition,
    estimate_nutrition_heuristic,
)
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

SUPPORTED_UBER_EATS_RESTAURANTS = [
    "McDonald's",
    "Taco Bell",
    "KFC",
]
STORE_TIMEOUT = 120
STORE_CONCURRENCY = 3
UBEREATS_CACHE = {}


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


def _compute_score_without_ai(
    *,
    item_name: str,
    restaurant_name: str,
    price: float,
    calories: Optional[int],
    protein_grams: Optional[float],
    category: str = "",
    description: str = "",
) -> Optional[dict]:
    """
    Deterministic scoring: use provided nutrition, else heuristics. Returns None when unavailable.
    """
    if price is None or price <= 0:
        return None

    cal = calories if calories and calories > 0 else None
    protein = protein_grams if protein_grams is not None else 0.0

    if cal is None or cal <= 0:
        est = estimate_nutrition_heuristic(item_name, category=category, description=description)
        if est and est.get("calories"):
            cal = est["calories"]
            protein = est.get("protein_grams", protein)

    if cal is None or cal <= 0:
        return None

    scores = calculate_final_value_score(cal, protein or 0.0, price)
    scores["calories"] = cal
    scores["protein_grams"] = protein or 0.0
    return scores


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


def _cache_key(location: str, restaurants: List[str]) -> str:
    loc_norm = (location or "").strip().lower()
    rest_norm = "|".join(sorted(r.strip().lower() for r in (restaurants or [])))
    return f"{loc_norm}__{rest_norm}"

def _normalize_item_name(name: Optional[str]) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


@router.get("/deals", response_model=List[DealResponse])
async def get_deals(
    restaurant: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(default=10, le=500),
    sort_by: str = Query(
        default="value_score",
        description="value_score|price|price_per_calorie|price_per_protein|protein_grams|calories",
    ),
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Get deals with optional filtering"""
    query = select(Deal)

    filters = []
    if active_only:
        filters.append(Deal.is_active == True)
    filters.append(Deal.price > 0)
    if restaurant:
        filters.append(Deal.restaurant_name == restaurant)
    if category:
        filters.append(Deal.category == category)

    if filters:
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
        raise HTTPException(
            status_code=400,
            detail="Invalid sort_by. Use one of: value_score, price, price_per_calorie, price_per_protein, protein_grams, calories.",
        )

    query = query.order_by(sort_options[sort_by]).limit(limit)

    result = await db.execute(query)
    deals = result.scalars().all()

    return deals


@router.get("/deals/top", response_model=List[DealResponse])
async def get_top_deals(
    limit: int = Query(default=10, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Get top N deals by value score"""
    query = (
        select(Deal)
        .where(Deal.is_active == True)
        .where(Deal.price > 0)
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
            provided_calories = deal.calories
            provided_protein = deal.protein_grams
            result = _compute_score_without_ai(
                item_name=deal.item_name,
                restaurant_name=deal.restaurant_name,
                price=deal.price,
                calories=provided_calories,
                protein_grams=provided_protein,
                category=deal.category or "",
                description=deal.description or "",
            )
            if result:
                _apply_score_results(
                    deal,
                    result,
                    provided_calories=provided_calories,
                    provided_protein=provided_protein,
                )
                logger.info("Deal auto-ranked (heuristic): %s = %s", deal.item_name, result["value_score"])
            else:
                logger.warning("Skipping ranking (missing nutrition and no heuristic) for %s", deal.item_name)
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
    """Rank a single deal using deterministic nutrition or heuristics"""
    result = await db.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    previous_score = deal.value_score

    try:
        result = _compute_score_without_ai(
            item_name=deal.item_name,
            restaurant_name=deal.restaurant_name,
            price=deal.price,
            calories=deal.calories,
            protein_grams=deal.protein_grams,
            category=deal.category or "",
            description=deal.description or "",
        )
        if not result:
            raise ValueError("Missing nutrition; unable to rank without AI")

        new_score = result["value_score"]
        _apply_score_results(
            deal,
            result,
            provided_calories=deal.calories,
            provided_protein=deal.protein_grams,
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
    """Rank all deals using deterministic nutrition or heuristics"""
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
            result = _compute_score_without_ai(
                item_name=d.item_name,
                restaurant_name=d.restaurant_name,
                price=d.price,
                calories=d.calories,
                protein_grams=d.protein_grams,
                category=d.category or "",
                description=d.description or "",
            )
            if not result:
                raise ValueError("Missing nutrition; unable to rank without AI")

            new_score = result["value_score"]
            _apply_score_results(
                d,
                result,
                provided_calories=d.calories,
                provided_protein=d.protein_grams,
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
            key = (deal.restaurant_name.lower(), _normalize_item_name(deal.item_name))
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

            key = (restaurant_name.lower(), _normalize_item_name(name))
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

                    scores = _compute_score_without_ai(
                        item_name=deal.item_name,
                        restaurant_name=deal.restaurant_name,
                        price=deal.price,
                        calories=calories_arg,
                        protein_grams=protein_arg,
                        category=deal.category or "",
                        description=deal.description or "",
                    )
                    if scores:
                        _apply_score_results(
                            deal,
                            scores,
                            provided_calories=calories_arg,
                            provided_protein=protein_arg,
                        )
                        ranked += 1
                    else:
                        skipped.append(
                            {
                                "restaurant": restaurant_name,
                                "item": name,
                                "reason": "missing_nutrition",
                            }
                        )
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

@router.post("/scrape/ubereats", response_model=ScrapeJobResponse, status_code=202)
async def import_ubereats_menus(
    payload: UberEatsImportRequest,
    mode: str = Query(default="async", description="async (default) or sync"),
    db: AsyncSession = Depends(get_db),
):
    """
    Default: enqueue async job and return job id (202).
    mode=sync: run inline, respecting per-store timeouts.

    Examples:
      curl -X POST http://localhost:8000/api/scrape/ubereats -H "Content-Type: application/json" -d '{"location":"21044","restaurants":["McDonald'\''s"],"auto_rank":false}'
      curl http://localhost:8000/api/scrape/ubereats/jobs/<job_id>
      curl -X POST "http://localhost:8000/api/scrape/ubereats?mode=sync" -H "Content-Type: application/json" -d '{"location":"21044"}'
    """
    cache_key = None
    cache_entry = None
    restaurants_list = (
        payload.restaurants if payload.restaurants else SUPPORTED_UBER_EATS_RESTAURANTS
    )
    if payload.location:
        cache_key = _cache_key(payload.location, restaurants_list)
        cached = UBEREATS_CACHE.get(cache_key)
        if cached and cached.get("expires_at") and cached["expires_at"] > datetime.utcnow():
            cache_entry = cached

    if cache_entry and mode != "sync":
        # Return cached job status/result without starting new work
        return ScrapeJobResponse(
            job_id=cache_entry.get("job_id"),
            status=cache_entry.get("status"),
            progress=cache_entry.get("progress"),
            result=cache_entry.get("result"),
        )

    job = ScrapeJob(status="queued", request_json=json.dumps(payload.model_dump()))
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if cache_key:
        UBEREATS_CACHE[cache_key] = {
            "job_id": job.id,
            "status": "queued",
            "progress": None,
            "result": None,
            "expires_at": datetime.utcnow() + timedelta(seconds=settings.ubereats_cache_ttl_seconds),
        }

    if mode == "sync":
        result = await _run_ubereats_job(job.id, payload)
        response = ScrapeJobResponse(
            job_id=job.id,
            status=result.get("status"),
            progress=result.get("progress"),
            result=result.get("result"),
        )
        return response

    asyncio.create_task(_run_ubereats_job(job.id, payload))
    return ScrapeJobResponse(job_id=job.id, status="queued")


@router.get("/scrape/ubereats/jobs/{job_id}", response_model=ScrapeJobResponse)
async def get_ubereats_job(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return ScrapeJobResponse(
        job_id=job.id,
        status=job.status,
        progress=json.loads(job.progress_json) if job.progress_json else None,
        result=json.loads(job.result_json) if job.result_json else None,
    )


async def _update_job(job_id: str, **fields):
    async with async_session_maker() as session:
        res = await session.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
        job = res.scalar_one_or_none()
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await session.commit()


async def _run_ubereats_job(job_id: str, payload: UberEatsImportRequest):
    """
    Run UberEats scraping job. Never raises HTTPException (runs in background).
    Converts all errors to status="failed" in result.
    """
    started = datetime.utcnow()
    progress = {"stage": "starting", "total_stores": 0, "completed": 0, "failed": 0, "stores": []}
    result_payload = {"ranked_deals": [], "unranked_deals": [], "metadata": {}}
    status = "running"
    await _update_job(job_id, status="running", started_at=started)

    try:
        async with async_session_maker() as session:
            await session.execute(delete(Deal).where(Deal.deal_type == "Uber Eats Menu"))
            await session.commit()

        progress["stage"] = "finding_stores"
        await _update_job(job_id, progress_json=json.dumps(progress))

        restaurants_to_fetch = (
            payload.restaurants if payload.restaurants else SUPPORTED_UBER_EATS_RESTAURANTS
        )
        max_restaurants = settings.ubereats_max_restaurants or 8
        if len(restaurants_to_fetch) > max_restaurants:
            status = "failed"
            progress["stage"] = "failed"
            progress["stores"].append(
                {
                    "status": "failed",
                    "error": f"Too many restaurants ({len(restaurants_to_fetch)}). Max {max_restaurants}.",
                }
            )
            return {"status": status, "progress": progress, "result": result_payload}

        store_targets: List[dict] = []

        if payload.store_urls:
            for idx, store_url in enumerate(payload.store_urls):
                restaurant_name = (
                    restaurants_to_fetch[idx]
                    if idx < len(restaurants_to_fetch)
                    else "Uber Eats Store"
                )
                store_url_str = str(store_url)
                store_targets.append(
                    {
                        "restaurant": restaurant_name,
                        "store_url": store_url_str,
                        "store_external_id": _extract_store_id_from_url(store_url_str),
                        "store_name": restaurant_name,
                        "source": "manual",
                    }
                )

        if payload.location:
            debug = settings.ubereats_debug
            store_limit = max(1, settings.ubereats_store_limit)
            total_restaurants = len(restaurants_to_fetch)
            found_count = 0

            async def _on_store_found(restaurant: str, stores) -> None:
                nonlocal found_count
                found_count += 1
                if stores:
                    for store in stores:
                        store_targets.append(
                            {
                                "restaurant": restaurant,
                                "store_url": store.store_url,
                                "store_external_id": store.store_id,
                                "store_name": store.name,
                                "source": "auto",
                            }
                        )
                    progress["stores"].append(
                        {"restaurant": restaurant, "status": "found", "store_url": stores[0].store_url}
                    )
                else:
                    progress["failed"] += 1
                    progress["stores"].append(
                        {
                            "restaurant": restaurant,
                            "status": "failed",
                            "error": f"no stores found near {payload.location}",
                        }
                    )
                # Update finding_stores sub-progress so frontend bar can advance
                progress["finding_stores_done"] = found_count
                progress["finding_stores_total"] = total_restaurants
                await _update_job(job_id, progress_json=json.dumps(progress))

            await ubereats_store_search.search_stores_bulk(
                restaurants=restaurants_to_fetch,
                location=payload.location,
                limit=store_limit,
                debug=debug,
                slow_mo_ms=settings.ubereats_slow_mo_ms,
                screenshots=settings.ubereats_screenshots,
                progress_callback=_on_store_found,
            )

        max_total_stores = settings.ubereats_max_total_stores or 10
        if len(store_targets) > max_total_stores:
            status = "failed"
            progress["stage"] = "failed"
            progress["stores"].append(
                {
                    "status": "failed",
                    "error": f"Too many stores ({len(store_targets)}). Max {max_total_stores}.",
                }
            )
            return {"status": status, "progress": progress, "result": result_payload}

        if not store_targets:
            status = "failed"
            progress["stage"] = "failed"
            progress["stores"].append(
                {"status": "failed", "error": "No Uber Eats stores to scrape."}
            )
            return {"status": status, "progress": progress, "result": result_payload}

        progress["stage"] = "scraping_menus"
        progress["total_stores"] = len(store_targets)
        progress["stores_found"] = len(store_targets)
        progress_lock = asyncio.Lock()
        sem = asyncio.Semaphore(STORE_CONCURRENCY)
        await _update_job(job_id, progress_json=json.dumps(progress))

        async with ubereats_scraper.shared_context() as scrape_ctx:
            # Start tracing at job level if enabled
            trace_path = None
            if settings.ubereats_trace:
                import os
                import time

                os.makedirs("traces", exist_ok=True)
                trace_path = os.path.join(
                    "traces",
                    f"ubereats-job-{job_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip",
                )
                await scrape_ctx.tracing.start(screenshots=True, snapshots=True, sources=True)

            async def process_target(target: dict):
                async with sem:
                    store_url = target["store_url"]
                    restaurant_name = target["restaurant"]
                    store_result = {"restaurant": restaurant_name, "store_url": store_url}
                    try:
                        items = await asyncio.wait_for(
                            ubereats_scraper.fetch_menu(
                                store_url,
                                restaurant_name=restaurant_name,
                                location=payload.location or "",
                                shared_context=scrape_ctx,
                            ),
                            timeout=STORE_TIMEOUT,
                        )
                        async with async_session_maker() as session_local:
                            ranked, unranked = await _persist_and_rank_items(
                                session_local,
                                items,
                                restaurant_name,
                                store_url,
                                payload.location or "",
                                auto_rank=payload.auto_rank,
                            )
                            await session_local.commit()
                        result_payload["ranked_deals"].extend(ranked)
                        result_payload["unranked_deals"].extend(unranked)
                        store_result["status"] = "completed"
                        store_result["items"] = len(items)
                        async with progress_lock:
                            progress["completed"] += 1
                            progress["stores"].append(store_result)
                    except asyncio.TimeoutError:
                        logger.error("Timeout scraping %s", store_url)
                        store_result["status"] = "failed"
                        store_result["error"] = "timeout"
                        async with progress_lock:
                            progress["failed"] += 1
                            progress["stores"].append(store_result)
                    except Exception as exc:
                        logger.error("Error scraping %s: %s", store_url, exc)
                        store_result["status"] = "failed"
                        store_result["error"] = str(exc)
                        async with progress_lock:
                            progress["failed"] += 1
                            progress["stores"].append(store_result)
                    await _update_job(job_id, progress_json=json.dumps(progress))

            await asyncio.gather(*[process_target(t) for t in store_targets])

            # Stop trace if running
            if trace_path:
                try:
                    await scrape_ctx.tracing.stop(path=trace_path)
                    logger.info("Trace saved to %s", trace_path)
                except Exception:
                    pass

        progress["stage"] = "finalizing"
        status = (
            "completed"
            if progress["failed"] == 0
            else ("partial" if progress["completed"] > 0 else "failed")
        )
        result_payload["metadata"] = {"stores_attempted": len(store_targets)}

    except Exception as exc:
        logger.exception("Fatal error in _run_ubereats_job: %s", exc)
        status = "failed"
        progress["stores"].append({"status": "failed", "error": str(exc)})

    finally:
        finished = datetime.utcnow()
        await _update_job(
            job_id,
            status=status,
            finished_at=finished,
            progress_json=json.dumps(progress),
            result_json=json.dumps(result_payload),
        )
        # update cache if applicable
        restaurants_list = (
            payload.restaurants if payload.restaurants else SUPPORTED_UBER_EATS_RESTAURANTS
        )
        if payload.location:
            cache_key = _cache_key(payload.location, restaurants_list)
            UBEREATS_CACHE[cache_key] = {
                "job_id": job_id,
                "status": status,
                "progress": progress,
                "result": result_payload,
                "expires_at": datetime.utcnow() + timedelta(seconds=settings.ubereats_cache_ttl_seconds),
            }

    return {"status": status, "progress": progress, "result": result_payload}


async def _persist_and_rank_items(
    session: AsyncSession,
    items,
    restaurant_name: str,
    store_url: str,
    location: str,
    auto_rank: bool,
):
    ranked: List[dict] = []
    unranked: List[dict] = []

    existing_stmt = select(Deal).where(
        (Deal.restaurant_name == restaurant_name)
        & (Deal.store_external_id == _extract_store_id_from_url(store_url))
    )
    existing_result = await session.execute(existing_stmt)
    existing_map = {_normalize_item_name(d.item_name): d for d in existing_result.scalars().all()}

    for item in items:
        if item.price is None or item.price <= 0:
            unranked.append(
                {
                    "restaurant": restaurant_name,
                    "item": item.name,
                    "store_url": store_url,
                    "reason": "invalid_price",
                }
            )
            continue

        category = classify_item_category(item.name)
        if category in ("merch", "sauce", "drink"):
            unranked.append(
                {
                    "restaurant": restaurant_name,
                    "item": item.name,
                    "store_url": store_url,
                    "reason": category,
                }
            )
            continue

        calories = item.calories
        protein = item.protein_grams

        # Try nugget estimation if calories missing
        if calories is None or calories <= 0:
            est = estimate_nugget_nutrition(item.name)
            if est:
                calories = int(est["calories"])
                protein = float(est["protein_grams"])
            else:
                est2 = estimate_nutrition_heuristic(item.name, category=item.category, description=item.category or "")
                if est2 and est2.get("calories"):
                    calories = est2["calories"]
                    protein = est2.get("protein_grams")

        # Decide whether to rank
        scores = None

        if calories and calories > 0:
            scores = calculate_final_value_score(calories, protein or 0.0, item.price)
            scores["calories"] = int(calories)
            scores["protein_grams"] = float(protein or 0.0)
        else:
            unranked.append(
                {
                    "restaurant": restaurant_name,
                    "item": item.name,
                    "store_url": store_url,
                    "reason": "missing_nutrition",
                }
            )
            continue

        key = _normalize_item_name(item.name)
        deal = existing_map.get(key)
        if deal is None:
            deal = Deal(
                restaurant_name=restaurant_name,
                item_name=item.name,
                price=item.price,
                category=item.category,
                deal_type="Uber Eats Menu",
                calories=scores.get("calories") if scores and scores.get("calories") is not None else calories,
                protein_grams=scores.get("protein_grams") if scores and scores.get("protein_grams") is not None else protein,
                source_price_vendor=item.source_price_vendor,
                store_external_id=item.store_external_id or _extract_store_id_from_url(store_url),
                price_retrieved_at=item.price_retrieved_at,
                location=location,
                is_active=True,
                value_score=scores.get("value_score", 0.0),
                satiety_score=scores.get("satiety_score", 0.0),
                price_per_calorie=scores.get("price_per_calorie", 0.0),
            )
            session.add(deal)
            existing_map[key] = deal
        else:
            deal.price = item.price
            deal.category = item.category
            if scores and scores.get("calories") is not None:
                deal.calories = scores["calories"]
            elif calories:
                deal.calories = calories
            if scores and scores.get("protein_grams") is not None:
                deal.protein_grams = scores["protein_grams"]
            elif protein is not None:
                deal.protein_grams = protein
            deal.source_price_vendor = item.source_price_vendor
            deal.store_external_id = item.store_external_id or _extract_store_id_from_url(store_url)
            deal.price_retrieved_at = item.price_retrieved_at
            deal.location = location
            deal.is_active = True
            deal.value_score = scores.get("value_score", deal.value_score or 0.0)
            deal.satiety_score = scores.get("satiety_score", deal.satiety_score or 0.0)
            deal.price_per_calorie = scores.get("price_per_calorie", deal.price_per_calorie or 0.0)

        ranked.append(
            {
                "restaurant": restaurant_name,
                "item": item.name,
                "price": item.price,
                "store_url": store_url,
                "value_score": deal.value_score,
            }
        )
    ranked = sorted(ranked, key=lambda x: x.get("value_score", 0), reverse=True)
    return ranked, unranked


@router.get("/locations/suggest", response_model=List[LocationSuggestionResponse])
async def suggest_locations(query: str = Query(...)):
    return []
