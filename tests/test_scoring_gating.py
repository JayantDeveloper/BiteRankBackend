import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database import Base
from api.scraping import _persist_items


class DummyItem:
    def __init__(self, name="Mystery zzz item", price=5.0):
        self.price = price
        self.name = name
        self.category = None
        self.calories = None
        self.protein_grams = None
        self.store_external_id = "store1"
        self.price_retrieved_at = None
        self.location = None
        self.source_price_vendor = "ubereats"


@pytest.mark.asyncio
async def test_missing_calories_persisted_unranked():
    """Items with no calories and no heuristic match are saved but not ranked."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        ranked, unranked = await _persist_items(
            session,
            [DummyItem()],
            "Test R",
            "http://example.com/store",
            "00000",
            auto_rank=True,
        )
        await session.commit()

    assert ranked == []
    assert any(u["reason"] == "missing_nutrition_persisted" for u in unranked)


@pytest.mark.asyncio
async def test_heuristic_fallback_ranks_known_shapes():
    """A burger with no scraped nutrition gets heuristic calories and a score."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        ranked, unranked = await _persist_items(
            session,
            [DummyItem(name="Double Cheeseburger", price=4.0)],
            "Test R",
            "http://example.com/store",
            "00000",
            auto_rank=True,
        )
        await session.commit()

    assert len(ranked) == 1
    assert ranked[0]["value_score"] > 0
