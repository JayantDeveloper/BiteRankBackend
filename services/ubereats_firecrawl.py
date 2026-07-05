"""Uber Eats scraping via the Firecrawl API.

Replaces the Playwright-based ubereats_scraper.py / ubereats_store_search.py.
Firecrawl handles JS rendering, bot detection, and proxies server-side, so the
backend never launches a browser (Render free tier has 512MB RAM).

Two operations:
- search_stores(): Firecrawl /v2/search with a site:ubereats.com query to find
  nearby store URLs for a restaurant chain (filtered so only stores whose URL
  slug matches the requested brand survive — search results mix in neighbors).
- fetch_menu(): Firecrawl /v2/scrape in markdown format, parsed with a
  deterministic regex. Uber Eats renders every item as
  "- [Name\\ ... $12.34 • 560 Cal.\\ ...", so plain parsing captures the FULL
  menu (~100+ items) where LLM JSON extraction truncated at ~20 — and costs
  1 credit instead of ~5. The JSON-extraction path remains as a fallback in
  case Uber's markup changes shape.
"""
from __future__ import annotations

import logging
import re
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
    {"type": "wait", "milliseconds": 800},
    {"type": "scroll", "direction": "down"},
    {"type": "wait", "milliseconds": 800},
    {"type": "scroll", "direction": "down"},
    {"type": "wait", "milliseconds": 800},
]

# One menu item in Firecrawl's markdown rendering of an Uber Eats store page:
#   - [10 pc. Chicken McNuggets®\\
#     \\
#     $7.29 • 410 Cal.\\
# Calories may be absent or a range ("900 - 1200 Cal.").
MENU_ITEM_RE = re.compile(
    r"\[(?P<name>[^\]\\\n]{2,120}?)\\\\\s*\\\\\s*"
    r"\$(?P<price>\d+(?:\.\d{1,2})?)"
    r"(?:\s*•\s*(?P<cal_lo>[\d,]+)(?:\s*-\s*(?P<cal_hi>[\d,]+))?\s*Cal\.)?"
)

# If the markdown parse finds fewer items than this, assume Uber changed their
# markup and fall back to LLM JSON extraction.
MARKDOWN_PARSE_MIN_ITEMS = 5


def parse_menu_markdown(markdown: str) -> List[dict]:
    """Deterministically parse menu items out of a store page's markdown.

    Returns dicts with name/price/calories, deduped by normalized name
    (items repeat across Featured/Most Popular and their home section).
    """
    seen: set = set()
    items: List[dict] = []
    for m in MENU_ITEM_RE.finditer(markdown):
        name = m.group("name").strip()
        key = " ".join(name.lower().split())
        if not name or key in seen:
            continue
        seen.add(key)
        try:
            price = float(m.group("price"))
        except (TypeError, ValueError):
            continue
        calories = None
        if m.group("cal_lo"):
            lo = int(m.group("cal_lo").replace(",", ""))
            hi = int(m.group("cal_hi").replace(",", "")) if m.group("cal_hi") else lo
            calories = (lo + hi) // 2
        items.append({"name": name, "price": price, "calories": calories, "protein_grams": None, "category": None})
    return items


def slug_matches_brand(store_url: str, restaurant: str) -> bool:
    """True when the store URL slug is actually the requested brand.

    Search results mix in neighboring restaurants (e.g. 'Panda Express' for a
    Subway query); a store slug like 'subway-7296-baltimore-ave' must contain
    the normalized brand name ('subway', 'chickfila', 'kfc')."""
    segments = [s for s in urlsplit(store_url).path.split("/") if s]
    slug = segments[1] if len(segments) >= 2 and segments[0] == "store" else ""
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    brand = norm(restaurant)
    return bool(brand) and brand in norm(slug)


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
            if not slug_matches_brand(url, restaurant):
                logger.info("Skipping wrong-brand store result for %s: %s", restaurant, url[:80])
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
        """Scrape one store page and return structured menu items.

        Primary path: markdown scrape + deterministic parse (full menu, 1
        credit), retried once uncached on a bad render. Fallback: LLM JSON
        extraction (partial but resilient to markup changes, ~5 credits)."""
        _, timeout = self._config()
        settings = get_settings()

        def markdown_payload(max_age_ms: int) -> dict:
            return {
                "url": store_url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "actions": MENU_SCRAPE_ACTIONS,
                "timeout": timeout * 1000,
                # Never accept a Firecrawl-cached page older than our own
                # deal-freshness window (their default is ~2 days).
                "maxAge": max_age_ms,
            }

        data = await self._post("/scrape", markdown_payload(settings.ubereats_cache_ttl_seconds * 1000))
        raw_items = parse_menu_markdown(data.get("markdown") or "")

        if len(raw_items) < MARKDOWN_PARSE_MIN_ITEMS:
            # Transient bad renders happen (bot interstitial / partial page);
            # one fresh (uncached) retry usually recovers the full menu.
            logger.warning(
                "Markdown parse found only %d items for %s — retrying uncached",
                len(raw_items), restaurant_name or store_url,
            )
            data = await self._post("/scrape", markdown_payload(0))
            raw_items = parse_menu_markdown(data.get("markdown") or "")

        if len(raw_items) < MARKDOWN_PARSE_MIN_ITEMS:
            logger.warning(
                "Markdown parse still found only %d items for %s — falling back to LLM extraction",
                len(raw_items), restaurant_name or store_url,
            )
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
        seen_names: set = set()
        for raw in raw_items:
            name = (raw.get("name") or "").strip()
            name_key = " ".join(name.lower().split())
            if not name or name_key in seen_names:
                continue
            seen_names.add(name_key)
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
