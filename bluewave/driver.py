"""Chromium driver factory + denylist-enforcing wrapper (spec §6.1, §6.3, §2).

Two surfaces:

- :func:`build_chrome_options` — pure builder for ``selenium`` ``Options``,
  unit-testable without launching Chromium.
- :func:`build_driver` — context manager that spins up a fresh Chromium per
  spec §6.3 and quits it in ``finally``.
- :class:`SafeDriver` — wraps a raw ``WebDriver`` and refuses to interact
  with any selector listed in :data:`bluewave.selectors.DENYLIST`.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement

from .exceptions import DenylistedSelector
from .selectors import DENYLIST, Selector


DEFAULT_DOWNLOAD_DIR = "/tmp/bluewave-dl"

# Spec §6.1 — args verbatim.
_CHROME_ARGS: tuple[str, ...] = (
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1366,900",
    "--lang=en-US",
)


def build_chrome_options(
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
    user_data_dir: str | None = None,
) -> Options:
    """Build a :class:`selenium.webdriver.chrome.options.Options` matching §6.1.

    Pure factory — no driver process is started. ``user_data_dir`` defaults
    to ``/tmp/cr-profile-{pid}`` (spec §6.1).
    """
    if user_data_dir is None:
        user_data_dir = f"/tmp/cr-profile-{os.getpid()}"

    opts = Options()
    for arg in _CHROME_ARGS:
        opts.add_argument(arg)
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
        },
    )

    return opts


@contextmanager
def build_driver(
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
) -> Iterator[webdriver.Chrome]:
    """Spin up a fresh Chromium and yield it. Quits in ``finally`` (spec §6.3).

    Also issues DevTools ``Page.setDownloadBehavior`` as a belt-and-suspenders
    fallback for older Chromium versions that ignore the prefs in headless
    mode (spec §6.1).
    """
    opts = build_chrome_options(download_dir=download_dir)
    drv = webdriver.Chrome(options=opts)
    try:
        try:
            drv.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": download_dir},
            )
        except Exception:
            # Some Chromium builds silently reject this in headless mode.
            # The pref-based config above remains in force; tolerate.
            pass
        yield drv
    finally:
        try:
            drv.quit()
        except Exception:
            # Already exited / crashed — nothing more we can do here.
            pass


class SafeDriver:
    """WebDriver wrapper that enforces the selector denylist (spec §2).

    All scrape code must use this class's methods (``find``, ``click``,
    ``type_into``) — not ``self.raw.find_element(...)`` directly. Bypassing
    ``SafeDriver`` defeats the denylist guarantee.

    The raw driver is exposed as ``.raw`` for read-only operations that the
    denylist does not need to govern: ``get(url)``, ``page_source``,
    ``save_screenshot()``, ``title``.
    """

    __slots__ = ("raw",)

    def __init__(self, raw: webdriver.Chrome) -> None:
        self.raw = raw

    def find(self, sel: Selector) -> WebElement:
        if sel in DENYLIST:
            raise DenylistedSelector(
                f"refusing to interact with denylisted selector: {sel.description}"
            )
        return self.raw.find_element(sel.by, sel.value)

    def click(self, sel: Selector) -> None:
        """Click ``sel``. If Selenium refuses (element-not-interactable or
        click-intercepted — typically because the element is in the DOM but
        ``display:none`` or covered), fall back to a JS click which fires
        the synthetic event regardless of visibility."""
        el = self.find(sel)
        try:
            el.click()
        except (ElementNotInteractableException,
                ElementClickInterceptedException):
            # Document the fallback so log readers know what happened.
            try:
                self.raw.execute_script("arguments[0].click();", el)
            except WebDriverException as js_err:
                raise WebDriverException(
                    f"standard click rejected and JS-click fallback also "
                    f"failed for {sel.description}: {js_err}"
                ) from js_err

    def type_into(self, sel: Selector, text: str) -> None:
        """Set ``text`` into ``sel``. Standard `clear()` + `send_keys()` first;
        falls back to a JS value-set with input/change events if Selenium
        rejects the element as not-interactable (display:none, hidden, etc.)."""
        el = self.find(sel)
        try:
            el.clear()
            el.send_keys(text)
        except (ElementNotInteractableException,
                ElementClickInterceptedException):
            # JS fallback — set .value and fire input + change so any
            # framework on-change handler fires too. Doesn't simulate
            # individual key events, but the page reads .value at submit.
            self.raw.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                el, text,
            )
