"""M3 — Chromium driver options + SafeDriver denylist enforcement."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bluewave.driver import (
    DEFAULT_DOWNLOAD_DIR,
    SafeDriver,
    build_chrome_options,
)
from bluewave.exceptions import DenylistedSelector
from bluewave.selectors import (
    DENYLIST,
    LOGIN_USERNAME,
    Selector,
)


# ---------------------------------------------------------------------------
# build_chrome_options — verify every spec §6.1 argument is present
# ---------------------------------------------------------------------------


SPEC_REQUIRED_ARGS = (
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1366,900",
    "--lang=en-US",
)


@pytest.mark.parametrize("expected_arg", SPEC_REQUIRED_ARGS)
def test_chrome_options_has_required_args(expected_arg: str) -> None:
    opts = build_chrome_options()
    assert expected_arg in opts.arguments, opts.arguments


def test_chrome_options_has_user_data_dir_with_pid() -> None:
    opts = build_chrome_options()
    matches = [a for a in opts.arguments if a.startswith("--user-data-dir=")]
    assert len(matches) == 1, matches
    # Per-pid directory keeps parallel runs isolated.
    assert "cr-profile-" in matches[0]


def test_chrome_options_user_data_dir_override() -> None:
    opts = build_chrome_options(user_data_dir="/tmp/my-profile")
    assert "--user-data-dir=/tmp/my-profile" in opts.arguments


def test_chrome_options_download_prefs() -> None:
    """Spec §6.1 download prefs verbatim."""
    opts = build_chrome_options(download_dir="/tmp/dl")
    prefs = opts.experimental_options["prefs"]
    assert prefs == {
        "download.default_directory": "/tmp/dl",
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": False,
    }


def test_chrome_options_default_download_dir() -> None:
    opts = build_chrome_options()
    prefs = opts.experimental_options["prefs"]
    assert prefs["download.default_directory"] == DEFAULT_DOWNLOAD_DIR


# ---------------------------------------------------------------------------
# SafeDriver — denylist enforcement (spec §2 hard rules / M3 pass criterion)
# ---------------------------------------------------------------------------


def _allowed_selector() -> Selector:
    """Any non-denylisted selector. LOGIN_USERNAME is convenient."""
    return LOGIN_USERNAME


def test_safe_driver_find_passes_through_for_allowed_selector() -> None:
    raw = MagicMock()
    raw.find_element.return_value = "ELEMENT"
    safe = SafeDriver(raw)
    sel = _allowed_selector()

    result = safe.find(sel)

    assert result == "ELEMENT"
    raw.find_element.assert_called_once_with(sel.by, sel.value)


@pytest.mark.parametrize("denied_sel", list(DENYLIST))
def test_safe_driver_find_blocks_each_denylist_entry(denied_sel: Selector) -> None:
    raw = MagicMock()
    safe = SafeDriver(raw)

    with pytest.raises(DenylistedSelector):
        safe.find(denied_sel)

    # Critical: the raw driver MUST NOT see the denylisted call.
    raw.find_element.assert_not_called()


@pytest.mark.parametrize("denied_sel", list(DENYLIST))
def test_safe_driver_click_blocks_each_denylist_entry(denied_sel: Selector) -> None:
    raw = MagicMock()
    safe = SafeDriver(raw)

    with pytest.raises(DenylistedSelector):
        safe.click(denied_sel)

    raw.find_element.assert_not_called()


@pytest.mark.parametrize("denied_sel", list(DENYLIST))
def test_safe_driver_type_blocks_each_denylist_entry(denied_sel: Selector) -> None:
    raw = MagicMock()
    safe = SafeDriver(raw)

    with pytest.raises(DenylistedSelector):
        safe.type_into(denied_sel, "anything")

    raw.find_element.assert_not_called()


def test_safe_driver_type_into_clears_before_send_keys() -> None:
    el = MagicMock()
    raw = MagicMock()
    raw.find_element.return_value = el
    safe = SafeDriver(raw)

    safe.type_into(_allowed_selector(), "hello")

    # order matters: clear() before send_keys() so the field is replaced,
    # not appended-to (matters for date inputs in M4).
    call_order = [c[0] for c in el.method_calls]
    assert call_order == ["clear", "send_keys"]
    el.send_keys.assert_called_once_with("hello")
