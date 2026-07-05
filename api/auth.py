"""JWT authentication endpoint (PyJWT, HS256)."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")
_TOKEN_TTL_HOURS = 24
_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    # main.py enforces this at server startup; the check here covers direct callers.
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET environment variable is not set")
    return secret


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_in: int


def _make_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=_TOKEN_TTL_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGORITHM)


def verify_token(token: str) -> Optional[str]:
    """Returns username if token is valid and not expired, else None."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return payload.get("sub")


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    user_ok = secrets.compare_digest(body.username.encode(), _ADMIN_USER.encode())
    pass_ok = secrets.compare_digest(body.password.encode(), _ADMIN_PASS.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = _make_token(body.username)
    return TokenResponse(token=token, expires_in=_TOKEN_TTL_HOURS * 3600)
