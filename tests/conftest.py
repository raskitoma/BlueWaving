"""Test-level fixtures + env setup.

The web layer reads four env vars at import time / route time:
``CONFIG_ENC_KEYS``, ``WEB_USER``, ``WEB_PASS_HASH``, ``WEB_ALLOW_HTTP``.
We set them here once so every test module sees a consistent environment.

Auth-aware tests use the credentials exposed as ``TEST_WEB_USER`` /
``TEST_WEB_PASS``.
"""
from __future__ import annotations

import os
import tempfile

import bcrypt
from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# Env stubs (module import time — must run before any test imports bluewave.web)
# ---------------------------------------------------------------------------

TEST_WEB_USER = "admin"
TEST_WEB_PASS = "test-password-12345"

# bcrypt with cost=4 — fast for tests; production uses cost=12.
_TEST_PASS_HASH = bcrypt.hashpw(
    TEST_WEB_PASS.encode("utf-8"), bcrypt.gensalt(rounds=4)
).decode("ascii")

# Generate a real Fernet key.
_TEST_FERNET_KEY = Fernet.generate_key().decode("ascii")

os.environ.setdefault("CONFIG_ENC_KEYS", _TEST_FERNET_KEY)
os.environ.setdefault("WEB_USER", TEST_WEB_USER)
os.environ.setdefault("WEB_PASS_HASH", _TEST_PASS_HASH)
os.environ.setdefault("WEB_ALLOW_HTTP", "1")
