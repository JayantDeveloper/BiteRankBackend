import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "DealScoutBot/0.1 (+https://dealscout.example; contact: support@dealscout.local)"
)


@dataclass
class UberEatsMenuItem:
    restaurant: str
    name: str
    price: float
    category: Optional[str] = None
    source_price_vendor: str = "ubereats"
    store_external_id: Optional[str] = None
    price_retrieved_at: datetime = datetime.now(timezone.utc)
    location: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        # datetime is not JSON serializable; convert to isoformat for responses
        data["price_retrieved_at"] = self.price_retrieved_at.isoformat()
        return data


class UberEatsScraper:
    """
    Lightweight scraper for Uber Eats store pages.
    Strategy: extract state payloads embedded in <script> tags and walk them for menu items.
    Falls back to scanning inline JSON blobs that resemble GraphQL caches.
    """

    STATE_PATTERNS = [
        re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"window\.__APP_INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"self\.__NUXT__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*type=[\"']application/json[\"'][^>]*>(.*?)</script>", re.DOTALL),
        re.compile(r"window\.__NEXT_DATA__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"window\.__RELAY_STORE__\s*=\s*(\{.*?\});", re.DOTALL),
        re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});", re.DOTALL),
    ]

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def fetch_menu(self, store_url: str, restaurant_name: str = "McDonald's", location: Optional[str] = None) -> List[UberEatsMenuItem]:
        html = await self._fetch(store_url)
        store_id = self._extract_store_id(store_url)

        payloads = self._extract_state_payloads(html)
        items: List[UberEatsMenuItem] = []
        for payload in payloads:
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue
            extracted = list(self._extract_items(parsed))
            if extracted:
                logger.info("Extracted %d items from Uber Eats payload", len(extracted))
            for item in extracted:
                price = self._parse_price(item.get("price"))
                if price is None:
                    continue
                name = self._clean_name(item.get("name"))
                if not name:
                    continue
                category = self._clean_name(item.get("category"))
                items.append(
                    UberEatsMenuItem(
                        restaurant=restaurant_name,
                        name=name,
                        price=price,
                        category=category,
                        store_external_id=store_id,
                        location=location,
                    )
                )

        # Deduplicate by name/category
        deduped: Dict[Tuple[str, Optional[str]], UberEatsMenuItem] = {}
        for item in items:
            key = (item.name.lower(), item.category.lower() if item.category else None)
            if key not in deduped:
                deduped[key] = item

        logger.info("Uber Eats scraper returning %d unique items", len(deduped))
        return list(deduped.values())

    async def _fetch(self, url: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8",
            "Referer": "https://www.ubereats.com/",
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            logger.info("Fetched Uber Eats store page: %s (%d bytes)", url, len(response.text))

            # Debug: Save first 5000 chars to see structure
            html_preview = response.text[:5000]
            if "ubereats" in url.lower():
                logger.debug(f"HTML preview (first 5000 chars):\n{html_preview}")

            return response.text

    def _extract_state_payloads(self, html: str) -> List[str]:
        payloads: List[str] = []

        # Direct regex matches
        for pattern in self.STATE_PATTERNS:
            matches = list(pattern.finditer(html))
            if matches:
                logger.info(f"Found {len(matches)} matches with pattern {pattern.pattern[:50]}...")
            for match in matches:
                payloads.append(match.group(1))

        # Parse <script> tags as fallback
        if not payloads:
            logger.warning("No direct pattern matches found, parsing script tags...")
            soup = BeautifulSoup(html, "html.parser")
            scripts = soup.find_all("script")
            logger.info(f"Found {len(scripts)} script tags total")

            for script in scripts:
                text = script.string or script.get_text()
                if not text:
                    continue

                # Check for specific markers
                has_relay = "__RELAY_STORE__" in text
                has_apollo = "__APOLLO_STATE__" in text
                has_initial = "__INITIAL_STATE__" in text
                has_next = "__NEXT_DATA__" in text

                if has_relay or has_apollo or has_initial or has_next:
                    logger.info(f"Found promising script tag with markers: relay={has_relay}, apollo={has_apollo}, initial={has_initial}, next={has_next}")

                for pattern in self.STATE_PATTERNS:
                    match = pattern.search(text)
                    if match:
                        logger.info(f"Pattern matched in script tag: {pattern.pattern[:50]}...")
                        payloads.append(match.group(1))

                if has_relay or has_apollo:
                    possible = self._extract_json_from_script(text)
                    logger.info(f"Extracted {len(possible)} JSON objects from script")
                    payloads.extend(possible)

        logger.info(f"Total payloads extracted: {len(payloads)}")
        return payloads

    def _extract_json_from_script(self, script_text: str) -> List[str]:
        """Attempt to pull inline JSON objects from a script when no direct pattern matches."""
        payloads: List[str] = []
        stack = []
        start = None
        for idx, char in enumerate(script_text):
            if char == "{":
                if not stack:
                    start = idx
                stack.append("{")
            elif char == "}":
                if stack:
                    stack.pop()
                    if not stack and start is not None:
                        payloads.append(script_text[start : idx + 1])
                        start = None
        return payloads

    def _extract_items(self, payload) -> Iterable[Dict[str, Optional[str]]]:
        """Walk arbitrary nested JSON to find menu item-like structures."""

        def walk(node, current_category=None):
            if isinstance(node, dict):
                name = node.get("title") or node.get("name") or node.get("displayName")
                if name and isinstance(name, str):
                    # Determine category candidate if node represents a section
                    node_type = node.get("type") or node.get("__typename")
                    if (
                        node_type
                        and isinstance(node_type, str)
                        and node_type.lower() in {"category", "section", "menuitemsection"}
                    ):
                        current_category_local = name
                    else:
                        current_category_local = current_category
                else:
                    current_category_local = current_category

                if self._looks_like_item(node):
                    yield {
                        "name": name or node.get("shortName"),
                        "price": self._extract_price_field(node),
                        "category": current_category,
                    }

                for key, value in node.items():
                    next_category = current_category_local
                    if key.lower() in {"category", "section", "menu_items_section"} and isinstance(
                        value, dict
                    ):
                        title = value.get("title") or value.get("name")
                        if isinstance(title, str):
                            next_category = title
                    yield from walk(value, next_category)

            elif isinstance(node, list):
                for item in node:
                    yield from walk(item, current_category)

        return walk(payload)

    def _looks_like_item(self, node: dict) -> bool:
        if not isinstance(node, dict):
            return False
        name = node.get("title") or node.get("name") or node.get("displayName")
        price = self._extract_price_field(node)
        return isinstance(name, str) and price is not None

    def _extract_price_field(self, node: dict):
        candidates = [
            node.get("price"),
            node.get("priceInfo", {}).get("price"),
            node.get("priceInfo", {}).get("unitPrice"),
            node.get("itemPrice", {}).get("price"),
            node.get("priceTag"),
            node.get("amount"),
            node.get("price_in_cents"),
            node.get("priceInCents"),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                # Some nested objects store cents as 'amount'
                amount = candidate.get("amount") or candidate.get("price")
                if amount is not None:
                    return amount
                continue
            return candidate
        return None

    def _parse_price(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            amount = float(value)
            if amount > 20:  # Likely cents
                return round(amount / 100.0, 2)
            return round(amount, 2)
        if isinstance(value, str):
            match = re.search(r"(\d+(?:\.\d{1,2})?)", value.replace(",", ""))
            if match:
                amount = float(match.group(1))
                return round(amount, 2)
        return None

    def _clean_name(self, text: Optional[str]) -> Optional[str]:
        if not text or not isinstance(text, str):
            return None
        cleaned = re.sub(r"[®™]", "", text)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _extract_store_id(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments:
            return segments[-1]
        return None


# Singleton
ubereats_scraper = UberEatsScraper()

