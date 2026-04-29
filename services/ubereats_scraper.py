"""Uber Eats Playwright menu scraper."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, BrowserContext

from config import get_settings

logger = logging.getLogger(__name__)

# Reduce Chromium memory footprint for constrained environments (e.g. Render free tier).
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
    "--single-process",
    "--disable-extensions",
]
settings = get_settings()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PRICE_RE = re.compile(r"^(?:US\$|\$)\s*(\d+(?:\.\d{1,2})?)$", re.IGNORECASE)
PRICE_RE_FLEXIBLE = re.compile(
    r"(?:US\$|\$|USD|EUR|GBP|€|£)?\s*(\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)

CAL_RE = re.compile(r"(\d+(?:\s*-\s*\d+)?)\s*Cal\.?", re.IGNORECASE)
PROTEIN_RE_1 = re.compile(r"(\d+(?:\.\d+)?)\s*g\s*protein\b", re.IGNORECASE)
PROTEIN_RE_2 = re.compile(r"\bprotein\b\s*(\d+(?:\.\d+)?)\s*g", re.IGNORECASE)
NUTRITION_LINE_RE = re.compile(r"(?P<cal>\d+(?:\s*-\s*\d+)?)\s*Cal.*?(?P<protein>\d+(?:\.\d+)?)\s*g\s*Protein", re.IGNORECASE)
NAME_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


@dataclass
class UberEatsMenuItem:
    restaurant: str
    name: str
    price: float
    category: Optional[str] = None

    calories: Optional[int] = None
    protein_grams: Optional[float] = None

    source_price_vendor: str = "ubereats"
    store_external_id: Optional[str] = None
    price_retrieved_at: datetime = datetime.now(timezone.utc)
    location: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["price_retrieved_at"] = self.price_retrieved_at.isoformat()
        return data


def _extract_store_id_from_url(url: str) -> str:
    path = urlparse(url).path
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 3 and segments[0] == "store":
        return segments[2]
    if len(segments) >= 2 and segments[0] == "store":
        return segments[1]
    return url


def _avg_or_single(cal_min: Optional[int], cal_max: Optional[int]) -> Optional[int]:
    if cal_min is None and cal_max is None:
        return None
    if cal_min is None:
        return cal_max
    if cal_max is None:
        return cal_min
    return int(round((cal_min + cal_max) / 2))


def _parse_calories(text: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (min, max) calories if present, else (None, None)
    """
    if not text:
        return None, None
    m = CAL_RE.search(text)
    if not m:
        return None, None

    cal_str = m.group(1).replace(" ", "")
    if "-" in cal_str:
        a, b = cal_str.split("-", 1)
        try:
            return int(a), int(b)
        except ValueError:
            return None, None

    try:
        v = int(cal_str)
        return v, v
    except ValueError:
        return None, None


def _parse_protein_grams(text: str) -> Optional[float]:
    if not text:
        return None

    m = NUTRITION_LINE_RE.search(text)
    if m:
        try:
            return float(m.group("protein"))
        except ValueError:
            pass

    m1 = PROTEIN_RE_1.search(text)
    if m1:
        try:
            return float(m1.group(1))
        except ValueError:
            return None

    m2 = PROTEIN_RE_2.search(text)
    if m2:
        try:
            return float(m2.group(1))
        except ValueError:
            return None

    return None


def _is_valid_item_name(name: str) -> bool:
    if not name:
        return False
    stripped = name.strip()
    if not (1 <= len(stripped) <= 120):
        return False
    return bool(NAME_ALNUM_RE.search(stripped))


def _extract_price_flexible(text: str) -> Optional[float]:
    """
    More flexible price extraction that handles currency symbols, codes,
    and plain numbers that look like prices.
    """
    if not text:
        return None
    text = text.strip()

    m = re.match(r"^(?:US\$|\$)\s*(\d+(?:\.\d{1,2})?)$", text, re.IGNORECASE)
    if m:
        return float(m.group(1))

    m = re.match(r"^(?:USD|EUR|GBP)\s*(\d+(?:\.\d{1,2})?)$", text, re.IGNORECASE)
    if m:
        return float(m.group(1))

    decimal_number = re.match(r"^(\d{1,3}(?:[.,]\d{1,2}))$", text)
    if decimal_number:
        try:
            val = float(decimal_number.group(1).replace(",", "."))
            if 0.5 <= val < 1000:
                return val
        except ValueError:
            pass

    m = PRICE_RE_FLEXIBLE.search(text)
    if m:
        candidate = m.group(1).replace(",", ".")
        try:
            val = float(candidate)
            if 0.5 <= val < 1000:
                return val
        except ValueError:
            return None

    return None


