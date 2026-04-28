from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./biterank.db"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- UberEats scraping controls ---
    ubereats_headless: bool = True
    ubereats_debug: bool = False          # if True: headed + slow_mo
    ubereats_slow_mo_ms: int = 250
    ubereats_trace: bool = False
    ubereats_screenshots: bool = False

    ubereats_store_limit: int = 1         # stores per restaurant
    ubereats_scroll_passes: int = 12      # menu page scroll loops
    ubereats_timeout_ms: int = 30000
    ubereats_request_timeout_seconds: int = 180
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
