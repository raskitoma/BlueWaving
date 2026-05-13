"""S3–S7: choose Event report, set date range, Get Report, click CSV, await
the downloaded file (spec §2, §6.4.1).

Public surface:

* :func:`scrape_event_report` — given a logged-in :class:`SafeDriver` on the
  Reports page and a ``report_date``, drives the full flow and returns the
  downloaded ``Path``, or ``None`` if the report is empty (spec §6.4.2).
* :func:`wipe_download_dir` — preserved between modules so M8 can call it.

Selector strings live in :mod:`bluewave.selectors`. This module is the only
production caller of S3–S7 selectors (greppability test enforces).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.wait import WebDriverWait

from .driver import SafeDriver
from .exceptions import DownloadFailed, NavFailed, ReportTimeout
from .selectors import (
    CSV_EXPORT_BUTTON,
    END_DATE_INPUT,
    GET_REPORT_BUTTON,
    REPORT_NO_RESULTS_INDICATOR,
    REPORT_RESULTS_FIRST_ROW,
    REPORT_TYPE_DROPDOWN,
    START_DATE_INPUT,
)


log = logging.getLogger(__name__)


CSV_FILENAME = "BlueWeb - Reports.csv"
REPORT_WAIT_S = 60
DOWNLOAD_WAIT_S = 60
NAV_WAIT_S = 30

EVENT_REPORT_LABEL = "Event"

# Outcome of waiting after Get Report (spec §6.4.2).
_RESULT_HAS_ROWS = "has_rows"
_RESULT_NO_ROWS = "no_rows"


# ---------------------------------------------------------------------------
# S3 — select Event report
# ---------------------------------------------------------------------------


def select_event_report(safe: SafeDriver, wait_s: int = NAV_WAIT_S) -> None:
    """S3 — choose 'Event' in the report-type dropdown."""
    try:
        WebDriverWait(safe.raw, wait_s).until(
            EC.element_to_be_clickable(
                (REPORT_TYPE_DROPDOWN.by, REPORT_TYPE_DROPDOWN.value)
            )
        )
    except TimeoutException as e:
        raise NavFailed("Choose Report dropdown did not render") from e

    try:
        dropdown = safe.find(REPORT_TYPE_DROPDOWN)
        Select(dropdown).select_by_visible_text(EVENT_REPORT_LABEL)
    except (NoSuchElementException, WebDriverException) as e:
        raise NavFailed(f"could not select 'Event' report type: {e}") from e


# ---------------------------------------------------------------------------
# S4 — set Start/End date inputs
# ---------------------------------------------------------------------------


def format_date_for_blueweb(d: date) -> str:
    """``MM/DD/YYYY`` per spec §2.1."""
    return d.strftime("%m/%d/%Y")


def _set_one_date_input(
    safe: SafeDriver,
    selector,
    value: str,
) -> None:
    """Set a date input using JS injection first, fall back to send_keys.

    Spec §2.2 — JS injection wins for most JS-bound pickers because it fires
    the framework's change-event lifecycle. ``send_keys`` is the fallback
    for inputs that ignore programmatic ``value=`` assignment.
    """
    el = safe.find(selector)

    # Strategy 1: JS value injection with input/change/blur events.
    safe.raw.execute_script(
        "arguments[0].value = arguments[1];"
        "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));"
        "arguments[0].dispatchEvent(new Event('blur', {bubbles:true}));",
        el,
        value,
    )

    # Tiny settle for any framework on-change handler (spec §6.2).
    time.sleep(0.3)

    # Verify the value stuck. If not, fall back to clear+send_keys.
    current = el.get_attribute("value")
    if current != value:
        try:
            el.clear()
            el.send_keys(value)
            time.sleep(0.2)
            current = el.get_attribute("value")
        except WebDriverException:
            current = None
        if current != value:
            raise NavFailed(
                f"date input did not accept {value!r} via JS or send_keys "
                f"(observed {current!r})"
            )


def set_date_range(safe: SafeDriver, report_date: date) -> None:
    """S4 — overwrite Start Date and End Date with the same ``MM/DD/YYYY`` value."""
    value = format_date_for_blueweb(report_date)
    try:
        _set_one_date_input(safe, START_DATE_INPUT, value)
        _set_one_date_input(safe, END_DATE_INPUT, value)
    except NoSuchElementException as e:
        raise NavFailed(f"date input not found: {e}") from e


# ---------------------------------------------------------------------------
# S6 — click Get Report, wait for table or no-results
# ---------------------------------------------------------------------------


def click_get_report_and_wait(
    safe: SafeDriver, wait_s: int = REPORT_WAIT_S
) -> str:
    """Click Get Report and wait until either a result row appears or a
    'no results' indicator appears.

    Returns ``_RESULT_HAS_ROWS`` or ``_RESULT_NO_ROWS``.
    Raises :class:`ReportTimeout` if neither appears in ``wait_s``.
    """
    try:
        safe.click(GET_REPORT_BUTTON)
    except NoSuchElementException as e:
        raise NavFailed(f"Get Report button not found: {e}") from e

    def _outcome(driver) -> Optional[str]:
        # Use raw driver here — find_elements (plural) is read-only and
        # does not need the denylist gate.
        rows = driver.find_elements(
            REPORT_RESULTS_FIRST_ROW.by, REPORT_RESULTS_FIRST_ROW.value
        )
        # DataTables convention: "no data" lives as a single tr in the tbody.
        # Distinguish actual data rows by checking for the no-results text.
        if rows:
            no_rows_cells = driver.find_elements(
                REPORT_NO_RESULTS_INDICATOR.by, REPORT_NO_RESULTS_INDICATOR.value
            )
            if no_rows_cells:
                return _RESULT_NO_ROWS
            return _RESULT_HAS_ROWS
        # No tr at all yet — keep waiting.
        return None

    try:
        return WebDriverWait(safe.raw, wait_s).until(_outcome)
    except TimeoutException as e:
        raise ReportTimeout(
            f"neither result row nor no-results indicator appeared within {wait_s}s"
        ) from e


# ---------------------------------------------------------------------------
# S7 — click CSV, wait for download
# ---------------------------------------------------------------------------


def wipe_download_dir(directory: str | os.PathLike[str]) -> None:
    """Delete every regular file in ``directory``. Tolerate locked files."""
    d = Path(directory)
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        return
    for entry in d.iterdir():
        if entry.is_file() or entry.is_symlink():
            try:
                entry.unlink()
            except OSError:
                log.warning("could not unlink %s during pre-run wipe", entry)


def _download_ready(directory: Path, filename: str) -> Optional[Path]:
    """Return the file path iff it exists AND no ``.crdownload`` remains
    anywhere in the directory."""
    target = directory / filename
    if not target.exists():
        return None
    # Chromium creates `<name>.crdownload` mid-download. Wait until none exist.
    if any(directory.glob("*.crdownload")):
        return None
    return target


def click_csv_and_await_file(
    safe: SafeDriver,
    download_dir: str | os.PathLike[str],
    *,
    filename: str = CSV_FILENAME,
    timeout: int = DOWNLOAD_WAIT_S,
) -> Path:
    """S7 — click CSV; poll the download dir until the file is fully written."""
    directory = Path(download_dir)
    try:
        safe.click(CSV_EXPORT_BUTTON)
    except NoSuchElementException as e:
        raise DownloadFailed(f"CSV export button not found: {e}") from e

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = _download_ready(directory, filename)
        if path is not None:
            return path
        time.sleep(0.5)

    raise DownloadFailed(
        f"{filename!r} did not materialize in {download_dir!r} within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Whole-flow orchestration
# ---------------------------------------------------------------------------


def scrape_event_report(
    safe: SafeDriver,
    report_date: date,
    *,
    download_dir: str | os.PathLike[str],
) -> Optional[Path]:
    """Run S3–S7 in order.

    Returns:
        * ``Path`` — the downloaded CSV's local path.
        * ``None`` — the report ran successfully but had zero rows
          (spec §6.4.2 — this is a success status, not a failure).

    Raises:
        * :class:`NavFailed` — S3 / S4 layout problems
        * :class:`ReportTimeout` — S6 produced neither outcome in time
        * :class:`DownloadFailed` — S7 CSV file never materialized
    """
    wipe_download_dir(download_dir)

    select_event_report(safe)
    set_date_range(safe, report_date)
    outcome = click_get_report_and_wait(safe)

    if outcome == _RESULT_NO_ROWS:
        return None

    return click_csv_and_await_file(safe, download_dir)
