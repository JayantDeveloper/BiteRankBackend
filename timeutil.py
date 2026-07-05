"""Timezone-aware time helpers.

asyncpg rejects naive datetimes for TIMESTAMP WITH TIME ZONE columns, so all
datetimes written to the DB must carry tzinfo. Use utcnow() instead of
datetime.utcnow() everywhere.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
