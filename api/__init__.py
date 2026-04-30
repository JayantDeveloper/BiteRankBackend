"""API router assembly."""
from fastapi import APIRouter

from api.deals import router as deals_router
from api.ranking import router as ranking_router
from api.scraping import router as scraping_router
from api.locations import router as locations_router
from api.debug import router as debug_router
from api.auth import router as auth_router

router = APIRouter()
router.include_router(deals_router)
router.include_router(ranking_router)
router.include_router(scraping_router)
router.include_router(locations_router)
router.include_router(debug_router)
router.include_router(auth_router)
