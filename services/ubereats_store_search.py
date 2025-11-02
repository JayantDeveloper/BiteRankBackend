import asyncio
import json
import logging
import re
import base64
from typing import List, Optional, Tuple
from dataclasses import dataclass
from urllib.parse import quote_plus, urlencode, urlparse, parse_qsl, urlunparse
from html import unescape

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class UberEatsStore:
    name: str
    store_url: str
    store_id: str
    address: Optional[str] = None
    distance: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class UberEatsStoreSearch:
    """
    Search for Uber Eats store URLs by restaurant name and location.
    Uses Uber Eats search/discovery endpoints to find matching stores.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.base_url = "https://www.ubereats.com"

    async def geocode_location(self, location: str) -> Tuple[Optional[float], Optional[float]]:
        return await self._geocode_location(location)

    async def search_stores(
        self,
        restaurant_name: str,
        location: str,
        *,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> List[UberEatsStore]:
        """
        Search for Uber Eats stores matching the restaurant name and location.

        Args:
            restaurant_name: Name of the restaurant (e.g., "McDonald's")
            location: Location string (ZIP code or "City, State")

        Returns:
            List of matching UberEatsStore objects
        """
        try:
            # First, try to geocode the location to get coordinates
            if lat is None or lon is None:
                lat, lon = await self._geocode_location(location)

            if not lat or not lon:
                logger.warning(f"Could not geocode location: {location}")
                return []

            # Search for stores using the Uber Eats API
            stores = await self._search_uber_eats(restaurant_name, location, lat, lon)

            logger.info(
                f"Found {len(stores)} Uber Eats stores for '{restaurant_name}' near {location}"
            )

            return stores

        except Exception as e:
            logger.error(f"Error searching Uber Eats stores: {e}")
            return []

    async def _geocode_location(self, location: str) -> tuple[Optional[float], Optional[float]]:
        """
        Convert location string to latitude/longitude coordinates.
        Uses Nominatim (OpenStreetMap) for geocoding.
        """
        try:
            # Check if it's a ZIP code
            if re.match(r'^\d{5}(-\d{4})?$', location.strip()):
                # Add USA for ZIP codes
                search_query = f"{location}, USA"
            else:
                search_query = location

            url = f"https://nominatim.openstreetmap.org/search?format=json&q={quote_plus(search_query)}&limit=1"

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "DealScout/1.0"}
                )
                response.raise_for_status()

                data = response.json()
                if data and len(data) > 0:
                    lat = float(data[0]["lat"])
                    lon = float(data[0]["lon"])
                    logger.info(f"Geocoded '{location}' to ({lat}, {lon})")
                    return lat, lon

            return None, None

        except Exception as e:
            logger.error(f"Geocoding error for '{location}': {e}")
            return None, None

    def _build_pl_parameter(self, location: str, lat: float, lon: float) -> str:
        payload = {
            "address": location,
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
            "reference": f"manual:{round(lat,6)},{round(lon,6)}",
            "referenceType": "manual",
        }
        json_payload = json.dumps(payload, separators=(",", ":"))
        encoded = base64_urlsafe_encode(json_payload.encode("utf-8"))
        return quote_plus(encoded)

    async def _search_uber_eats(
        self, restaurant_name: str, location: str, lat: float, lon: float
    ) -> List[UberEatsStore]:
        """
        Search Uber Eats for stores matching the restaurant name near the coordinates.
        Uses multiple strategies to find stores.
        """
        stores = []

        try:
            # Strategy 1: Search via web interface and parse to get real store URLs
            pl_param = self._build_pl_parameter(location, lat, lon)
            query_params = {
                "q": restaurant_name,
                "pl": pl_param,
                "diningMode": "DELIVERY",
                "ps": "1",
                "sc": "SEARCH_SUGGESTION",
            }
            search_url = f"{self.base_url}/search?{urlencode(query_params)}"

            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            ) as client:
                response = await client.get(search_url)
                response.raise_for_status()

                html = response.text

                # Parse the HTML to extract store information
                stores = self._parse_search_results(html, restaurant_name, location, lat, lon)
                if not stores:
                    logger.debug(
                        "Uber Eats search HTML sample for %s: %s",
                        restaurant_name,
                        html[:1000].replace("\n", " "),
                    )

            # Strategy 2: If no stores found via parsing, try direct store URL pattern as fallback
            if not stores:
                logger.warning(f"No stores found via search, trying direct URL pattern for {restaurant_name}")
                stores_from_url = await self._try_direct_store_url(restaurant_name, location, lat, lon)
                if stores_from_url:
                    stores.extend(stores_from_url)

        except Exception as e:
            logger.error(f"Error searching Uber Eats: {e}")

        return stores

    async def _try_direct_store_url(
        self, restaurant_name: str, location: str, lat: float, lon: float
    ) -> List[UberEatsStore]:
        """
        Try to construct a direct store URL for common restaurant chains.
        """
        stores = []

        # Common restaurant chain URL patterns
        # These are based on typical Uber Eats store slug formats
        slug_map = {
            "mcdonald's": "mcdonalds",
            "mcdonalds": "mcdonalds",
            "burger king": "burger-king",
            "wendy's": "wendys",
            "wendys": "wendys",
            "taco bell": "taco-bell",
            "chipotle": "chipotle-mexican-grill",
            "subway": "subway",
            "kfc": "kfc",
            "popeyes": "popeyes",
            "chick-fil-a": "chick-fil-a",
            "five guys": "five-guys",
            "sonic": "sonic-drive-in",
            "arby's": "arbys",
            "arbys": "arbys",
            "panda express": "panda-express",
            "panera": "panera-bread",
            "panera bread": "panera-bread",
        }

        normalized_name = restaurant_name.lower().strip()
        base_slug = slug_map.get(normalized_name)

        if base_slug:
            # Try to find a store with this slug near the location
            # For now, we'll create a generic store entry
            # In a production system, you'd want to validate this exists
            pl_param = self._build_pl_parameter(location, lat, lon)
            store_url = f"{self.base_url}/store/{base_slug}?diningMode=DELIVERY&pl={pl_param}"

            stores.append(
                UberEatsStore(
                    name=restaurant_name.title(),
                    store_url=store_url,
                    store_id=base_slug,
                    address=f"Near {lat}, {lon}",
                    latitude=lat,
                    longitude=lon,
                )
            )

        return stores

    def _encode_store_url(self, path: str, pl_param: str) -> str:
        parsed = urlparse(path)
        if parsed.scheme:
            url_parts = list(parsed)
        else:
            url_parts = list(urlparse(f"{self.base_url}{path}"))

        query = dict(parse_qsl(url_parts[4]))
        query.setdefault("diningMode", "DELIVERY")
        query["pl"] = pl_param
        url_parts[4] = urlencode(query)
        return urlunparse(url_parts)

    def _parse_search_results(
        self,
        html: str,
        restaurant_name: str,
        location: str,
        lat: float,
        lon: float,
    ) -> List[UberEatsStore]:
        """
        Parse Uber Eats search results HTML to extract store URLs.
        This looks for embedded JSON data structures and store links in the page.
        """
        stores = []

        try:
            # Strategy 1: Look for direct store URLs in href attributes
            # Pattern: /store/restaurant-name-location/uuid
            store_url_pattern = re.compile(
                r'href=[\"\\\'](/store/([^\"\\\']+))[\"\\\']',
                re.IGNORECASE
            )

            matches = store_url_pattern.findall(html)
            restaurant_name_lower = restaurant_name.lower()
            pl_param = self._build_pl_parameter(location, lat, lon)

            for match in matches:
                store_path = match[0]  # /store/mcdonalds-college-park/le7gZEtjRyexb-hyyq6w4w
                store_slug_with_id = match[1]  # mcdonalds-college-park/le7gZEtjRyexb-hyyq6w4w

                store_path = unescape(store_path)
                store_slug_with_id = unescape(store_slug_with_id)

                # Check if the slug contains the restaurant name
                if restaurant_name_lower.replace(" ", "-") in store_slug_with_id.lower() or \
                   restaurant_name_lower.replace("'", "") in store_slug_with_id.lower():

                    # Extract store name and ID
                    parts = store_slug_with_id.split('/')
                    if len(parts) >= 2:
                        store_name_slug = parts[0]
                        store_id = parts[1]

                        # Clean up query parameters if present
                        store_id = store_id.split('?')[0]

                        full_url = self._encode_store_url(store_path.split('?')[0], pl_param)

                        stores.append(UberEatsStore(
                            name=store_name_slug.replace('-', ' ').title(),
                            store_url=full_url,
                            store_id=store_id,
                            address=None,
                            latitude=lat,
                            longitude=lon,
                        ))

            # Strategy 2: Look for store data in embedded JSON (original approach)
            if not stores:
                patterns = [
                    re.compile(r'window\.__NUXT__\s*=\s*(\{.*?\});', re.DOTALL),
                    re.compile(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', re.DOTALL),
                    re.compile(r'window\.__NEXT_DATA__\s*=\s*(\{.*?\});', re.DOTALL),
                ]

                for pattern in patterns:
                    matches = pattern.finditer(html)
                    for match in matches:
                        try:
                            json_str = match.group(1)
                            data = json.loads(json_str)

                            # Extract stores from the data structure
                            extracted_stores = self._extract_stores_from_data(
                                data, restaurant_name, location, lat, lon
                            )
                            stores.extend(extracted_stores)

                        except json.JSONDecodeError:
                            continue

            # Deduplicate by store_id
            seen = set()
            unique_stores = []
            for store in stores:
                if store.store_id not in seen:
                    seen.add(store.store_id)
                    unique_stores.append(store)

            return unique_stores[:10]  # Return top 10 results

        except Exception as e:
            logger.error(f"Error parsing search results: {e}")
            return []

    def _extract_stores_from_data(
        self,
        data,
        restaurant_name: str,
        location: str,
        lat: float,
        lon: float,
    ) -> List[UberEatsStore]:
        """
        Recursively search through nested data structure to find store information.
        """
        stores = []
        restaurant_name_lower = restaurant_name.lower()

        def search_recursive(obj, path=""):
            if isinstance(obj, dict):
                # Look for store-like objects
                if self._looks_like_store(obj, restaurant_name_lower):
                    store = self._create_store_from_object(obj, location, lat, lon)
                    if store:
                        stores.append(store)

                # Continue searching
                for key, value in obj.items():
                    search_recursive(value, f"{path}.{key}")

            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    search_recursive(item, f"{path}[{i}]")

        search_recursive(data)
        return stores

    def _looks_like_store(self, obj: dict, restaurant_name_lower: str) -> bool:
        """
        Check if a dictionary object looks like a store entry.
        """
        if not isinstance(obj, dict):
            return False

        # Look for common store fields
        has_name = any(
            key in obj for key in ["title", "name", "storeName", "store_name"]
        )
        has_uuid = any(
            key in obj for key in ["uuid", "storeUuid", "store_uuid", "slug"]
        )

        if has_name and has_uuid:
            # Check if the name matches our search
            name = str(obj.get("title") or obj.get("name") or obj.get("storeName") or obj.get("store_name") or "")
            return restaurant_name_lower in name.lower()

        return False

    def _create_store_from_object(
        self, obj: dict, location: str, lat: float, lon: float
    ) -> Optional[UberEatsStore]:
        """
        Create a UberEatsStore object from a parsed dictionary.
        """
        try:
            name = str(
                obj.get("title") or
                obj.get("name") or
                obj.get("storeName") or
                obj.get("store_name") or
                ""
            )

            store_id = str(
                obj.get("uuid") or
                obj.get("storeUuid") or
                obj.get("store_uuid") or
                obj.get("slug") or
                ""
            )

            if not name or not store_id:
                return None

            # Construct store URL
            store_url = f"{self.base_url}/store/{store_id}"
            store_url = self._encode_store_url(store_url, self._build_pl_parameter(location, lat, lon))

            # Extract address if available
            address = obj.get("location", {}).get("address") if isinstance(obj.get("location"), dict) else None

            return UberEatsStore(
                name=name,
                store_url=store_url,
                store_id=store_id,
                address=address,
                latitude=obj.get("latitude", lat)
                if isinstance(obj.get("latitude"), (int, float))
                else lat,
                longitude=obj.get("longitude", lon)
                if isinstance(obj.get("longitude"), (int, float))
                else lon,
            )

        except Exception as e:
            logger.error(f"Error creating store object: {e}")
            return None


# Singleton
ubereats_store_search = UberEatsStoreSearch()


def base64_urlsafe_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")
