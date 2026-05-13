"""HTTP Basic Auth + CSRF token (spec §7.2 / §7.3)."""
from __future__ import annotations

import os
import secrets
import time
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from itsdangerous import BadSignature, URLSafeTimedSerializer


_basic = HTTPBasic(realm="bluewave-worker")

# Header carrying the CSRF token on state-changing requests.
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_MAX_AGE_S = 8 * 60 * 60  # 8 h, matches spec §8 session lifetime


def _check_credentials(creds: HTTPBasicCredentials) -> str:
    expected_user = os.environ.get("WEB_USER", "")
    expected_hash = os.environ.get("WEB_PASS_HASH", "").encode("ascii")

    # constant-time username compare
    user_ok = secrets.compare_digest(creds.username, expected_user)

    try:
        pw_ok = bcrypt.checkpw(creds.password.encode("utf-8"), expected_hash)
    except (ValueError, TypeError):
        pw_ok = False

    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="bluewave-worker"'},
        )
    return creds.username


def require_basic_auth(
    creds: HTTPBasicCredentials = Depends(_basic),
) -> str:
    """FastAPI dependency. Returns the authenticated username."""
    return _check_credentials(creds)


# ---------------------------------------------------------------------------
# CSRF — token signed with itsdangerous, derived from $CONFIG_ENC_KEYS[0]
# ---------------------------------------------------------------------------


def _csrf_serializer() -> URLSafeTimedSerializer:
    """The first Fernet key doubles as the CSRF signing secret (separate
    purpose tag so the key is not literally used for two different ciphers)."""
    raw = os.environ.get("CONFIG_ENC_KEYS", "")
    first = raw.split(",", 1)[0].strip()
    if not first:
        raise RuntimeError("CONFIG_ENC_KEYS missing — required for CSRF signing")
    return URLSafeTimedSerializer(first, salt="bluewave.csrf.v1")


def issue_csrf_token() -> str:
    """Mint a fresh token tied to the current minute. Operator's session-like."""
    return _csrf_serializer().dumps({"ts": int(time.time())})


def verify_csrf_token(token: Optional[str]) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="missing CSRF token")
    try:
        _csrf_serializer().loads(token, max_age=CSRF_MAX_AGE_S)
    except BadSignature:
        raise HTTPException(status_code=403, detail="invalid CSRF token")


async def require_csrf(request: Request) -> None:
    """Dependency for state-changing endpoints.

    Accepts the token from either the ``X-CSRF-Token`` header (JSON / API
    clients) or a form field named ``csrf_token`` (browser submissions).
    """
    token = request.headers.get(CSRF_HEADER)
    if token is None and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Try to read a form value without consuming the body irrevocably.
        ct = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
            form = await request.form()
            token = form.get(CSRF_FORM_FIELD)
    verify_csrf_token(token)
