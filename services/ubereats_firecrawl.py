"""Uber Eats scraping via the Firecrawl API.

Replaces the Playwright-based ubereats_scraper.py / ubereats_store_search.py.
Firecrawl handles JS rendering, bot detection, and proxies server-side, so the
backend never launches a browser (Render free tier has 512MB RAM).

Two operations:
- search_stores(): Firecrawl /v2/search with a site:ubereats.com query to find
  nearby store URLs for a restaurant chain.
- fetch_menu(): Firecrawl /v2/scrape with a JSON extraction schema that pulls
  structured menu items (name, price, calories, protein) off the store page.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from config import get_settings
from timeutil import utcnow

logger = logging.getLogger(__name__)

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"

MENU_EXTRACT_PROMPT = (
    "Extract every individual food menu item shown on this Uber Eats store page. "
    "For each item include its name, price in USD as a number, calories as an "
    "integer if displayed (e.g. '340 Cal.'), protein in grams if displayed, and "
    "the menu section/category it appears under. Include all sections, not just "
    "featured items. Skip toys, merchandise, and standalone sauces."
)

MENU_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                    "calories": {"type": ["integer", "null"]},
                    "protein_grams": {"type": ["number", "null"]},
                    "category": {"type": ["string", "null"]},
                },
                "required": ["name", "price"],
            },
        },
    },
    "required": ["items"],
}

# Scroll the page a few times before extraction so lazy-loaded menu sections render.
MENU_SCRAPE_ACTIONS = [
    {"type": "wait", "milliseconds": 1500},
    {"type": "scroll", "direction": "down"},
    {"type": "wait", "milliseconds": 1000},
    {"type": "scroll", "direction": "down"},
    {"type": "wait", "milliseconds": 1000},
]


class FirecrawlError(RuntimeError):
    """Raised when the Firecrawl API returns an error or is misconfigured."""


@dataclass
class UberEatsStore:
    store_url: str
    store_id: str
    title: str = ""


@dataclass
class UberEatsMenuItem:
    name: str
    price: Optional[float]
    calories: Optional[int] = None
    protein_grams: Optional[float] = None
    category: Optional[str] = None
    store_external_id: Optional[str] = None
    price_retrieved_at: Optional[datetime] = field(default=None)
    source_price_vendor: str = "ubereats"


def _store_id_from_url(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) >= 3 and segments[0] == "store":
        return segments[2]
    if len(segments) >= 2 and segments[0] == "store":
        return segments[1]
    return url


def _canonical_store_url(url: str) -> str:
    """Strip tracking query params (srsltid etc.) — keep scheme://host/path."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class UberEatsFirecrawl:
    def __init__(self, api_key: Optional[str] = None, timeout_seconds: Optional[int] = None):
        self._api_key = api_key
        self._timeout = timeout_seconds

    def _config(self) -> tuple[str, int]:
        settings = get_settings()
        api_key = self._api_key or settings.firecrawl_api_key
        if not api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set — cannot scrape Uber Eats")
        return api_key, self._timeout or settings.firecrawl_timeout_seconds

    async def _post(self, path: str, payload: dict) -> dict:
        api_key, timeout = self._config()
        async with httpx.AsyncClient(timeout=timeout + 30) as client:
            resp = await client.post(
                f"{FIRECRAWL_BASE}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code >= 400:
            raise FirecrawlError(f"Firecrawl {path} returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if not body.get("success"):
            raise FirecrawlError(f"Firecrawl {path} failed: {str(body)[:300]}")
        return body.get("data") or {}

    async def search_stores(self, restaurant: str, location: str, limit: int = 1) -> List[UberEatsStore]:
        """Find nearby Uber Eats store URLs for a restaurant chain via web search."""
        data = await self._post(
            "/search",
            {"query": f'"{restaurant}" {location} site:ubereats.com', "limit": 8},
        )
        results = data.get("web") or []

        stores: List[UberEatsStore] = []
        seen_ids: set = set()
        for r in results:
            url = r.get("url") or ""
            if "/store/" not in urlsplit(url).path:
                continue
            clean_url = _canonical_store_url(url)
            store_id = _store_id_from_url(clean_url)
            if store_id in seen_ids:
                continue
            seen_ids.add(store_id)
            stores.append(UberEatsStore(store_url=clean_url, store_id=store_id, title=r.get("title") or ""))
            if len(stores) >= limit:
                break

        logger.info("Firecrawl store search for %s near %s → %d store(s)", restaurant, location, len(stores))
        return stores

    async def fetch_menu(self, store_url: str, restaurant_name: str = "") -> List[UberEatsMenuItem]:
        """Scrape one store page and return structured menu items."""
        _, timeout = self._config()
        data = await self._post(
            "/scrape",
            {
                "url": store_url,
                "formats": [{
                    "type": "json",
                    "prompt": MENU_EXTRACT_PROMPT,
                    "schema": MENU_EXTRACT_SCHEMA,
                }],
                "onlyMainContent": True,
                "actions": MENU_SCRAPE_ACTIONS,
                "timeout": timeout * 1000,
            },
        )
        extracted = data.get("json") or {}
        raw_items = extracted.get("items") or []

        store_id = _store_id_from_url(store_url)
        retrieved_at = utcnow()
        items: List[UberEatsMenuItem] = []
        for raw in raw_items:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            try:
                price = float(raw["price"]) if raw.get("price") is not None else None
            except (TypeError, ValueError):
                price = None
            try:
                calories = int(raw["calories"]) if raw.get("calories") else None
            except (TypeError, ValueError):
                calories = None
            try:
                protein = float(raw["protein_grams"]) if raw.get("protein_grams") is not None else None
            except (TypeError, ValueError):
                protein = None

            items.append(UberEatsMenuItem(
                name=name,
                price=price,
                calories=calories,
                protein_grams=protein,
                category=(raw.get("category") or None),
                store_external_id=store_id,
                price_retrieved_at=retrieved_at,
            ))

        logger.info("Firecrawl menu scrape %s (%s) → %d items", restaurant_name or store_url, store_id, len(items))
        return items


ubereats_firecrawl = UberEatsFirecrawl()
