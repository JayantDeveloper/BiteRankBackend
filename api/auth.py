"""JWT authentication endpoint."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")
_JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_hex(32))
_TOKEN_TTL_HOURS = 24


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_in: int


def _make_token(username: str) -> str:
    """Simple signed token: base64(payload).signature — no dependency on python-jose."""
    import base64
    import hashlib
    import hmac
    import json

    expires_at = int((datetime.utcnow() + timedelta(hours=_TOKEN_TTL_HOURS)).timestamp())
    payload = json.dumps({"sub": username, "exp": expires_at}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """Returns username if token is valid and not expired, else None."""
    import base64
    import hashlib
    import hmac
    import json

    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = hmac.new(_JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(sig, expected_sig):
            return None
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        if datetime.utcnow().timestamp() > payload["exp"]:
            return None
        return payload["sub"]
    except Exception:
        return None


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    if body.username != _ADMIN_USER or body.password != _ADMIN_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = _make_token(body.username)
    return TokenResponse(token=token, expires_in=_TOKEN_TTL_HOURS * 3600)
