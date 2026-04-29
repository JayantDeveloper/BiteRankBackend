"""Uber Eats store search via Playwright."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

HOME_URL = "https://www.ubereats.com/"

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


@dataclass
class UberEatsStore:
    name: str
    store_url: str
    store_id: str
    address: Optional[str] = None
    distance: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def _extract_store_id_from_url(url: str) -> str:
    path = urlparse(url).path
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 3 and segments[0] == "store":
        return segments[2]
    if len(segments) >= 2 and segments[0] == "store":
        return segments[1]
    return url


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


class UberEatsStoreSearch:
    """
    Playwright UI-based store discovery (bot typing),
    directly based on your scraping_mvp set_location + search_store_urls.
    """

    def __init__(self, *, max_concurrent: int = 1, timeout_ms: int = 30000):
        self._sem = asyncio.Semaphore(max_concurrent)
        self.timeout_ms = timeout_ms

    @asynccontextmanager
    async def shared_context(
        self,
        *,
        debug: bool = False,
        slow_mo_ms: int = 250,
        trace: bool = False,
        screenshots: bool = False,
    ) -> BrowserContext:
        """
        Shared browser/context for a job. Allows reuse across multiple restaurant searches.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=(not debug),
                slow_mo=(slow_mo_ms if debug else 0),
                args=_CHROMIUM_ARGS,
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )

            trace_path = None
            if trace:
                os.makedirs("traces", exist_ok=True)
                trace_path = os.path.join(
                    "traces",
                    f"ubereats-store-search-{_ts()}.zip",
                )
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)

            try:
                yield context
            finally:
                if trace_path:
                    try:
                        await context.tracing.stop(path=trace_path)
                    except Exception:
                        pass
                await context.close()
                await browser.close()

    async def search_stores(
        self,
        restaurant_name: str,
        location: str,
        *,
        limit: int = 1,
        debug: bool = False,
        slow_mo_ms: int = 250,
        trace: bool = False,
        screenshots: bool = False,
    ) -> List[UberEatsStore]:
        """
        Returns a list of UberEatsStore entries (store_url + store_id).
        Uses UI typing + first suggestion click for both:
          - location typeahead
          - restaurant search suggestions

        If shared_context is passed: uses it (does NOT close it).
        Else: creates local context and closes it.
        """
        restaurant_name = (restaurant_name or "").strip()
        location = (location or "").strip()
        if not restaurant_name or not location:
            return []

        async with self._sem:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=(not debug),
                    slow_mo=(slow_mo_ms if debug else 0),
                    args=_CHROMIUM_ARGS,
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )

                trace_path = None
                if trace:
                    os.makedirs("traces", exist_ok=True)
                    trace_path = os.path.join(
                        "traces",
                        f"ubereats-store-search-{_ts()}.zip",
                    )
                    await context.tracing.start(screenshots=True, snapshots=True, sources=True)

                page = await context.new_page()

                try:
                    await set_location(page, location, debug=debug)
                    store_urls = await search_store_urls(page, restaurant_name, limit=limit, debug=debug)

                    logger.info(
                        "UberEatsStoreSearch discovered %d store urls for %s near %s",
                        len(store_urls),
                        restaurant_name,
                        location,
                    )

                    stores: List[UberEatsStore] = []
                    for u in store_urls:
                        stores.append(
                            UberEatsStore(
                                name=restaurant_name,
                                store_url=u,
                                store_id=_extract_store_id_from_url(u),
                            )
                        )
                    return stores

                except Exception as exc:
                    logger.exception(
                        "UberEatsStoreSearch failed (restaurant=%s, location=%s): %s",
                        restaurant_name,
                        location,
                        exc,
                    )
                    if screenshots:
                        os.makedirs("screenshots", exist_ok=True)
                        try:
                            await page.screenshot(
                                path=os.path.join("screenshots", f"ubereats-store-search-fail-{_ts()}.png"),
                                full_page=True,
                            )
                        except Exception:
                            pass
                    return []

                finally:
                    if trace_path:
                        try:
                            await context.tracing.stop(path=trace_path)
                        except Exception:
                            pass
                    await context.close()
                    await browser.close()

    async def search_stores_bulk(
        self,
        restaurants: List[str],
        location: str,
        *,
        limit: int = 1,
        debug: bool = False,
        slow_mo_ms: int = 0,
        screenshots: bool = False,
        progress_callback=None,
    ) -> dict:
        """
        Opens ONE browser, sets location once, then searches for each restaurant in sequence.
        Returns Dict[restaurant_name, List[UberEatsStore]].
        On per-restaurant failure: logs and continues with empty list.
        progress_callback(restaurant, stores) is awaited after each restaurant resolves.
        """
        from typing import Dict
        results: Dict[str, List[UberEatsStore]] = {}

        if not restaurants or not location:
            return {r: [] for r in (restaurants or [])}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=(not debug),
                slow_mo=(slow_mo_ms if debug else 0),
                args=_CHROMIUM_ARGS,
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = await context.new_page()
            page.set_default_timeout(self.timeout_ms)

            try:
                await set_location(page, location, debug=debug)
            except Exception as exc:
                logger.error("Bulk location set failed for '%s': %s", location, exc)
                if screenshots:
                    os.makedirs("screenshots", exist_ok=True)
                    try:
                        await page.screenshot(
                            path=os.path.join("screenshots", f"ubereats-bulk-location-fail-{_ts()}.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                await context.close()
                await browser.close()
                empty = {r: [] for r in restaurants}
                if progress_callback:
                    for r in restaurants:
                        await progress_callback(r, [])
                return empty

            for restaurant in restaurants:
                stores: List[UberEatsStore] = []
                try:
                    urls = await search_store_urls(page, restaurant, limit=limit, debug=debug)
                    stores = [
                        UberEatsStore(
                            name=restaurant,
                            store_url=u,
                            store_id=_extract_store_id_from_url(u),
                        )
                        for u in urls
                    ]
                    logger.info(
                        "search_stores_bulk: found %d store(s) for %s near %s",
                        len(stores),
                        restaurant,
                        location,
                    )
                except Exception as exc:
                    logger.error(
                        "search_stores_bulk: search_store_urls failed for '%s': %s",
                        restaurant,
                        exc,
                    )

                results[restaurant] = stores
                if progress_callback:
                    await progress_callback(restaurant, stores)

            await context.close()
            await browser.close()

        return results


async def set_location(page: Page, delivery_address: str, debug: bool = False) -> None:
    """
    Opens Uber Eats and sets delivery address using the homepage typeahead.
    """
    logger.info("Setting UberEats location via homepage typeahead: %s", delivery_address)
    await page.goto(HOME_URL, wait_until="domcontentloaded")

    loc = page.locator("#location-typeahead-home-input")
    await loc.wait_for(state="visible", timeout=25000)
    await loc.click()
    await loc.fill(delivery_address)

    if debug:
        input_value = await loc.input_value()
        logger.info("[DEBUG] Location input filled with: %s", input_value)

    menu = page.locator("#location-typeahead-home-menu")
    options = menu.locator('[role="option"]')

    # Wait for dropdown + at least one option to exist
    await options.first.wait_for(state="visible", timeout=25000)

    # Wait until the first option is actually populated with text
    first_handle = await options.first.element_handle()
    await page.wait_for_function(
        "(el) => el && el.innerText && el.innerText.trim().length > 0",
        arg=first_handle,
        timeout=25000,
    )

    if debug:
        suggestion_text = await options.first.inner_text()
        logger.info("[DEBUG] First location suggestion: %s", suggestion_text)

    await page.wait_for_timeout(250)

    await options.first.click()
    logger.info("Location suggestion clicked; waiting for global search bar")

    try:
        await page.locator('[data-testid="search-input"]').wait_for(state="visible", timeout=30000)
    except Exception as exc:
        logger.warning("Search bar not ready after location select; retrying on feed page: %s", exc)
        try:
            await page.goto(HOME_URL + "feed", wait_until="domcontentloaded")
        except Exception:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
        await page.locator('[data-testid="search-input"]').wait_for(state="visible", timeout=45000)

async def ensure_location(page: Page, delivery_address: str, debug: bool = False) -> None:
    """
    Only set location if the global search bar isn't already visible.
    This avoids retyping location when UberEats session already has it stored.
    """
    try:
        await page.locator('[data-testid="search-input"]').wait_for(state="visible", timeout=3000)
        return
    except Exception:
        pass
    await set_location(page, delivery_address, debug=debug)


async def search_store_urls(page: Page, restaurant_query: str, limit: int = 3, debug: bool = False) -> List[str]:
    """
    Searches for a restaurant and returns up to `limit` store URLs.
    Uses the typeahead suggestions instead of pressing Enter (more reliable on Uber Eats).
    """
    logger.info("Starting UberEats search for restaurant: %s", restaurant_query)
    try:
        logger.info("[UE-SEARCH] Page before search: %s", page.url)
    except Exception:
        pass
    search = page.locator('[data-testid="search-input"]')
    await search.wait_for(state="visible", timeout=30000)
    await search.scroll_into_view_if_needed()

    try:
        await search.click(force=True)
    except Exception:
        try:
            await search.focus()
        except Exception:
            pass

    await search.fill("")
    await search.type(restaurant_query, delay=40)  # slight human-ish typing helps

    if debug:
        input_value = await search.input_value()
        logger.info("[DEBUG] Search input filled with: %s", input_value)

    menu = page.locator("#search-suggestions-typeahead-menu")
    options = menu.locator('[role="option"]')

    try:
        await options.first.wait_for(state="visible", timeout=15000)

        first_handle = await options.first.element_handle()
        await page.wait_for_function(
            "(el) => el && el.innerText && el.innerText.trim().length > 0",
            arg=first_handle,
            timeout=15000,
        )

        if debug:
            suggestion_text = await options.first.inner_text()
            logger.info("[DEBUG] First restaurant suggestion: %s", suggestion_text)

        await page.wait_for_timeout(200)
        await options.first.click()
    except Exception:
        try:
            await search.click(force=True)
            await search.fill("")
            await search.type(restaurant_query, delay=40)
            await options.first.wait_for(state="visible", timeout=8000)
            await options.first.click()
        except Exception:
            logger.warning("Restaurant suggestions did not appear, falling back to Enter")
            await search.press("Enter")

    store_links = page.locator('a[href*="/store/"]')
    await store_links.first.wait_for(state="visible", timeout=30000)
    try:
        logger.info("[UE-SEARCH] Landed on search results page: %s", page.url)
    except Exception:
        pass

    urls: List[str] = []
    count = await store_links.count()

    for i in range(min(count, 60)):
        href = await store_links.nth(i).get_attribute("href")
        if not href or "/store/" not in href:
            continue

        full = "https://www.ubereats.com" + href if href.startswith("/") else href
        full = full.split("?")[0]

        if full not in urls:
            urls.append(full)
        if len(urls) >= limit:
            break

    logger.info("UberEats search found %d matching store URLs for %s", len(urls), restaurant_query)
    if urls:
        logger.info("UberEats store URLs (first %d): %s", min(len(urls), limit), urls[:limit])
    return urls


ubereats_store_search = UberEatsStoreSearch(max_concurrent=1)
