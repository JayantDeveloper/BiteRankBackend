"""Debug and diagnostic endpoints."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Deal, ScrapeJob

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/debug/scrape-status")
async def scrape_status(db: AsyncSession = Depends(get_db)):
    """Returns the last scrape job's full details plus DB deal counts — for diagnosing empty results."""
    last_job_result = await db.execute(
        select(ScrapeJob).order_by(ScrapeJob.created_at.desc()).limit(1)
    )
    last_job = last_job_result.scalar_one_or_none()

    deal_count_result = await db.execute(select(func.count()).select_from(Deal).where(Deal.is_active == True))
    total_active_deals = deal_count_result.scalar()

    by_restaurant_result = await db.execute(
        select(Deal.restaurant_name, func.count().label("count"))
        .where(Deal.is_active == True)
        .group_by(Deal.restaurant_name)
    )
    by_restaurant = {row[0]: row[1] for row in by_restaurant_result.all()}

    zero_score_result = await db.execute(
        select(func.count()).select_from(Deal).where(Deal.is_active == True, Deal.value_score == 0)
    )
    zero_score_count = zero_score_result.scalar()

    return {
        "db_summary": {
            "total_active_deals": total_active_deals,
            "by_restaurant": by_restaurant,
            "zero_score_count": zero_score_count,
        },
        "last_job": {
            "id": last_job.id if last_job else None,
            "status": last_job.status if last_job else None,
            "created_at": last_job.created_at.isoformat() if last_job and last_job.created_at else None,
            "started_at": last_job.started_at.isoformat() if last_job and last_job.started_at else None,
            "finished_at": last_job.finished_at.isoformat() if last_job and last_job.finished_at else None,
            "progress": json.loads(last_job.progress_json) if last_job and last_job.progress_json else None,
            "result": json.loads(last_job.result_json) if last_job and last_job.result_json else None,
            "request": json.loads(last_job.request_json) if last_job and last_job.request_json else None,
        } if last_job else None,
    }
