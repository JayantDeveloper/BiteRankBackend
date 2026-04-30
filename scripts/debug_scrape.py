"""
Standalone debug script — run from backend/ directory:
    python scripts/debug_scrape.py

Does NOT require the FastAPI server. Runs Playwright HEADED so you can
watch, captures HTML/JSON to /tmp/biterank_debug/ for inspection.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("debug_scrape")
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.INFO)

from playwright.async_api import async_playwright
from services.ubereats_store_search import UberEatsStoreSearch, set_location, search_store_urls
from services.ubereats_scraper import UberEatsScraper, _extract_embedded_state, parse_menu_from_html_fallback

LOCATION   = "10001"          # NYC zip
RESTAURANT = "McDonald's"
OUT_DIR    = Path("/tmp/biterank_debug")
OUT_DIR.mkdir(exist_ok=True)
HEADLESS   = False             # watch the browser


async def main():
    logger.info("=== BiteRank Debug Scrape ===  loc=%s  restaurant=%s", LOCATION, RESTAURANT)

    # ── Step 1: Store search ──────────────────────────────────────────────────
    logger.info("\n── STEP 1: Store search ──")
    searcher = UberEatsStoreSearch()
    try:
        stores = await searcher.search_stores(
            restaurant_name=RESTAURANT,
            location=LOCATION,
            limit=1,
            debug=not HEADLESS,
            slow_mo_ms=150 if not HEADLESS else 0,
        )
        logger.info("Found %d store(s): %s", len(stores), [s.store_url for s in stores])
    except Exception as e:
        logger.exception("Store search FAILED: %s", e)
        stores = []

    if not stores:
        logger.error("❌ No stores found — location/search selectors are broken. Stopping.")
        return

    store_url = stores[0].store_url
    logger.info("Store URL: %s", store_url)

    # ── Step 2: Raw page inspection ───────────────────────────────────────────
    logger.info("\n── STEP 2: Raw page inspection ──")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=100 if not HEADLESS else 0,
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        try:
            await page.goto(store_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning("goto raised (continuing): %s", e)

        # Immediately check what testids exist
        try:
            testids = await page.evaluate("""
                () => [...document.querySelectorAll('[data-testid]')]
                       .map(e => e.dataset.testid)
                       .filter((v,i,a) => a.indexOf(v)===i)
                       .sort()
            """)
            logger.info("data-testid attrs (immediately): %s", testids)
        except Exception as e:
            logger.warning("testid check failed: %s", e)

        logger.info("Waiting 5s for JS…")
        await page.wait_for_timeout(5000)

        # Scroll to trigger lazy-loading
        for _ in range(6):
            await page.mouse.wheel(0, 1000)
            await page.wait_for_timeout(600)

        # Check rich-text spans
        for sel in [
            'span[data-testid="rich-text"]',
            'li[data-testid]',
            '[data-testid*="item"]',
            '[data-testid*="menu"]',
            '[data-testid*="section"]',
        ]:
            try:
                count = await page.locator(sel).count()
                logger.info("Selector %-45s → %d elements", repr(sel), count)
            except Exception as e:
                logger.warning("Selector %s failed: %s", sel, e)

        # Full data-testid list after scroll
        try:
            testids_after = await page.evaluate("""
                () => [...document.querySelectorAll('[data-testid]')]
                       .map(e => e.dataset.testid)
                       .filter((v,i,a) => a.indexOf(v)===i)
                       .sort()
            """)
            logger.info("data-testid attrs (after scroll): %s", testids_after)
        except Exception as e:
            logger.warning("testid-after check failed: %s", e)

        # Capture page source
        html = await page.content()
        html_path = OUT_DIR / "store_page.html"
        html_path.write_text(html, encoding="utf-8")
        logger.info("HTML saved (%d bytes) → %s", len(html), html_path)

        ss_path = OUT_DIR / "store_page.png"
        await page.screenshot(path=str(ss_path), full_page=False)
        logger.info("Screenshot → %s", ss_path)

        await ctx.close()
        await browser.close()

    # ── Step 3: Embedded JSON ─────────────────────────────────────────────────
    logger.info("\n── STEP 3: Embedded JSON ──")
    embedded = _extract_embedded_state(html)
    if embedded is None:
        logger.error("❌ No embedded JSON found!")
        for var in ["__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__"]:
            logger.info("  '%s' in HTML: %s", var, var in html)
        import re as _re
        script_ids = _re.findall(r'<script[^>]+id="([^"]+)"', html)
        logger.info("  <script id=…> found: %s", script_ids)
    else:
        emb_path = OUT_DIR / "embedded_state.json"
        emb_path.write_text(json.dumps(embedded, indent=2, default=str)[:5_000_000], encoding="utf-8")
        logger.info("✅ Embedded JSON → %s  (top-level keys: %s)", emb_path,
                    list(embedded.keys())[:20] if isinstance(embedded, dict) else type(embedded).__name__)

    # ── Step 4: fetch_menu ────────────────────────────────────────────────────
    logger.info("\n── STEP 4: fetch_menu ──")
    scraper = UberEatsScraper(
        headless=HEADLESS,
        debug=not HEADLESS,
        slow_mo_ms=100 if not HEADLESS else 0,
        scroll_passes=8,
        timeout_ms=40000,
    )
    try:
        items = await scraper.fetch_menu(store_url=store_url, restaurant_name=RESTAURANT, location=LOCATION)
    except Exception as e:
        logger.exception("fetch_menu FAILED: %s", e)
        items = []

    logger.info("fetch_menu → %d items", len(items))
    for it in items[:25]:
        logger.info("  %-45s  $%-6.2f  cal=%-5s  protein=%-5s  cat=%s",
                    it.name[:45], it.price, it.calories, it.protein_grams, it.category)
    if len(items) > 25:
        logger.info("  … and %d more", len(items) - 25)

    # ── Step 5: HTML fallback ─────────────────────────────────────────────────
    logger.info("\n── STEP 5: HTML fallback parse ──")
    html_items = parse_menu_from_html_fallback(html)
    logger.info("HTML fallback → %d items", len(html_items))
    for it in html_items[:10]:
        logger.info("  %s → $%s", it.get("name"), it.get("price_usd"))

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("\n═══ SUMMARY ═══")
    logger.info("Store found:         %s  (%s)", bool(stores), store_url)
    logger.info("Embedded JSON:       %s", embedded is not None)
    logger.info("fetch_menu items:    %d", len(items))
    logger.info("HTML fallback items: %d", len(html_items))


if __name__ == "__main__":
    asyncio.run(main())
