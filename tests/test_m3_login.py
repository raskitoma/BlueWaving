"""M3 — login_and_navigate status taxonomy (spec §6.4.5, §10/M3).

Verifies each failure path raises the right ``RunFailure`` subclass with
the right ``.status`` string. Uses a fully mocked driver — no Chromium
process is started. The live smoke test lives in test_m3_smoke.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)

from bluewave import login
from bluewave.driver import SafeDriver
from bluewave.exceptions import AuthFailed, NavFailed


def _make_safe(raw: MagicMock | None = None) -> SafeDriver:
    return SafeDriver(raw or MagicMock())


# ---------------------------------------------------------------------------
# S1.a — transport failure
# ---------------------------------------------------------------------------


def test_unreachable_host_raises_nav_failed() -> None:
    raw = MagicMock()
    raw.get.side_effect = WebDriverException("net::ERR_CONNECTION_REFUSED")
    safe = _make_safe(raw)

    with pytest.raises(NavFailed) as excinfo:
        login.login_and_navigate(safe, "http://unreachable", "u", "p")

    assert excinfo.value.status == "nav_failed"
    assert "unreachable" in str(excinfo.value)


# ---------------------------------------------------------------------------
# S1.b — login form did not render
# ---------------------------------------------------------------------------


def test_login_form_timeout_raises_nav_failed() -> None:
    safe = _make_safe()

    # First WebDriverWait().until() — login form wait — times out.
    with patch.object(login, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.side_effect = TimeoutException("no form")
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p", login_wait_s=1)

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# S1.c — bad creds
# ---------------------------------------------------------------------------


def test_wrong_credentials_raises_auth_failed() -> None:
    safe = _make_safe()

    with patch.object(login, "WebDriverWait") as wait_cls:
        # First wait (form render) — OK. Second wait (welcome banner) — fails.
        wait_cls.return_value.until.side_effect = [
            object(),  # S1.b — form clickable
            TimeoutException("no welcome"),  # S1.c — welcome banner missing
        ]
        with pytest.raises(AuthFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "wrong", "creds")

    assert excinfo.value.status == "auth_failed"


# ---------------------------------------------------------------------------
# S2.a — Reports icon never appears
# ---------------------------------------------------------------------------


def test_reports_icon_timeout_raises_nav_failed() -> None:
    safe = _make_safe()

    with patch.object(login, "WebDriverWait") as wait_cls:
        # form OK, welcome OK, reports icon timeout
        wait_cls.return_value.until.side_effect = [
            object(),
            object(),
            TimeoutException("no reports icon"),
        ]
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p")

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# S2.b — Reports page renders something but not "Choose Report:"
# ---------------------------------------------------------------------------


def test_reports_page_marker_missing_raises_nav_failed() -> None:
    safe = _make_safe()

    with patch.object(login, "WebDriverWait") as wait_cls:
        # form OK, welcome OK, reports icon OK, marker timeout
        wait_cls.return_value.until.side_effect = [
            object(),
            object(),
            object(),
            TimeoutException("marker missing"),
        ]
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p")

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# Happy path — all waits succeed, no exception
# ---------------------------------------------------------------------------


def test_happy_path_returns_none() -> None:
    raw = MagicMock()
    el = MagicMock()
    raw.find_element.return_value = el
    safe = _make_safe(raw)

    with patch.object(login, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.return_value = object()
        login.login_and_navigate(safe, "http://b/", "u", "p")

    # Form interactions happened.
    assert el.send_keys.call_count == 2          # user + password
    assert el.click.called                       # submit + reports icon
    # And the URL was fetched.
    raw.get.assert_called_once_with("http://b/")


# ---------------------------------------------------------------------------
# Missing form element — distinct from timeout (e.g. selector miss)
# ---------------------------------------------------------------------------


def test_missing_login_element_raises_nav_failed() -> None:
    raw = MagicMock()
    raw.find_element.side_effect = NoSuchElementException("username gone")
    safe = _make_safe(raw)

    with patch.object(login, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.return_value = object()
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p")

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# Argv handling (CLI)
# ---------------------------------------------------------------------------


def test_main_usage_error() -> None:
    rc = login.main(argv=[])
    assert rc == 2


def test_main_usage_error_two_args() -> None:
    rc = login.main(argv=["http://b/", "user"])
    assert rc == 2


# ---------------------------------------------------------------------------
# save_screenshot is best-effort
# ---------------------------------------------------------------------------


def test_save_screenshot_returns_empty_on_failure(tmp_path) -> None:
    raw = MagicMock()
    raw.save_screenshot.side_effect = WebDriverException("display gone")
    safe = SafeDriver(raw)

    out = login.save_screenshot(safe, "test", directory=str(tmp_path))

    assert out == ""


def test_save_screenshot_writes_to_directory(tmp_path) -> None:
    raw = MagicMock()
    raw.save_screenshot.return_value = True
    safe = SafeDriver(raw)

    out = login.save_screenshot(safe, "test", directory=str(tmp_path))

    assert out.startswith(str(tmp_path))
    assert out.endswith("_test.png")
    raw.save_screenshot.assert_called_once_with(out)
