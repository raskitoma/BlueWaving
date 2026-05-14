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
    # Default: the fast-path "already authenticated" check finds nothing,
    # so the login flow runs normally. Tests that want the fast-path can
    # override `raw.find_elements.return_value` AFTER this call.
    if raw is None:
        raw = MagicMock()
    raw.find_elements.return_value = []
    return SafeDriver(raw)


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
# S2.a — Reports click rejected (covers JS-fallback failure paths)
# ---------------------------------------------------------------------------


def test_reports_click_webdriver_error_raises_nav_failed() -> None:
    """If clicking the Reports button raises a non-recoverable
    WebDriverException (i.e. not the kind SafeDriver retries via JS), the
    orchestrator surfaces ``nav_failed``."""
    from bluewave.selectors import MAIN_MENU_REPORTS

    raw = MagicMock()

    def _find(by, value):
        el = MagicMock()
        if value == MAIN_MENU_REPORTS.value:
            # WebDriverException is the *parent* class — SafeDriver.click only
            # retries the two specific intercept subclasses, so this propagates.
            el.click.side_effect = WebDriverException("page broken")
        return el

    raw.find_element.side_effect = _find
    safe = SafeDriver(raw)

    with patch.object(login, "WebDriverWait") as wait_cls:
        # form OK, main-menu presence OK (S1.b + S1.c both pass).
        wait_cls.return_value.until.return_value = object()
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p")

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# S2.b — Reports page renders something but not "Choose Report:"
# ---------------------------------------------------------------------------


def test_reports_page_marker_missing_raises_nav_failed() -> None:
    """Third wait (Choose Report: marker) times out → NavFailed."""
    safe = _make_safe()

    with patch.object(login, "WebDriverWait") as wait_cls:
        # form OK, main-menu presence OK, marker timeout (3 waits total).
        wait_cls.return_value.until.side_effect = [
            object(),
            object(),
            TimeoutException("marker missing"),
        ]
        with pytest.raises(NavFailed) as excinfo:
            login.login_and_navigate(safe, "http://b/", "u", "p")

    assert excinfo.value.status == "nav_failed"


# ---------------------------------------------------------------------------
# Fast-path — existing session detected, login form skipped
# ---------------------------------------------------------------------------


def test_fast_path_skips_login_when_main_menu_already_present() -> None:
    """If `find_elements(MAIN_MENU_REPORTS)` returns a non-empty list right
    after `driver.get(url)`, the login form is never accessed."""
    raw = MagicMock()
    el = MagicMock()
    raw.find_element.return_value = el
    safe = _make_safe(raw)
    # Simulate already-authenticated state — override AFTER _make_safe.
    raw.find_elements.return_value = [MagicMock()]

    with patch.object(login, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.return_value = object()
        login.login_and_navigate(safe, "http://b/", "u", "p")

    # The username/password inputs were never typed into; only the Reports
    # button got clicked.
    assert el.send_keys.call_count == 0, \
        "should NOT have typed credentials on fast-path"

    # Only ONE WebDriverWait should have run: S2.b (Choose Report: marker).
    # S1.b and S1.c are inside _do_login_form_flow which was skipped.
    assert wait_cls.return_value.until.call_count == 1


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
