"""M1 — env validation per spec §4.5 / L13 / L17.

Pass criteria covered:
- All required vars must be present and non-empty (§4.5).
- WEB_ALLOW_HTTP must be exactly "1" (L13).
- TZ must be UTC (L17).
- Default TZ (absent) is treated as UTC.
"""
from __future__ import annotations

import pytest

from bluewave.web import REQUIRED_ENV, validate_env


GOOD_ENV = {
    "CONFIG_ENC_KEYS": "stub-key",
    "WEB_USER": "admin",
    "WEB_PASS_HASH": "$2b$12$stub",
    "WEB_ALLOW_HTTP": "1",
}


def test_good_env_passes() -> None:
    assert validate_env(GOOD_ENV) == []


def test_default_tz_is_utc() -> None:
    """No TZ in env means UTC by spec convention; should pass."""
    env = dict(GOOD_ENV)  # no TZ key
    assert validate_env(env) == []


def test_explicit_tz_utc_passes() -> None:
    env = dict(GOOD_ENV, TZ="UTC")
    assert validate_env(env) == []


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_each_required_var_is_required(missing: str) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != missing}
    errors = validate_env(env)
    assert errors, f"removing {missing} should produce an error"
    assert any(missing in e for e in errors)


def test_empty_required_var_is_rejected() -> None:
    env = dict(GOOD_ENV, WEB_USER="")
    errors = validate_env(env)
    assert any("WEB_USER" in e for e in errors)


def test_web_allow_http_must_be_one() -> None:
    for bad in ("0", "true", "yes", "TRUE", " 1"):
        env = dict(GOOD_ENV, WEB_ALLOW_HTTP=bad)
        errors = validate_env(env)
        assert any("WEB_ALLOW_HTTP" in e for e in errors), bad


def test_tz_non_utc_is_rejected() -> None:
    env = dict(GOOD_ENV, TZ="America/New_York")
    errors = validate_env(env)
    assert any("TZ" in e for e in errors)


def test_all_violations_are_collected() -> None:
    """Validator returns *all* errors, not just the first — operator sees the
    full diagnosis."""
    env = {"WEB_ALLOW_HTTP": "0", "TZ": "America/Los_Angeles"}
    errors = validate_env(env)
    # All four required vars missing (one is WEB_ALLOW_HTTP, which is also
    # violating its value rule but still counts as 'set'), plus TZ + the
    # WEB_ALLOW_HTTP value.
    msgs = " | ".join(errors)
    assert "CONFIG_ENC_KEYS" in msgs
    assert "WEB_USER" in msgs
    assert "WEB_PASS_HASH" in msgs
    assert "WEB_ALLOW_HTTP" in msgs
    assert "TZ" in msgs
