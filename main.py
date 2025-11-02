from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from api.routes import router
from database import init_db
from services.gemini_service import gemini_service  # Force import to trigger logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    # Startup
    logger.info("Starting MenuRanker API...")
    await init_db()
    logger.info("Database initialized")
    logger.info(f"Gemini service loaded: {gemini_service}")
    logger.info("MenuRanker API started successfully")

    yield

    # Shutdown
    logger.info("Shutting down MenuRanker API...")


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
