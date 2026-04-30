"""FastAPI entrypoint for BiteRank backend."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select, update

from api import router
from api.scraping import run_ubereats_job, SUPPORTED_RESTAURANTS
from database import async_session_maker, init_db
from models import Deal, ScrapeJob
from schemas import UberEatsImportRequest
from config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting BiteRank API...")
    await init_db()
    await _recover_stuck_jobs()
    await _seed_if_empty()

    daily_task = None
    if settings.ubereats_cron_enabled and settings.ubereats_cron_location:
        daily_task = asyncio.create_task(_schedule_daily_import())
        logger.info("Daily import scheduled at %02d:00 UTC for %s", settings.ubereats_cron_hour_utc, settings.ubereats_cron_location)

    logger.info("BiteRank API started")
    yield

    if daily_task:
        daily_task.cancel()
        with contextlib.suppress(Exception):
            await daily_task


async def _recover_stuck_jobs() -> None:
    """Mark jobs that have been running for >10 minutes as failed."""
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    async with async_session_maker() as session:
        result = await session.execute(
            select(ScrapeJob).where(ScrapeJob.status == "running", ScrapeJob.started_at < cutoff)
        )
        stuck = result.scalars().all()
        for job in stuck:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            job.progress_json = json.dumps({"stage": "failed", "error": "Job timed out (server restart)"})
        if stuck:
            await session.commit()
            logger.warning("Recovered %d stuck jobs", len(stuck))


async def _seed_if_empty() -> None:
    """Populate DB with curated static deals if it is empty."""
    async with async_session_maker() as session:
        count_result = await session.execute(select(func.count()).select_from(Deal))
        if count_result.scalar() > 0:
            return

    seed_path = os.path.join(os.path.dirname(__file__), "seed_data.json")
    if not os.path.exists(seed_path):
        logger.warning("seed_data.json not found, skipping seed")
        return

    with open(seed_path) as f:
        seed_items = json.load(f)

    from services.value_calculator import calculate_final_value_score, estimate_nutrition_heuristic

    inserted = 0
    async with async_session_maker() as session:
        for item in seed_items:
            cal = item.get("calories")
            protein = item.get("protein_grams", 0.0)
            price = item.get("price", 0)

            if not cal or not price:
                continue

            scores = calculate_final_value_score(cal, protein, price)
            deal = Deal(
                restaurant_name=item["restaurant_name"],
                item_name=item["item_name"],
                price=price,
                calories=cal,
                protein_grams=protein,
                category=item.get("category"),
                deal_type=item.get("deal_type", "Menu Item"),
                description=item.get("description"),
                is_active=True,
                value_score=scores["value_score"],
                satiety_score=scores["satiety_score"],
                price_per_calorie=scores["price_per_calorie"],
                last_ranked_at=datetime.utcnow(),
            )
            session.add(deal)
            inserted += 1
        await session.commit()

    logger.info("Seeded %d deals from seed_data.json", inserted)


async def _schedule_daily_import() -> None:
    while True:
        now = datetime.utcnow()
        target = datetime.combine(now.date(), time(hour=settings.ubereats_cron_hour_utc))
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            async with async_session_maker() as session:
                await session.execute(delete(Deal).where(Deal.deal_type == "Uber Eats Menu"))
                await session.commit()
            payload = UberEatsImportRequest(location=settings.ubereats_cron_location, restaurants=SUPPORTED_RESTAURANTS, auto_rank=True)
            async with async_session_maker() as session:
                job = ScrapeJob(status="queued", request_json=json.dumps(payload.model_dump()))
                session.add(job)
                await session.commit()
                await session.refresh(job)
            await run_ubereats_job(job.id, payload)
        except Exception as exc:
            logger.exception("Daily import failed: %s", exc)


app = FastAPI(title="BiteRank API", description="Fast food value ranking", version="2.0.0", lifespan=lifespan)

_cors_raw = os.environ.get("ALLOWED_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_raw.split(",") if o.strip()]
    if _cors_raw.strip() and _cors_raw.strip() != "*"
    else ["*"]
)
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(router, prefix="/api")


@app.get("/")
async def root():
    return {"message": "BiteRank API v2", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
