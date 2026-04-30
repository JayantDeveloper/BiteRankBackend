"""Uber Eats scraping endpoints and job orchestration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import async_session_maker, get_db
from models import Deal, ScrapeJob
from schemas import ScrapeJobResponse, UberEatsImportRequest
from services.ubereats_scraper import ubereats_scraper
from services.ubereats_store_search import ubereats_store_search
from services.value_calculator import (
    calculate_final_value_score,
    classify_item_category,
    estimate_nugget_nutrition,
    estimate_nutrition_heuristic,
)

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

SUPPORTED_RESTAURANTS = [
    "McDonald's",
    "Taco Bell",
    "KFC",
    "Burger King",
    "Wendy's",
    "Chick-fil-A",
    "Subway",
    "Popeyes",
]

STORE_TIMEOUT = 120
STORE_CONCURRENCY = 3
UBEREATS_CACHE: Dict[str, dict] = {}

_SCRAPE_API_KEY = os.environ.get("SCRAPE_API_KEY", "")


def _require_scrape_key(x_api_key: Optional[str] = None):
    from fastapi import Header
    if _SCRAPE_API_KEY and x_api_key != _SCRAPE_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key")


def _cache_key(location: str, restaurants: List[str]) -> str:
    loc = (location or "").strip().lower()
    rest = "|".join(sorted(r.strip().lower() for r in (restaurants or [])))
    return f"{loc}__{rest}"


def _store_id_from_url(url: str) -> str:
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 3 and segments[0] == "store":
        return segments[2]
    if len(segments) >= 2 and segments[0] == "store":
        return segments[1]
    return url


def _normalize(name: Optional[str]) -> str:
    if not name:
        return ""
    return " ".join(name.strip().lower().split())


async def _update_job(job_id: str, **fields) -> None:
    async with async_session_maker() as session:
        res = await session.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
        job = res.scalar_one_or_none()
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await session.commit()


@router.post("/scrape/ubereats", response_model=ScrapeJobResponse, status_code=202)
async def import_ubereats_menus(
    payload: UberEatsImportRequest,
    mode: str = Query(default="async"),
    db: AsyncSession = Depends(get_db),
):
    restaurants_list = payload.restaurants or SUPPORTED_RESTAURANTS

    if payload.location:
        key = _cache_key(payload.location, restaurants_list)
        cached = UBEREATS_CACHE.get(key)
        if cached and cached.get("expires_at") and cached["expires_at"] > datetime.utcnow() and mode != "sync":
            return ScrapeJobResponse(job_id=cached["job_id"], status=cached["status"], progress=cached["progress"], result=cached["result"])

    job = ScrapeJob(status="queued", request_json=json.dumps(payload.model_dump()))
    db.add(job)
    await db.commit()
    await db.refresh(job)

    if payload.location:
        key = _cache_key(payload.location, restaurants_list)
        UBEREATS_CACHE[key] = {"job_id": job.id, "status": "queued", "progress": None, "result": None, "expires_at": datetime.utcnow() + timedelta(seconds=settings.ubereats_cache_ttl_seconds)}

    if mode == "sync":
        result = await run_ubereats_job(job.id, payload)
        return ScrapeJobResponse(job_id=job.id, status=result["status"], progress=result["progress"], result=result["result"])

    asyncio.create_task(run_ubereats_job(job.id, payload))
    return ScrapeJobResponse(job_id=job.id, status="queued")


@router.get("/scrape/ubereats/jobs/{job_id}", response_model=ScrapeJobResponse)
async def get_ubereats_job(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ScrapeJob).where(ScrapeJob.id == job_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    progress_data = json.loads(job.progress_json) if job.progress_json else None
    top_error = None
    if job.status == "failed" and progress_data:
        failed_stores = [s for s in (progress_data.get("stores") or []) if s.get("status") == "failed" and s.get("error")]
        if failed_stores:
            top_error = failed_stores[0]["error"]

    return ScrapeJobResponse(
        job_id=job.id,
        status=job.status,
        progress=progress_data,
        result=json.loads(job.result_json) if job.result_json else None,
        error=top_error,
    )


async def run_ubereats_job(job_id: str, payload: UberEatsImportRequest) -> dict:
    """Orchestrate a full Uber Eats scrape job. Never raises — errors become status=failed."""
    started = datetime.utcnow()
    progress = {"stage": "starting", "total_stores": 0, "completed": 0, "failed": 0, "stores": []}
    result_payload: dict = {"ranked_deals": [], "unranked_deals": [], "metadata": {}}
    status = "running"
    await _update_job(job_id, status="running", started_at=started)

    try:
        restaurants_to_fetch = payload.restaurants or SUPPORTED_RESTAURANTS

        # DB-level persistent cache: skip Playwright if fresh scraped deals already exist
        async with async_session_maker() as check_session:
            cutoff = datetime.utcnow() - timedelta(seconds=settings.ubereats_cache_ttl_seconds)
            fresh_count_result = await check_session.execute(
                select(func.count(Deal.id)).where(
                    Deal.is_active == True,
                    Deal.deal_type == "Uber Eats Menu",
                    Deal.created_at > cutoff,
                )
            )
            fresh_count = fresh_count_result.scalar() or 0

        if fresh_count >= 10:
            logger.info(
                "⚡ DB cache hit: %d fresh Uber Eats deals (< %dh old) — skipping scrape",
                fresh_count, settings.ubereats_cache_ttl_seconds // 3600,
            )
            cache_result = {
                "cached": True,
                "fresh_deals": fresh_count,
                "ranked_deals": [],
                "metadata": {"cache_hit": True, "fresh_count": fresh_count},
            }
            cache_progress = {"stage": "cache_hit", "cached": True, "fresh_deals": fresh_count}
            await _update_job(
                job_id,
                status="completed",
                finished_at=datetime.utcnow(),
                progress_json=json.dumps(cache_progress),
                result_json=json.dumps(cache_result),
            )
            return {"status": "completed", "progress": cache_progress, "result": cache_result}

        # Snapshot current deals before we touch DB — restore if scrape yields nothing
        async with async_session_maker() as snap_session:
            snap_result = await snap_session.execute(
                select(Deal).where(Deal.deal_type == "Uber Eats Menu", Deal.is_active == True)
            )
            snapshot_deals = snap_result.scalars().all()
            snapshot_data = [
                {c.name: getattr(d, c.name) for c in Deal.__table__.columns}
                for d in snapshot_deals
            ]

        # Delete old Uber Eats deals
        async with async_session_maker() as del_session:
            await del_session.execute(delete(Deal).where(Deal.deal_type == "Uber Eats Menu"))
            await del_session.commit()

        progress["stage"] = "finding_stores"
        await _update_job(job_id, progress_json=json.dumps(progress))

        store_targets: List[dict] = []

        # Explicit store URLs take priority
        if payload.store_urls:
            for idx, store_url in enumerate(payload.store_urls):
                restaurant_name = restaurants_to_fetch[idx] if idx < len(restaurants_to_fetch) else "Uber Eats Store"
                url_str = str(store_url)
                store_targets.append({"restaurant": restaurant_name, "store_url": url_str, "store_external_id": _store_id_from_url(url_str), "source": "manual"})

        # Playwright-based store discovery
        if payload.location:
            total_restaurants = len(restaurants_to_fetch)
            found_count = 0

            async def _on_store_found(restaurant: str, stores, *, error: str = None) -> None:
                nonlocal found_count
                found_count += 1
                if stores:
                    for store in stores:
                        store_targets.append({"restaurant": restaurant, "store_url": store.store_url, "store_external_id": store.store_id, "source": "auto"})
                    progress["stores"].append({"restaurant": restaurant, "status": "found", "store_url": stores[0].store_url})
                else:
                    progress["failed"] += 1
                    err_msg = error or f"no stores found near {payload.location}"
                    progress["stores"].append({"restaurant": restaurant, "status": "not_found", "error": err_msg})
                progress["finding_stores_done"] = found_count
                progress["finding_stores_total"] = total_restaurants
                await _update_job(job_id, progress_json=json.dumps(progress))

            await ubereats_store_search.search_stores_bulk(
                restaurants=restaurants_to_fetch,
                location=payload.location,
                limit=max(1, settings.ubereats_store_limit),
                debug=settings.ubereats_debug,
                slow_mo_ms=settings.ubereats_slow_mo_ms,
                screenshots=settings.ubereats_screenshots,
                progress_callback=_on_store_found,
            )

        if not store_targets:
            logger.warning("No store targets found — preserving existing deals")
            await _restore_snapshot(snapshot_data)
            status = "failed"
            progress["stage"] = "failed"
            progress["stores"].append({"status": "failed", "error": "No Uber Eats stores found for this location. Showing previously cached deals."})
            return {"status": status, "progress": progress, "result": result_payload}

        progress["stage"] = "scraping_menus"
        progress["total_stores"] = len(store_targets)
        await _update_job(job_id, progress_json=json.dumps(progress))

        progress_lock = asyncio.Lock()
        sem = asyncio.Semaphore(STORE_CONCURRENCY)

        async with ubereats_scraper.shared_context() as scrape_ctx:
            async def process_target(target: dict) -> None:
                async with sem:
                    store_url = target["store_url"]
                    restaurant_name = target["restaurant"]
                    store_result = {"restaurant": restaurant_name, "store_url": store_url}
                    try:
                        items = await asyncio.wait_for(
                            ubereats_scraper.fetch_menu(store_url, restaurant_name=restaurant_name, location=payload.location or "", shared_context=scrape_ctx),
                            timeout=STORE_TIMEOUT,
                        )
                        async with async_session_maker() as sess:
                            ranked, unranked = await _persist_items(sess, items, restaurant_name, store_url, payload.location or "", auto_rank=payload.auto_rank)
                            await sess.commit()
                        result_payload["ranked_deals"].extend(ranked)
                        result_payload["unranked_deals"].extend(unranked)
                        store_result["status"] = "completed"
                        store_result["items"] = len(items)
                        async with progress_lock:
                            progress["completed"] += 1
                            progress["stores"].append(store_result)
                    except asyncio.TimeoutError:
                        store_result["status"] = "failed"
                        store_result["error"] = "timeout"
                        async with progress_lock:
                            progress["failed"] += 1
                            progress["stores"].append(store_result)
                    except Exception as exc:
                        logger.exception("Error scraping %s: %s", store_url, exc)
                        store_result["status"] = "failed"
                        store_result["error"] = str(exc)
                        async with progress_lock:
                            progress["failed"] += 1
                            progress["stores"].append(store_result)
                    await _update_job(job_id, progress_json=json.dumps(progress))

            await asyncio.gather(*[process_target(t) for t in store_targets])

        progress["stage"] = "finalizing"
        total_ranked = len(result_payload["ranked_deals"])

        if total_ranked == 0:
            logger.warning("Scrape produced 0 ranked deals — restoring snapshot")
            await _restore_snapshot(snapshot_data)

        status = "completed" if progress["failed"] == 0 else ("partial" if progress["completed"] > 0 else "failed")
        result_payload["metadata"] = {"stores_attempted": len(store_targets), "deals_ranked": total_ranked}

    except Exception as exc:
        logger.exception("Fatal error in run_ubereats_job: %s", exc)
        status = "failed"
        progress["stores"].append({"status": "failed", "error": str(exc)})

    finally:
        finished = datetime.utcnow()
        await _update_job(job_id, status=status, finished_at=finished, progress_json=json.dumps(progress), result_json=json.dumps(result_payload))
        restaurants_list = payload.restaurants or SUPPORTED_RESTAURANTS
        if payload.location:
            key = _cache_key(payload.location, restaurants_list)
            UBEREATS_CACHE[key] = {"job_id": job_id, "status": status, "progress": progress, "result": result_payload, "expires_at": datetime.utcnow() + timedelta(seconds=settings.ubereats_cache_ttl_seconds)}

    return {"status": status, "progress": progress, "result": result_payload}


async def _restore_snapshot(snapshot_data: list) -> None:
    """Re-insert snapshot deals if they were deleted and scraping produced nothing."""
    if not snapshot_data:
        return
    async with async_session_maker() as sess:
        for row in snapshot_data:
            row_copy = {k: v for k, v in row.items() if k != "id"}
            sess.add(Deal(**row_copy))
        await sess.commit()
    logger.info("Restored %d snapshot deals", len(snapshot_data))


async def _persist_items(
    session: AsyncSession,
    items,
    restaurant_name: str,
    store_url: str,
    location: str,
    auto_rank: bool,
) -> tuple[list, list]:
    """Persist scraped items. Items without nutrition are still saved — just with score=0."""
    ranked: List[dict] = []
    unranked: List[dict] = []

    store_id = _store_id_from_url(store_url)
    existing_result = await session.execute(
        select(Deal).where(Deal.restaurant_name == restaurant_name, Deal.store_external_id == store_id)
    )
    existing_map = {_normalize(d.item_name): d for d in existing_result.scalars().all()}

    for item in items:
        if not item.price or item.price <= 0:
            unranked.append({"restaurant": restaurant_name, "item": item.name, "reason": "invalid_price"})
            continue

        category = classify_item_category(item.name)
        if category in ("merch", "sauce", "drink"):
            unranked.append({"restaurant": restaurant_name, "item": item.name, "reason": category})
            continue

        calories = item.calories
        protein = item.protein_grams

        if not calories or calories <= 0:
            est = estimate_nugget_nutrition(item.name) or estimate_nutrition_heuristic(item.name, category=item.category or category)
            if est:
                calories = int(est["calories"])
                protein = float(est.get("protein_grams", protein or 0))

        scores = None
        if calories and calories > 0:
            scores = calculate_final_value_score(calories, protein or 0.0, item.price)
            scores["calories"] = int(calories)
            scores["protein_grams"] = float(protein or 0.0)

        key = _normalize(item.name)
        deal = existing_map.get(key)
        if deal is None:
            deal = Deal(
                restaurant_name=restaurant_name,
                item_name=item.name,
                price=item.price,
                category=item.category or category,
                deal_type="Uber Eats Menu",
                calories=scores["calories"] if scores else calories,
                protein_grams=scores["protein_grams"] if scores else protein,
                source_price_vendor=item.source_price_vendor,
                store_external_id=item.store_external_id or store_id,
                price_retrieved_at=item.price_retrieved_at,
                location=location,
                is_active=True,
                value_score=scores["value_score"] if scores else 0.0,
                satiety_score=scores["satiety_score"] if scores else 0.0,
                price_per_calorie=scores["price_per_calorie"] if scores else 0.0,
            )
            session.add(deal)
            existing_map[key] = deal
        else:
            deal.price = item.price
            deal.category = item.category or category
            deal.location = location
            deal.is_active = True
            if scores:
                deal.calories = scores["calories"]
                deal.protein_grams = scores["protein_grams"]
                deal.value_score = scores["value_score"]
                deal.satiety_score = scores["satiety_score"]
                deal.price_per_calorie = scores["price_per_calorie"]
            elif calories:
                deal.calories = calories
                deal.protein_grams = protein

        if scores:
            ranked.append({"restaurant": restaurant_name, "item": item.name, "price": item.price, "value_score": deal.value_score})
        else:
            unranked.append({"restaurant": restaurant_name, "item": item.name, "reason": "missing_nutrition_persisted"})

    return sorted(ranked, key=lambda x: x.get("value_score", 0), reverse=True), unranked
