"""Database setup for BiteRank."""

import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from config import get_settings

settings = get_settings()


def _normalize_database_url(url: str) -> tuple[str, dict]:
    """Render/Heroku hand out postgres:// or postgresql:// URLs. SQLAlchemy async
    needs the asyncpg driver, and asyncpg takes ssl via connect_args rather than
    a ?sslmode= query param (which it rejects)."""
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://"):
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query))
        sslmode = query.pop("sslmode", None)
        if sslmode == "require":
            # libpq's "require" means encrypt but don't verify the cert —
            # needed for poolers with provider-CA certs (e.g. Supabase).
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ctx
        elif sslmode in ("verify-ca", "verify-full"):
            connect_args["ssl"] = True
        url = urlunsplit(parsed._replace(query=urlencode(query)))
    return url, connect_args


_db_url, _connect_args = _normalize_database_url(settings.database_url)

engine = create_async_engine(
    _db_url,
    echo=settings.database_echo,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

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
    dialect = sync_conn.dialect.name
    if dialect != "sqlite":
        return
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
    if "last_ranked_at" not in existing:
        sync_conn.execute(
            text("ALTER TABLE deals ADD COLUMN last_ranked_at DATETIME")
        )
