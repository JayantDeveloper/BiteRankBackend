from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./biterank.db"  # set DATABASE_URL in prod (PostgreSQL)
    database_echo: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Firecrawl scraping controls ---
    firecrawl_api_key: str = ""            # FIRECRAWL_API_KEY env var
    firecrawl_timeout_seconds: int = 90    # per scrape/search request
    firecrawl_concurrency: int = 2         # simultaneous Firecrawl requests

    # --- UberEats import controls ---
    ubereats_store_limit: int = 1         # stores per restaurant
    ubereats_max_restaurants: int = 8
    ubereats_max_total_stores: int = 10
    ubereats_cron_enabled: bool = False
    ubereats_cron_hour_utc: int = 0       # 0 = 12 AM UTC
    ubereats_cron_location: str = ""      # e.g., "21044" required when cron enabled
    ubereats_cache_ttl_seconds: int = 14400  # 4 hours

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings():
    return Settings()
