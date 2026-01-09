"""
Minimal Uber Eats smoke test runner.

Run from repo root after starting the FastAPI app:
    uvicorn main:app --reload
Then:
    python backend/scripts/ubereats_smoke_test.py
"""

import asyncio
import json
from typing import Any, Dict

import httpx


PAYLOAD: Dict[str, Any] = {
    "location": "21044",
    "restaurants": ["McDonald's", "KFC"],
    "auto_rank": False,
}


async def main() -> None:
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        resp = await client.post("/api/scrape/ubereats", json=PAYLOAD, timeout=200)
        print(f"Status: {resp.status_code}")
        data = resp.json()
        print(json.dumps(data, indent=2))

        stores = data.get("stores_processed") or []
        created = data.get("created")
        updated = data.get("updated")
        print(f"Stores processed ({len(stores)}):")
        for store in stores:
            url = store.get("store_url")
            restaurant = store.get("restaurant")
            print(f"- {restaurant}: {url}")
        print(f"Created: {created}  Updated: {updated}")


if __name__ == "__main__":
    asyncio.run(main())
