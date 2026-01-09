import asyncio
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from api.routes import _persist_and_rank_items


class DummyItem:
    def __init__(self):
        self.price = 5.0
        self.name = "Mystery item"
        self.category = None
        self.calories = None
        self.protein_grams = None
        self.store_external_id = "store1"
        self.price_retrieved_at = None
        self.location = None
        self.source_price_vendor = "ubereats"


@pytest.mark.asyncio
async def test_missing_calories_unranked(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        ranked, unranked = await _persist_and_rank_items(
            session,
            [DummyItem()],
            "Test R",
            "http://example.com/store",
            "00000",
            auto_rank=True,
        )
        await session.commit()

    assert ranked == []
    assert any(u["reason"] == "missing_nutrition" for u in unranked)
