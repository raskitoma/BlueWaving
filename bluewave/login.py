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
    * S1.b — wait for login form, fill, submit
    * S1.c — wait for the main menu (Reports entry) — this is our
      authentication signal: if we can see Reports, we are logged in.
      The login form's *absence* is the implicit second confirmation
      because Reports does not appear on the login page.
    * S2.a — click Reports (already located by the S1.c wait)
    * S2.b — wait for "Choose Report:" caption on the Reports page
    """
    # S1.a — transport reachability.
    try:
        safe.raw.get(url)
    except WebDriverException as e:
        raise NavFailed(f"could not reach {url}: {e}") from e

    # S1.b — login form must render and accept credentials.
    try:
        WebDriverWait(safe.raw, login_wait_s).until(
            EC.element_to_be_clickable((LOGIN_USERNAME.by, LOGIN_USERNAME.value))
        )
    except TimeoutException as e:
        raise NavFailed(
            f"login form did not render within {login_wait_s}s"
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
    # We deliberately do NOT match a "Welcome …" banner: that text drifts
    # by theme / localization. The presence of the Reports entry is both
    # an unambiguous success signal and the destination for S2.
    try:
        WebDriverWait(safe.raw, login_wait_s).until(
            EC.presence_of_element_located(
                (MAIN_MENU_REPORTS.by, MAIN_MENU_REPORTS.value)
            )
        )
    except TimeoutException as e:
        raise AuthFailed(
            "Main menu not visible after submitting the login form — "
            "credentials likely rejected, or BlueWeb returned an error page"
        ) from e

    # S2.a — Reports must be clickable (it's present per S1.c, now wait
    # for any JS-driven enabling to finish).
    try:
        WebDriverWait(safe.raw, nav_wait_s).until(
            EC.element_to_be_clickable(
                (MAIN_MENU_REPORTS.by, MAIN_MENU_REPORTS.value)
            )
        )
    except TimeoutException as e:
        raise NavFailed("Reports entry did not become clickable") from e

    try:
        safe.click(MAIN_MENU_REPORTS)
    except NoSuchElementException as e:
        raise NavFailed(f"Reports icon not found at click time: {e}") from e

    # S2.b — Reports page rendered.
    try:
        WebDriverWait(safe.raw, nav_wait_s).until(
            lambda d: REPORTS_PAGE_MARKER in (d.page_source or "")
        )
    except TimeoutException as e:
        raise NavFailed(
            f"'{REPORTS_PAGE_MARKER}' not present on Reports page within {nav_wait_s}s"
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
