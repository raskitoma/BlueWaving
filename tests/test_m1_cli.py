"""M1 — CLI helpers (keygen + hashpw) per spec pass criteria.

Pass criteria covered:
- keygen prints a 44-char base64 string; two invocations produce distinct strings.
- hashpw produces a bcrypt hash that verifies the original password.
- hashpw rejects passwords below MIN_PASSWORD_LENGTH.
"""
from __future__ import annotations

import re
import subprocess
import sys

import bcrypt
import pytest

from bluewave.hashpw import MIN_PASSWORD_LENGTH, hash_password


FERNET_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{43}=$")


def _run_keygen() -> str:
    out = subprocess.check_output(
        [sys.executable, "-m", "bluewave.keygen"],
        text=True,
    )
    return out.strip()


def test_keygen_prints_fernet_shaped_key() -> None:
    key = _run_keygen()
    assert FERNET_KEY_RE.match(key), f"unexpected shape: {key!r}"
    assert len(key) == 44


def test_keygen_keys_are_distinct() -> None:
    assert _run_keygen() != _run_keygen()


def test_hash_password_roundtrips() -> None:
    plain = "correct-horse-battery"
    h = hash_password(plain).encode("ascii")
    assert bcrypt.checkpw(plain.encode("utf-8"), h)
    assert not bcrypt.checkpw(b"wrong", h)


def test_hash_password_rejects_short() -> None:
    with pytest.raises(ValueError, match="at least"):
        hash_password("a" * (MIN_PASSWORD_LENGTH - 1))


def test_hash_password_accepts_min_length() -> None:
    # Should not raise at exactly the minimum.
    _ = hash_password("a" * MIN_PASSWORD_LENGTH)


def test_hashpw_stdin_mode() -> None:
    """Scriptable form: `echo password | python -m bluewave.hashpw --stdin`."""
    out = subprocess.check_output(
        [sys.executable, "-m", "bluewave.hashpw", "--stdin"],
        input="correct-horse-battery\n",
        text=True,
    )
    h = out.strip().encode("ascii")
    assert bcrypt.checkpw(b"correct-horse-battery", h)


def test_hashpw_stdin_rejects_short() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "bluewave.hashpw", "--stdin"],
        input="short\n",
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 1
    assert "at least" in proc.stderr
