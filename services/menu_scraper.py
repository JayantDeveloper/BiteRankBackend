import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

USER_AGENT = (
    "DealScoutBot/0.1 (+https://dealscout.example; contact: support@dealscout.local)"
)


@dataclass
class ScrapedMenuItem:
    restaurant: str
    name: str
    price: Optional[float] = None
    calories: Optional[int] = None
    protein_grams: Optional[float] = None
    category: Optional[str] = None
    source_url: Optional[str] = None
    raw: Optional[dict] = None

    def to_dict(self) -> dict:
        """Serialize to JSON-friendly dict without None values."""
        data = {k: v for k, v in asdict(self).items() if v is not None}
        return data


class MenuScraper:
    """
    Lightweight HTML scraper tuned for the public menu pages of
    several US fast-food chains. Relies on structured data (JSON-LD)
    when available and falls back to simple CSS/regex heuristics.
    """

    TARGETS: Dict[str, Dict[str, str]] = {
        "mcdonalds": {
            "restaurant": "McDonald's",
            "url": "https://www.mcdonalds.com/us/en-us/full-menu.html",
        },
        "taco-bell": {
            "restaurant": "Taco Bell",
            "url": "https://www.tacobell.com/food",
        },
        "wendys": {
            "restaurant": "Wendy's",
            "url": "https://www.wendys.com/menu",
        },
        "burger-king": {
            "restaurant": "Burger King",
            "url": "https://www.bk.com/menu",
        },
        "chick-fil-a": {
            "restaurant": "Chick-fil-A",
            "url": "https://www.chick-fil-a.com/menu",
        },
        "subway": {
            "restaurant": "Subway",
            "url": "https://www.subway.com/en-us/menunutrition/menu",
        },
    }

    def __init__(self, *, timeout: float = 20.0):
        self.timeout = timeout

    async def scrape_restaurant(self, slug: str) -> List[ScrapedMenuItem]:
        target = self.TARGETS.get(slug)
        if not target:
            raise ValueError(f"Unknown restaurant slug '{slug}'")

        html = await self._fetch(target["url"])
        soup = BeautifulSoup(html, "html.parser")
        restaurant_name = target["restaurant"]

        items = self._parse_structured_data(restaurant_name, target["url"], soup)

        if not items:
            logger.info("Structured data not found for %s, falling back to heuristics", slug)
            items = self._parse_with_heuristics(restaurant_name, target["url"], soup)

        if not items:
            logger.warning("No menu items extracted for %s", slug)

        return items

    async def scrape_all(self, slugs: Optional[Iterable[str]] = None) -> Dict[str, List[dict]]:
        slugs = list(slugs) if slugs else list(self.TARGETS.keys())
        tasks = [self.scrape_restaurant(slug) for slug in slugs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        aggregated: Dict[str, List[dict]] = {}
        for slug, result in zip(slugs, results):
            if isinstance(result, Exception):
                logger.error("Failed to scrape %s: %s", slug, result)
                aggregated[slug] = []
                continue
            aggregated[slug] = [item.to_dict() for item in result]

        return aggregated

    async def _fetch(self, url: str) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            logger.info("Fetched %s (%d bytes)", url, len(response.content))
            return response.text

    def _parse_structured_data(
        self, restaurant: str, url: str, soup: BeautifulSoup
    ) -> List[ScrapedMenuItem]:
        items: List[ScrapedMenuItem] = []

        for script in soup.find_all("script", {"type": "application/ld+json"}):
            text = script.string or script.get_text()
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            for entry in self._flatten_ldjson(data):
                menu_item = self._coerce_menu_item(entry)
                if menu_item:
                    items.append(
                        ScrapedMenuItem(
                            restaurant=restaurant,
                            name=menu_item["name"],
                            price=menu_item.get("price"),
                            calories=menu_item.get("calories"),
                            protein_grams=menu_item.get("protein"),
                            category=menu_item.get("category"),
                            source_url=url,
                            raw=entry if isinstance(entry, dict) else None,
                        )
                    )

        logger.info("Extracted %d structured items for %s", len(items), restaurant)
        return items

    def _flatten_ldjson(self, data) -> Iterable[dict]:
        if isinstance(data, dict):
            yield data
            for key in ("hasMenu", "hasMenuItem", "hasMenuSection", "itemListElement"):
                if key in data:
                    for child in self._flatten_ldjson(data[key]):
                        yield child
        elif isinstance(data, list):
            for entry in data:
                for child in self._flatten_ldjson(entry):
                    yield child

    def _coerce_menu_item(self, entry) -> Optional[Dict[str, Optional[float]]]:
        if not isinstance(entry, dict):
            return None

        type_value = entry.get("@type") or entry.get("type")

        if isinstance(type_value, list):
            types = {t.lower() for t in type_value if isinstance(t, str)}
        else:
            types = {type_value.lower()} if isinstance(type_value, str) else set()

        if not {"menuitem", "listitem"}.intersection(types):
            # Some schemas store the actual item under item/hasMenuItem
            if "item" in entry and isinstance(entry["item"], dict):
                return self._coerce_menu_item(entry["item"])
            return None

        name = entry.get("name")
        if not name or not isinstance(name, str):
            return None

        category = entry.get("category")

        price = self._extract_price(entry)
        calories, protein = self._extract_nutrition(entry)

        return {
            "name": name.strip(),
            "price": price,
            "calories": calories,
            "protein": protein,
            "category": category.strip() if isinstance(category, str) else None,
        }

    def _extract_price(self, entry: dict) -> Optional[float]:
        offers = entry.get("offers")
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("priceSpecification", {}).get("price")
            currency = offers.get("priceCurrency") or offers.get("priceSpecification", {}).get(
                "priceCurrency"
            )
            return self._parse_price(price, currency)
        if isinstance(offers, list):
            for offer in offers:
                price = self._extract_price({"offers": offer})
                if price is not None:
                    return price

        price = entry.get("price") or entry.get("priceSpecification", {}).get("price")
        return self._parse_price(price, entry.get("priceCurrency"))

    def _extract_nutrition(self, entry: dict) -> Tuple[Optional[int], Optional[float]]:
        nutrition = entry.get("nutrition")

        if isinstance(nutrition, dict):
            calories = nutrition.get("calories") or nutrition.get("calorieContent")
            protein = (
                nutrition.get("proteinContent")
                or nutrition.get("protein")
                or nutrition.get("proteinContentValue")
            )
            return self._parse_calories(calories), self._parse_protein(protein)

        return None, None

    def _parse_with_heuristics(
        self, restaurant: str, url: str, soup: BeautifulSoup
    ) -> List[ScrapedMenuItem]:
        items: List[ScrapedMenuItem] = []

        selectors = [
            ("[data-qa='menu-item']", ".//text()"),
            ("[data-test='menu-item']", ".//text()"),
            (".menu-item", None),
            (".menu__item", None),
            (".product", None),
            ("[class*='MenuItem']", None),
        ]

        seen_names = set()
        for css_selector, _ in selectors:
            for element in soup.select(css_selector):
                name = element.get("data-name") or element.get("aria-label")
                if not name:
                    headline = element.find(["h2", "h3", "h4", "span"], class_=re.compile("title|name", re.I))
                    if headline and headline.get_text(strip=True):
                        name = headline.get_text(strip=True)
                if not name:
                    continue
                name = name.strip()
                if name in seen_names or not self._looks_like_menu_item(name):
                    continue
                seen_names.add(name)

                price = self._parse_price_from_text(element.get_text(" ", strip=True))

                items.append(
                    ScrapedMenuItem(
                        restaurant=restaurant,
                        name=name,
                        price=price,
                        source_url=url,
                    )
                )

        if not items:
            # Last resort: scan plain text for "$" patterns
            text_items = self._guess_items_from_text(soup.get_text("\n"))
            for name, price in text_items:
                if name in seen_names:
                    continue
                items.append(
                    ScrapedMenuItem(
                        restaurant=restaurant,
                        name=name,
                        price=price,
                        source_url=url,
                    )
                )

        logger.info("Extracted %d heuristic items for %s", len(items), restaurant)
        return items

    def _parse_price(self, value, currency: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        if isinstance(value, str):
            match = re.search(r"(\d+(?:\.\d{1,2})?)", value.replace(",", ""))
            if match:
                return round(float(match.group(1)), 2)
        return None

    def _parse_calories(self, value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"(\d{2,4})", value.replace(",", ""))
            if match:
                return int(match.group(1))
        return None

    def _parse_protein(self, value) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            match = re.search(r"(\d+(?:\.\d+)?)", value)
            if match:
                return float(match.group(1))
        return None

    def _parse_price_from_text(self, text: str) -> Optional[float]:
        if not text:
            return None
        match = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", text)
        if match:
            try:
                return round(float(match.group(1)), 2)
            except ValueError:
                return None
        return None

    def _guess_items_from_text(self, text: str) -> List[Tuple[str, Optional[float]]]:
        items = []
        lines = [line.strip() for line in text.splitlines()]
        for line in lines:
            if not line or "$" not in line:
                continue
            if len(line) > 120:
                continue
            price = self._parse_price_from_text(line)
            cleaned = re.sub(r"\s*\$?\d+(?:\.\d{1,2})?\s*", "", line)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" •:-")
            if self._looks_like_menu_item(cleaned):
                items.append((cleaned, price))
        return items

    def _looks_like_menu_item(self, text: str) -> bool:
        if not text:
            return False
        words = text.split()
        if len(words) < 2 or len(words) > 8:
            return False
        if text.lower() in {"menu", "learn more", "order now"}:
            return False
        return True


# Singleton instance for reuse across API calls
menu_scraper = MenuScraper()

