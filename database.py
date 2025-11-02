from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from config import get_settings

settings = get_settings()

# Create async engine
engine = create_async_engine(
    settings.database_url,
    echo=True,
    future=True
)

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()


async def init_db():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_optional_columns)


async def get_db():
    """Dependency for getting database sessions"""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


def _ensure_optional_columns(sync_conn):
    """Ensure newly added columns exist (SQLite lacks automatic ALTER support)."""
    try:
        result = sync_conn.execute(text("PRAGMA table_info(deals)"))
    except Exception:
        return

    existing = {row[1] for row in result.fetchall()}

    if "source_price_vendor" not in existing:
        sync_conn.execute(
            text("ALTER TABLE deals ADD COLUMN source_price_vendor VARCHAR(50)")
        )
    if "store_external_id" not in existing:
        sync_conn.execute(
            text("ALTER TABLE deals ADD COLUMN store_external_id VARCHAR(100)")
        )
    if "price_retrieved_at" not in existing:
        sync_conn.execute(
            text("ALTER TABLE deals ADD COLUMN price_retrieved_at DATETIME")
        )
    if "location" not in existing:
        sync_conn.execute(
            text("ALTER TABLE deals ADD COLUMN location VARCHAR(200)")
        )