def _extract_price_from_node(node: dict) -> Optional[float]:
    """
    Comprehensive price extraction from JSON node.
    Checks many possible field names and formats (cents vs dollars).
    """
    if not isinstance(node, dict):
        return None

    price_fields = [
        "price",
        "displayPrice",
        "itemPrice",
        "basePrice",
        "rawPrice",
        "formattedPrice",
        "priceString",
        "displayPriceString",
        "menuItemPrice",
        "cost",
        "amount",
        "value",
        "displayValue",
    ]

    for field in ["price", "displayPrice", "itemPrice", "basePrice", "rawPrice"]:
        price_obj = node.get(field)
        if isinstance(price_obj, dict):
            amt = price_obj.get("amount") or price_obj.get("value")
            if isinstance(amt, int):
                if amt >= 100:
                    return amt / 100.0
                if 0.5 <= amt < 1000:
                    return float(amt)
            if isinstance(amt, (float, str)):
                try:
                    val = float(amt)
                    if val >= 100:
                        return val / 100.0
                    if 0.5 <= val < 1000:
                        return val
                except (ValueError, TypeError):
                    pass

    for field in price_fields:
        val = node.get(field)
        if isinstance(val, (int, float)):
            fval = float(val)
            if fval >= 100:
                return fval / 100.0
            if 0.5 <= fval < 1000:
                return fval

    for field in price_fields:
        val = node.get(field)
        if isinstance(val, str):
            parsed = _extract_price_flexible(val)
            if parsed is not None:
                return parsed

    return None


def _safe_get(obj: Any, keys: Iterable[str]) -> Any:
    """
    Try multiple keys in order for dict-like objects.
    """
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None


def _walk(obj: Any) -> Iterable[Any]:
    """
    Generic deep walk generator through dict/list trees.
    """
    stack = [obj]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            for v in cur.values():
                stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                stack.append(v)


def _extract_items_from_embedded_json(
    data: Any,
    *,
    restaurant_name: str,
    store_id: str,
    retrieved_at: datetime,
    location: Optional[str],
) -> List[UberEatsMenuItem]:
    """
    Attempt to extract structured items from Uber embedded state.
    Uber changes shapes often, so this is intentionally defensive.
    Hard cap at 500 items to prevent runaway extraction.
    """
    items: List[UberEatsMenuItem] = []

    for node in _walk(data):
        if len(items) >= 500:
            logger.warning("⚠️ Hit 500 item cap during embedded JSON extraction")
            break
        if not isinstance(node, dict):
            continue

        name = _safe_get(node, ["title", "name", "displayName", "itemName"])
        if not isinstance(name, str) or not _is_valid_item_name(name):
            continue

        price_val = _extract_price_from_node(node)
        if price_val is None or not (0.5 <= price_val < 1000):
            continue

        category = _safe_get(node, ["category", "sectionTitle", "sectionName", "menuSection"])
        if isinstance(category, dict):
            category = _safe_get(category, ["title", "name"])
        if not isinstance(category, str):
            category = None
        else:
            category = category.strip()[:80] if category.strip() else None

        calories_min = calories_max = None
        protein_grams: Optional[float] = None

        nutrition = _safe_get(node, ["nutrition", "nutritionalInfo", "nutritionInfo"])
        if isinstance(nutrition, dict):
            cal_candidate = _safe_get(nutrition, ["calories", "calorie", "kcal"])
            if isinstance(cal_candidate, (int, float, str)):
                try:
                    c = float(cal_candidate)
                    calories_min = int(round(c))
                    calories_max = calories_min
                except ValueError:
                    pass

            prot_candidate = _safe_get(nutrition, ["protein", "proteinGrams", "protein_grams"])
            if isinstance(prot_candidate, (int, float, str)):
                try:
                    protein_grams = float(prot_candidate)
                except ValueError:
                    pass

            nutrition_line = _safe_get(nutrition, ["displayText", "label", "summary"])
            if isinstance(nutrition_line, str):
                cmin, cmax = _parse_calories(nutrition_line)
                if cmin is not None:
                    calories_min, calories_max = cmin, cmax
                p = _parse_protein_grams(nutrition_line)
                if p is not None:
                    protein_grams = p

        if calories_min is None or protein_grams is None:
            maybe_text = _safe_get(node, ["subtitle", "description", "nutritionLabel", "meta"])
            if isinstance(maybe_text, str):
                cmin, cmax = _parse_calories(maybe_text)
                if cmin is not None and calories_min is None:
                    calories_min, calories_max = cmin, cmax
                p = _parse_protein_grams(maybe_text)
                if p is not None and protein_grams is None:
                    protein_grams = p

        calories = _avg_or_single(calories_min, calories_max)

        items.append(
            UberEatsMenuItem(
                restaurant=restaurant_name,
                name=name.strip(),
                price=float(price_val),
                category=category,
                calories=calories,
                protein_grams=protein_grams,
                store_external_id=store_id,
                price_retrieved_at=retrieved_at,
                location=location,
                source_price_vendor="ubereats",
            )
        )

    return items


