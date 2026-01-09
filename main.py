from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio
import json
import contextlib
from datetime import datetime, timedelta, time

from sqlalchemy import delete

from api.routes import (
    router,
    _run_ubereats_job,
    SUPPORTED_UBER_EATS_RESTAURANTS,
)
from database import init_db, async_session_maker
from models import Deal, ScrapeJob
from schemas import UberEatsImportRequest
from services.gemini_service import gemini_service  # Force import to trigger logging
from config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    # Startup
    logger.info("Starting MenuRanker API...")
    await init_db()
    logger.info("Database initialized")
    logger.info(f"Gemini service loaded: {gemini_service}")

    daily_task = None
    if settings.ubereats_cron_enabled and settings.ubereats_cron_location:
        daily_task = asyncio.create_task(schedule_daily_ubereats_import())
        logger.info(
            "Uber Eats daily import scheduled for %02d:00 UTC using location %s",
            settings.ubereats_cron_hour_utc,
            settings.ubereats_cron_location,
        )
    elif settings.ubereats_cron_enabled:
        logger.warning("Uber Eats daily import enabled but no location set; skipping scheduling.")

    logger.info("MenuRanker API started successfully")

    yield

    # Shutdown
    logger.info("Shutting down MenuRanker API...")
    if daily_task:
        daily_task.cancel()
        with contextlib.suppress(Exception):
            await daily_task


app = FastAPI(
    title="MenuRanker API",
    description="API for ranking fast food deals by value using AI",
    version="1.0.0",
    lifespan=lifespan
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update with specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router, prefix="/api")


@app.get("/")
async def root():
    return {
        "message": "Welcome to MenuRanker API",
        "docs": "/docs",
        "version": "1.0.0"
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


async def schedule_daily_ubereats_import():
    """Run a daily Uber Eats import at the configured UTC hour."""
    while True:
        now = datetime.utcnow()
        target = datetime.combine(now.date(), time(hour=settings.ubereats_cron_hour_utc))
        if target <= now:
            target += timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        await asyncio.sleep(sleep_seconds)
        try:
            await run_daily_ubereats_import()
        except Exception as exc:
            logger.exception("Daily Uber Eats import failed: %s", exc)


async def run_daily_ubereats_import():
    """Clear old Uber Eats deals and trigger a fresh import."""
    if not settings.ubereats_cron_location:
        logger.warning("Cron import skipped: no location configured.")
        return

    # Clear previous Uber Eats menu deals
    async with async_session_maker() as session:
        await session.execute(delete(Deal).where(Deal.deal_type == "Uber Eats Menu"))
        await session.commit()

    payload = UberEatsImportRequest(
        location=settings.ubereats_cron_location,
        restaurants=SUPPORTED_UBER_EATS_RESTAURANTS,
        auto_rank=True,
    )

    async with async_session_maker() as session:
        job = ScrapeJob(status="queued", request_json=json.dumps(payload.model_dump()))
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    logger.info(
        "Starting scheduled Uber Eats import job %s for %s",
        job_id,
        settings.ubereats_cron_location,
    )
    await _run_ubereats_job(job_id, payload)
    logger.info("Scheduled Uber Eats import job %s finished", job_id)
