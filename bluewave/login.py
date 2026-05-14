"""S1 (login) + S2 (navigate to Reports) per spec §2.

Used both as an importable function (``login_and_navigate``) by future
milestones, and as a CLI smoke test:

    python -m bluewave.login <url> <user> <password>

Exit codes match the spec §10/M3 pass criteria:

* ``0`` — login succeeded, on the Reports page
* ``1`` — a recognized failure (``status`` printed to stderr)
* ``2`` — argv misuse
"""
from __future__ import annotations

import logging
import os
import sys
import time

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from .driver import SafeDriver, build_driver
from .exceptions import AuthFailed, NavFailed, RunFailure
from .selectors import (
    LOGIN_PASSWORD,
    LOGIN_SUBMIT,
    LOGIN_USERNAME,
    MAIN_MENU_REPORTS,
)


# Spec §6.2 default. Per-step waits inherit unless overridden.
DEFAULT_WAIT_S = 30
NAV_WAIT_S = 15  # tighter for nav transitions that should be near-instant

REPORTS_PAGE_MARKER = "Choose Report:"

DEFAULT_SCREENSHOT_DIR = "/var/lib/bluewave-worker/screenshots"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostics — capture page state when login fails so the operator can
# tell apart bad creds, selector drift, and unexpected redirects.
# ---------------------------------------------------------------------------


