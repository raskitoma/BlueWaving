"""M3 — live smoke test against a real BlueWeb instance (spec §10/M3).

This test only runs when all three env vars are set:

    BLUEWAVE_SMOKE_URL       e.g. http://blueweb.lan
    BLUEWAVE_SMOKE_USER      operator username
    BLUEWAVE_SMOKE_PASS      operator password

Without those, the test is skipped — the unit tests in
``test_m3_login.py`` cover the failure taxonomy via mocks.

Run via::

    BLUEWAVE_SMOKE_URL=http://blueweb \
    BLUEWAVE_SMOKE_USER=admin \
    BLUEWAVE_SMOKE_PASS=... \
        pytest tests/test_m3_smoke.py -v

When run, the test verifies the §10/M3 pass criteria:

- ``login_and_navigate`` reaches the Reports page within 30 s with correct creds.
- Wrong credentials raise ``AuthFailed`` within 30 s.
- An unreachable host raises ``NavFailed`` within 10 s.
"""
from __future__ import annotations

import os
import time

import pytest

from bluewave.driver import SafeDriver, build_driver
from bluewave.exceptions import AuthFailed, NavFailed
from bluewave.login import REPORTS_PAGE_MARKER, login_and_navigate


_URL = os.environ.get("BLUEWAVE_SMOKE_URL")
_USER = os.environ.get("BLUEWAVE_SMOKE_USER")
_PASS = os.environ.get("BLUEWAVE_SMOKE_PASS")

pytestmark = pytest.mark.skipif(
    not (_URL and _USER and _PASS),
    reason="BLUEWAVE_SMOKE_{URL,USER,PASS} not set — skipping live smoke",
)


# Each test gets its own driver to avoid cross-test state.


def test_smoke_happy_path() -> None:
    started = time.monotonic()
    with build_driver() as raw:
        safe = SafeDriver(raw)
        login_and_navigate(safe, _URL, _USER, _PASS)
        assert REPORTS_PAGE_MARKER in (raw.page_source or "")
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"login_and_navigate took {elapsed:.1f}s (spec: <30s)"


def test_smoke_wrong_password_is_auth_failed() -> None:
    started = time.monotonic()
    with build_driver() as raw:
        safe = SafeDriver(raw)
        with pytest.raises(AuthFailed):
            login_and_navigate(safe, _URL, _USER, "definitely-wrong-password")
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"auth-fail path took {elapsed:.1f}s (spec: <30s)"


def test_smoke_unreachable_host_is_nav_failed() -> None:
    started = time.monotonic()
    with build_driver() as raw:
        safe = SafeDriver(raw)
        with pytest.raises(NavFailed):
            login_and_navigate(
                safe,
                "http://this-host-does-not-exist.invalid",
                _USER,
                _PASS,
                login_wait_s=5,
                nav_wait_s=5,
            )
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"nav-fail path took {elapsed:.1f}s (spec: <10s)"