def _extract_embedded_state(html: str) -> Optional[Any]:
    """
    Try common script blobs used by Uber web apps.
    """
    patterns = [
        re.compile(r"window\.__NEXT_DATA__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
        re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
        re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
    ]

    for pat in patterns:
        m = pat.search(html)
        if not m:
            continue
        raw = m.group(1)
        try:
            return json.loads(raw)
        except Exception:
            continue

    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except Exception:
            pass

    return None


def parse_menu_from_html_fallback(html: str) -> List[Dict]:
    """
    Fallback HTML parse using rich-text spans:
      name -> price -> optional nutrition line
    Improved to use flexible price detection and skip nutrition offsets properly.
    """
    soup = BeautifulSoup(html, "html.parser")

    spans = [
        s.get_text(" ", strip=True)
        for s in soup.select('span[data-testid="rich-text"], span._a0n, span._wx')
    ]
    spans = [s for s in spans if s]

    items: List[Dict] = []
    seen = set()

    i = 0
    CAL_ONLY_RE = re.compile(r"^\s*\d[\d\s,.-]*\s*Cal\.?\s*$", re.IGNORECASE)

    while i < len(spans) - 1:
        name = spans[i]
        if CAL_ONLY_RE.match(name):
            i += 1
            continue
        price = _extract_price_flexible(spans[i + 1])
        if price is None:
            i += 1
            continue

        nutrition_txt = ""
        lookahead_limit = min(len(spans), i + 8)
        for j in range(i + 2, lookahead_limit):
            maybe_nutrition = spans[j]
            if re.search(r"\d+\s*Cal|Protein|cal\.", maybe_nutrition, re.IGNORECASE):
                nutrition_txt = maybe_nutrition
                break

        cmin, cmax = _parse_calories(nutrition_txt)
        protein = _parse_protein_grams(nutrition_txt)

        if _is_valid_item_name(name):
            key = (name.lower(), price, cmin, cmax, protein)
            if key not in seen:
                seen.add(key)
                items.append(
                    {
                        "name": name,
                        "price_usd": price,
                        "calories_min": cmin,
                        "calories_max": cmax,
                        "protein_grams": protein,
                        "category": None,
                    }
                )

        i += 2 if not nutrition_txt else 3

    return items


async def _scroll_to_load_menu(page: Page, passes: int = 12) -> None:
    """
    Gentler scroll loop to avoid skipping lazy-loaded sections.
    Uses smaller steps and stops early if no new rich-text spans appear.
    """
    await page.wait_for_timeout(2000)
    steps = max(0, passes)
    last_count = 0
    stable_runs = 0
    for _ in range(steps):
        await page.mouse.wheel(0, 800)
        await page.wait_for_timeout(450)
        try:
            count = await page.locator('span[data-testid="rich-text"]').count()
            if count == last_count:
                stable_runs += 1
            else:
                stable_runs = 0
            last_count = count
            if stable_runs >= 3:
                break
        except Exception:
            continue
    for _ in range(3):
        await page.mouse.wheel(0, 400)
        await page.wait_for_timeout(400)


class UberEatsScraper:
    """
    Playwright-based Uber Eats scraper.
    Tries embedded JSON first (better nutrition/category), falls back to rich-text parsing.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        max_concurrent_scrapes: int = 1,
        scroll_passes: int = 12,
        debug: bool = False,
        slow_mo_ms: int = 0,
        trace: bool = False,
        screenshots: bool = False,
        timeout_ms: int = 60000,
    ):
        self.headless = headless
        self.debug = debug
        self.slow_mo_ms = slow_mo_ms
        self.trace = trace
        self.screenshots = screenshots
        self.timeout_ms = timeout_ms
        self.scroll_passes = scroll_passes
        self._semaphore = asyncio.Semaphore(max_concurrent_scrapes)
        self._routed_context_ids: set[int] = set()

    @asynccontextmanager
    async def shared_context(self) -> BrowserContext:
        """
        Shared browser/context for a job. Caller must close via context manager.
        """
        async with async_playwright() as p:
            headless = False if self.debug else self.headless
            slow_mo = self.slow_mo_ms if self.debug else 0
            browser = await p.chromium.launch(headless=headless, slow_mo=slow_mo, args=_CHROMIUM_ARGS)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=USER_AGENT,
            )
            try:
                yield context
            finally:
                await context.close()
                await browser.close()

    async def fetch_menu(
        self,
        store_url: str,
        restaurant_name: str = "Uber Eats Store",
        location: Optional[str] = None,
        shared_context: Optional[BrowserContext] = None,
    ) -> List[UberEatsMenuItem]:
        async with self._semaphore:
            store_id = _extract_store_id_from_url(store_url)
            retrieved_at = datetime.now(timezone.utc)

            logger.info("🛵 UberEatsScraper: scraping %s (%s)", restaurant_name, store_url)

            page = None
            playwright_instance = None
            browser = None
            context = None
            local_context = False

            try:
                if shared_context is not None:
                    context = shared_context
                    local_context = False
                else:
                    playwright_instance = await async_playwright().start()
                    headless = False if self.debug else self.headless
                    slow_mo = self.slow_mo_ms if self.debug else 0
                    browser = await playwright_instance.chromium.launch(
                        headless=headless, slow_mo=slow_mo, args=_CHROMIUM_ARGS
                    )
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="en-US",
                        user_agent=USER_AGENT,
                    )
                    local_context = True

                page = await context.new_page()
                page.set_default_timeout(min(self.timeout_ms, 60000))
                page.set_default_navigation_timeout(min(self.timeout_ms, 60000))

                await page.goto(
                    store_url,
                    wait_until="load",
                    timeout=max(self.timeout_ms or 60000, 90000),
                )

                ready_locator = page.locator('span[data-testid="rich-text"]')
                try:
                    await ready_locator.first.wait_for(state="visible", timeout=30000)
                except Exception:
                    await page.wait_for_timeout(10000)

                await _scroll_to_load_menu(page, self.scroll_passes)
                html = await page.content()

            except Exception as exc:
                if self.screenshots and page:
                    os.makedirs("screenshots", exist_ok=True)
                    try:
                        await page.screenshot(
                            path=os.path.join(
                                "screenshots",
                                f"ubereats-menu-fail-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.png",
                            ),
                            full_page=True,
                        )
                    except Exception:
                        pass
                logger.error("❌ Playwright scrape failed for %s: %s", store_url, exc)
                raise

            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
                if local_context:
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass
                    if browser:
                        try:
                            await browser.close()
                        except Exception:
                            pass
                    if playwright_instance:
                        try:
                            await playwright_instance.stop()
                        except Exception:
                            pass

            embedded = _extract_embedded_state(html)
            structured_items: List[UberEatsMenuItem] = []
            if embedded is not None:
                structured_items = _extract_items_from_embedded_json(
                    embedded,
                    restaurant_name=restaurant_name,
                    store_id=store_id,
                    retrieved_at=retrieved_at,
                    location=location,
                )

            structured_items = [
                it
                for it in structured_items
                if _is_valid_item_name(it.name) and 0 < it.price < 1000
            ]

            fallback_items: List[UberEatsMenuItem] = []
            parsed = parse_menu_from_html_fallback(html)
            for d in parsed:
                name = d.get("name")
                price = d.get("price_usd")
                if not name or price is None or price <= 0:
                    continue
                calories = _avg_or_single(d.get("calories_min"), d.get("calories_max"))
                fallback_items.append(
                    UberEatsMenuItem(
                        restaurant=restaurant_name,
                        name=str(name),
                        price=float(price),
                        category=None,
                        calories=calories,
                        protein_grams=d.get("protein_grams"),
                        store_external_id=store_id,
                        price_retrieved_at=retrieved_at,
                        location=location,
                        source_price_vendor="ubereats",
                    )
                )

            combined = structured_items + fallback_items
            if not combined:
                logger.warning("⚠️ No menu items parsed for %s", store_url)
                return []

            def score(it: UberEatsMenuItem) -> Tuple[int, int]:
                return (
                    1 if it.calories is not None else 0,
                    1 if it.protein_grams is not None else 0,
                )

            deduped: Dict[Tuple[str, Optional[str], float], UberEatsMenuItem] = {}
            for it in combined:
                key = (
                    it.name.strip().lower(),
                    it.category.lower().strip() if it.category else None,
                    round(it.price, 2),
                )
                if key not in deduped:
                    deduped[key] = it
                else:
                    if score(it) > score(deduped[key]):
                        deduped[key] = it

            final_items = list(deduped.values())

            logger.info(
                "✅ UberEatsScraper: %d structured, %d fallback => %d final items for %s (%s)",
                len(structured_items),
                len(fallback_items),
                len(final_items),
                store_id,
                store_url,
            )
            return final_items


ubereats_scraper = UberEatsScraper(
    headless=settings.ubereats_headless,
    max_concurrent_scrapes=1,
    scroll_passes=settings.ubereats_scroll_passes,
    debug=settings.ubereats_debug,
    slow_mo_ms=settings.ubereats_slow_mo_ms,
    trace=settings.ubereats_trace,
    screenshots=settings.ubereats_screenshots,
    timeout_ms=settings.ubereats_timeout_ms,
)