def _short(s: object, limit: int = 250) -> str:
    """Squeeze a string into a single-line excerpt."""
    text = " ".join(str(s).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def capture_page_state(
    driver,
    *,
    save_html_to: str | None = None,
) -> dict:
    """Return a dict describing the current page for diagnostic purposes.

    Best-effort — every accessor is wrapped because the driver may be in a
    half-dead state by the time we get here.
    """
    state: dict[str, object] = {}
    for key, fn in (
        ("url",   lambda: driver.current_url),
        ("title", lambda: driver.title),
    ):
        try:
            state[key] = fn()
        except Exception:
            state[key] = None

    # Body excerpt — 250 chars of visible text, helps spot "Invalid login"
    # or distinguish login page from dashboard.
    try:
        body = driver.find_element("tag name", "body").text or ""
        state["body_excerpt"] = _short(body)
    except Exception:
        state["body_excerpt"] = None

    # Did the login form's username input survive? If yes → we're probably
    # still on the login page (failed submit OR rejected creds).
    try:
        from .selectors import LOGIN_USERNAME

        els = driver.find_elements(LOGIN_USERNAME.by, LOGIN_USERNAME.value)
        state["login_form_still_visible"] = bool(els)
    except Exception:
        state["login_form_still_visible"] = None

    # Drop the raw page source to disk for offline inspection.
    if save_html_to:
        try:
            with open(save_html_to, "w", encoding="utf-8") as f:
                f.write(driver.page_source or "")
            state["page_source_path"] = save_html_to
        except Exception:
            state["page_source_path"] = None

    return state


def _state_to_message(state: dict) -> str:
    """Render the dict from capture_page_state into a one-line summary."""
    parts: list[str] = []
    if state.get("login_form_still_visible") is True:
        parts.append("login form still visible (creds rejected or submit failed)")
    elif state.get("login_form_still_visible") is False:
        parts.append("login form is gone — landed on an unexpected page")
    if state.get("url"):
        parts.append(f"url={state['url']!r}")
    if state.get("title"):
        parts.append(f"title={state['title']!r}")
    if state.get("body_excerpt"):
        parts.append(f"body={state['body_excerpt']!r}")
    if state.get("page_source_path"):
        parts.append(f"html dumped to {state['page_source_path']}")
    return "; ".join(parts) if parts else "no page state captured"


def login_and_navigate(
    safe: SafeDriver,
    url: str,
    user: str,
    password: str,
    *,
    login_wait_s: int = DEFAULT_WAIT_S,
    nav_wait_s: int = NAV_WAIT_S,
) -> None:
    """Run S1 then S2. Raises a :class:`RunFailure` subclass on failure.

    Step boundaries:

    * S1.a — GET ``url`` (transport reachability)
    * S1.fast — if the main menu is *already* visible (existing session,
      SSO, network auth, cached creds, anything), skip the login form
      entirely and jump straight to S2. Robust against scenarios where
      something else already authenticated this Chromium.
    * S1.b — (only if S1.fast did not trigger) wait for login form, fill, submit
    * S1.c — (only if S1.fast did not trigger) wait for the main menu —
      this is our authentication signal: if we can see Reports, we are
      logged in.
    * S2.a — click Reports
    * S2.b — wait for "Choose Report:" caption on the Reports page
    """
    # S1.a — transport reachability.
    try:
        safe.raw.get(url)
    except WebDriverException as e:
        raise NavFailed(f"could not reach {url}: {e}") from e

    # S1.fast — fast-path: if the page we landed on ALREADY contains the
    # Reports menu element, we're authenticated (existing session / SSO /
    # whatever) and there's nothing to submit. Skip directly to S2.
    # find_elements (plural) returns [] when nothing matches — never raises.
    try:
        already_authed = bool(
            safe.raw.find_elements(MAIN_MENU_REPORTS.by, MAIN_MENU_REPORTS.value)
        )
    except WebDriverException:
        already_authed = False

    if already_authed:
        log.info("login_and_navigate: existing session detected, skipping login form")
    else:
        _do_login_form_flow(safe, user, password, login_wait_s=login_wait_s)

    # S2.a — Click Reports. We deliberately do NOT gate on Selenium's
    # `element_to_be_clickable` because BlueWeb's Quick Links bar is
    # `display:none` on the main-menu page (the big icon row handles
    # navigation there). Selenium would never see the element as
    # "clickable", but SafeDriver.click falls back to a JS click which
    # fires the synthetic event regardless of visibility.
    try:
        safe.click(MAIN_MENU_REPORTS)
    except NoSuchElementException as e:
        raise NavFailed(f"Reports button not found at click time: {e}") from e
    except WebDriverException as e:
        raise NavFailed(f"could not click Reports button: {e}") from e

    # S2.b — Reports page rendered.
    try:
        WebDriverWait(safe.raw, nav_wait_s).until(
            lambda d: REPORTS_PAGE_MARKER in (d.page_source or "")
        )
    except TimeoutException as e:
        raise NavFailed(
            f"'{REPORTS_PAGE_MARKER}' not present on Reports page within {nav_wait_s}s"
        ) from e


def _do_login_form_flow(
    safe: SafeDriver,
    user: str,
    password: str,
    *,
    login_wait_s: int,
) -> None:
    """S1.b + S1.c — fill the login form and verify the main menu appears.

    Skipped entirely when :func:`login_and_navigate` detects an existing
    session (fast-path).
    """
    # S1.b — login form must be in the DOM. We use `presence_of_element_located`
    # rather than `element_to_be_clickable` because some BlueWeb skins wrap the
    # form in a panel whose CSS fails Selenium's visibility heuristics; the
    # input is still typeable. SafeDriver.type_into has a JS fallback if
    # send_keys is rejected.
    try:
        WebDriverWait(safe.raw, login_wait_s).until(
            EC.presence_of_element_located(
                (LOGIN_USERNAME.by, LOGIN_USERNAME.value)
            )
        )
    except TimeoutException as e:
        html_dump: str | None = None
        try:
            os.makedirs(DEFAULT_SCREENSHOT_DIR, exist_ok=True)
            html_dump = os.path.join(
                DEFAULT_SCREENSHOT_DIR,
                f"{int(time.time())}_login_form_missing.html",
            )
        except OSError:
            html_dump = None
        try:
            state = capture_page_state(safe.raw, save_html_to=html_dump)
        except Exception:
            state = {}
        raise NavFailed(
            f"login form (id={LOGIN_USERNAME.value!r}) did not appear in the "
            f"DOM within {login_wait_s}s. " + _state_to_message(state)
        ) from e

    try:
        safe.type_into(LOGIN_USERNAME, user)
        safe.type_into(LOGIN_PASSWORD, password)
    except NoSuchElementException as e:
        raise NavFailed(f"login form input not found: {e}") from e

    # Try clicking the explicit submit control. If we can't locate it,
    # fall back to pressing Enter in the password field — most forms (and
    # certainly classic ASP.NET ones) submit on Enter regardless of the
    # button's HTML shape.
    try:
        safe.click(LOGIN_SUBMIT)
    except NoSuchElementException:
        try:
            pw_el = safe.find(LOGIN_PASSWORD)
            pw_el.send_keys(Keys.ENTER)
        except (NoSuchElementException, WebDriverException) as e:
            raise NavFailed(
                f"could not submit login form (no submit control, no Enter): {e}"
            ) from e

    # S1.c — main menu visible (Reports entry) means we are authenticated.
    try:
        WebDriverWait(safe.raw, login_wait_s).until(
            EC.presence_of_element_located(
                (MAIN_MENU_REPORTS.by, MAIN_MENU_REPORTS.value)
            )
        )
    except TimeoutException as e:
        html_dump = None
        try:
            os.makedirs(DEFAULT_SCREENSHOT_DIR, exist_ok=True)
            html_dump = os.path.join(
                DEFAULT_SCREENSHOT_DIR, f"{int(time.time())}_login_fail.html"
            )
        except OSError:
            html_dump = None
        try:
            state = capture_page_state(safe.raw, save_html_to=html_dump)
        except Exception:
            state = {}
        raise AuthFailed(
            "Main menu not visible after submit. " + _state_to_message(state)
        ) from e


def save_screenshot(
    safe: SafeDriver,
    label: str,
    directory: str = DEFAULT_SCREENSHOT_DIR,
) -> str:
    """Save a PNG screenshot. Returns the path written, or empty string on
    failure. Never raises — screenshot capture is best-effort observability,
    not a gate."""
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        # Volume not mounted (e.g. local dev). Fall back to CWD.
        directory = os.getcwd()
    path = os.path.join(directory, f"{int(time.time())}_{label}.png")
    try:
        safe.raw.save_screenshot(path)
        return path
    except WebDriverException:
        return ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        sys.stderr.write(
            "usage: python -m bluewave.login <url> <user> <password>\n"
        )
        return 2

    url, user, password = args

    with build_driver() as raw:
        safe = SafeDriver(raw)
        try:
            login_and_navigate(safe, url, user, password)
        except RunFailure as e:
            path = save_screenshot(safe, f"login_fail_{e.status}")
            sys.stderr.write(f"FAIL status={e.status}: {e}\n")
            if path:
                sys.stderr.write(f"screenshot: {path}\n")
            return 1
        # Optional success screenshot for forensic continuity.
        path = save_screenshot(safe, "login_ok")
        print(f"ok status=login_ok screenshot={path or '(none)'}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
