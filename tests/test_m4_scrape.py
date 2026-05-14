"""M4 — scrape flow unit tests (S3–S7) with a mocked driver."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from bluewave import scrape
from bluewave.driver import SafeDriver
from bluewave.exceptions import DownloadFailed, NavFailed, ReportTimeout


def _safe() -> SafeDriver:
    raw = MagicMock()
    raw.find_elements.return_value = []
    return SafeDriver(raw)


# ---------------------------------------------------------------------------
# format_date_for_blueweb
# ---------------------------------------------------------------------------


def test_format_date_uses_mm_dd_yyyy() -> None:
    assert scrape.format_date_for_blueweb(date(2026, 5, 12)) == "05/12/2026"
    assert scrape.format_date_for_blueweb(date(2026, 1, 9)) == "01/09/2026"


# ---------------------------------------------------------------------------
# select_event_report
# ---------------------------------------------------------------------------


def test_select_event_report_uses_select_helper() -> None:
    safe = _safe()
    dropdown = MagicMock()
    dropdown.tag_name = "select"
    safe.raw.find_element.return_value = dropdown

    with patch.object(scrape, "WebDriverWait") as wait_cls, \
         patch.object(scrape, "Select") as select_cls:
        wait_cls.return_value.until.return_value = object()
        scrape.select_event_report(safe)

        select_cls.assert_called_once_with(dropdown)
        select_cls.return_value.select_by_visible_text.assert_called_once_with(
            scrape.EVENT_REPORT_LABEL
        )


def test_select_event_report_dropdown_timeout_raises_nav_failed() -> None:
    safe = _safe()
    with patch.object(scrape, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.side_effect = TimeoutException("no dropdown")
        with pytest.raises(NavFailed):
            scrape.select_event_report(safe)


# ---------------------------------------------------------------------------
# set_date_range — JS injection success + fallback path
# ---------------------------------------------------------------------------


def test_set_date_range_uses_js_injection_when_value_sticks() -> None:
    safe = _safe()
    el = MagicMock()
    el.get_attribute.return_value = "05/12/2026"  # value stuck after JS
    safe.raw.find_element.return_value = el

    scrape.set_date_range(safe, date(2026, 5, 12))

    # JS injection was attempted (execute_script called).
    assert safe.raw.execute_script.call_count == 2  # start + end inputs
    # Send-keys fallback was NOT used because JS worked.
    el.clear.assert_not_called()
    el.send_keys.assert_not_called()


def test_set_date_range_falls_back_to_send_keys_when_js_doesnt_stick() -> None:
    safe = _safe()
    el = MagicMock()
    # JS injection returns wrong value the first time, right value after send_keys.
    el.get_attribute.side_effect = [
        "",            # after JS for start date — didn't stick
        "05/12/2026",  # after send_keys for start date — stuck
        "",            # after JS for end date — didn't stick
        "05/12/2026",  # after send_keys for end date — stuck
    ]
    safe.raw.find_element.return_value = el

    scrape.set_date_range(safe, date(2026, 5, 12))

    # Send-keys fallback was used.
    assert el.clear.call_count == 2
    assert el.send_keys.call_count == 2
    el.send_keys.assert_called_with("05/12/2026")


def test_set_date_range_raises_when_neither_strategy_sticks() -> None:
    safe = _safe()
    el = MagicMock()
    el.get_attribute.return_value = ""  # never accepts
    safe.raw.find_element.return_value = el

    with pytest.raises(NavFailed):
        scrape.set_date_range(safe, date(2026, 5, 12))


# ---------------------------------------------------------------------------
# click_get_report_and_wait — has_rows / no_rows / timeout
# ---------------------------------------------------------------------------


def test_get_report_has_rows_outcome() -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()  # for SafeDriver.click

    has_rows_row = MagicMock()
    no_rows_cells: list = []

    def _find_elements(by, value):
        if value == scrape.REPORT_RESULTS_FIRST_ROW.value:
            return [has_rows_row]
        if value == scrape.REPORT_NO_RESULTS_INDICATOR.value:
            return no_rows_cells
        return []

    safe.raw.find_elements.side_effect = _find_elements

    # Skip the actual WebDriverWait — invoke our predicate directly.
    with patch.object(scrape, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.side_effect = lambda predicate: predicate(safe.raw)
        outcome = scrape.click_get_report_and_wait(safe)
    assert outcome == "has_rows"


def test_get_report_no_rows_outcome() -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()

    no_rows_marker = MagicMock()
    fake_tr = MagicMock()  # the "no data available" row IS a tr

    def _find_elements(by, value):
        if value == scrape.REPORT_RESULTS_FIRST_ROW.value:
            return [fake_tr]
        if value == scrape.REPORT_NO_RESULTS_INDICATOR.value:
            return [no_rows_marker]
        return []

    safe.raw.find_elements.side_effect = _find_elements

    with patch.object(scrape, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.side_effect = lambda predicate: predicate(safe.raw)
        outcome = scrape.click_get_report_and_wait(safe)
    assert outcome == "no_rows"


def test_get_report_timeout_raises() -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()
    with patch.object(scrape, "WebDriverWait") as wait_cls:
        wait_cls.return_value.until.side_effect = TimeoutException("nothing rendered")
        with pytest.raises(ReportTimeout):
            scrape.click_get_report_and_wait(safe)


def test_get_report_missing_button_raises_nav_failed() -> None:
    safe = _safe()
    safe.raw.find_element.side_effect = NoSuchElementException("button missing")
    with pytest.raises(NavFailed):
        scrape.click_get_report_and_wait(safe)


# ---------------------------------------------------------------------------
# wipe_download_dir
# ---------------------------------------------------------------------------


def test_wipe_download_dir_creates_missing(tmp_path: Path) -> None:
    sub = tmp_path / "new"
    scrape.wipe_download_dir(sub)
    assert sub.exists() and sub.is_dir()


def test_wipe_download_dir_removes_files(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("a")
    (tmp_path / "b.crdownload").write_text("b")
    scrape.wipe_download_dir(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_wipe_download_dir_raises_download_failed_on_unwritable(
    tmp_path: Path, monkeypatch
) -> None:
    """If the download dir exists but the worker can't write a probe file,
    raise DownloadFailed immediately — don't wait 60 s for Chromium to time
    out trying to land the CSV. This is the tmpfs-permission scenario."""
    from bluewave.exceptions import DownloadFailed

    # Make Path.write_text raise PermissionError to simulate an unwritable
    # mount. We patch the bound method on the probe path.
    real_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name == ".bluewave-write-probe":
            raise PermissionError("Permission denied")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)
    with pytest.raises(DownloadFailed, match="not writable"):
        scrape.wipe_download_dir(tmp_path)


# ---------------------------------------------------------------------------
# click_csv_and_await_file
# ---------------------------------------------------------------------------


def test_csv_download_waits_for_file_appearance(tmp_path: Path) -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()

    target = tmp_path / scrape.CSV_FILENAME

    # Simulate the file appearing on the 3rd poll.
    poll_count = {"n": 0}
    real_sleep = scrape.time.sleep

    def fake_sleep(_):
        poll_count["n"] += 1
        if poll_count["n"] >= 3:
            target.write_text("Date/Time,...\n")

    with patch.object(scrape.time, "sleep", fake_sleep):
        path = scrape.click_csv_and_await_file(safe, tmp_path, timeout=10)

    assert path == target
    assert path.exists()


def test_csv_download_treats_crdownload_as_incomplete(tmp_path: Path) -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()

    target = tmp_path / scrape.CSV_FILENAME
    target.write_text("partial")
    crdl = tmp_path / "BlueWeb - Reports.csv.crdownload"
    crdl.write_text("partial")  # in-progress

    sleeps = {"n": 0}

    def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            crdl.unlink()

    with patch.object(scrape.time, "sleep", fake_sleep):
        path = scrape.click_csv_and_await_file(safe, tmp_path, timeout=10)

    assert path == target


def test_csv_download_times_out_raises(tmp_path: Path) -> None:
    safe = _safe()
    safe.raw.find_element.return_value = MagicMock()
    with patch.object(scrape.time, "sleep", lambda _: None):
        with patch.object(scrape.time, "monotonic", side_effect=[0, 0, 100]):
            with pytest.raises(DownloadFailed):
                scrape.click_csv_and_await_file(safe, tmp_path, timeout=1)


def test_csv_button_missing_raises_download_failed() -> None:
    safe = _safe()
    safe.raw.find_element.side_effect = NoSuchElementException("no csv button")
    with pytest.raises(DownloadFailed):
        scrape.click_csv_and_await_file(safe, "/tmp/anywhere")


# ---------------------------------------------------------------------------
# scrape_event_report — empty-report (None) and happy paths
# ---------------------------------------------------------------------------


def test_scrape_event_report_returns_none_on_empty(tmp_path: Path) -> None:
    safe = _safe()
    with patch.object(scrape, "select_event_report") as sel, \
         patch.object(scrape, "set_date_range") as setd, \
         patch.object(scrape, "click_get_report_and_wait", return_value="no_rows"), \
         patch.object(scrape, "click_csv_and_await_file") as csv_click:

        out = scrape.scrape_event_report(safe, date(2026, 5, 12), download_dir=tmp_path)

    assert out is None
    csv_click.assert_not_called()
    sel.assert_called_once()
    setd.assert_called_once_with(safe, date(2026, 5, 12))


def test_scrape_event_report_returns_path_on_has_rows(tmp_path: Path) -> None:
    safe = _safe()
    fake_path = tmp_path / scrape.CSV_FILENAME
    with patch.object(scrape, "select_event_report"), \
         patch.object(scrape, "set_date_range"), \
         patch.object(scrape, "click_get_report_and_wait", return_value="has_rows"), \
         patch.object(scrape, "click_csv_and_await_file", return_value=fake_path):
        out = scrape.scrape_event_report(safe, date(2026, 5, 12), download_dir=tmp_path)
    assert out == fake_path


def test_scrape_event_report_wipes_dir_before_run(tmp_path: Path) -> None:
    (tmp_path / "leftover.csv").write_text("from previous run")
    safe = _safe()
    with patch.object(scrape, "select_event_report"), \
         patch.object(scrape, "set_date_range"), \
         patch.object(scrape, "click_get_report_and_wait", return_value="no_rows"):
        scrape.scrape_event_report(safe, date(2026, 5, 12), download_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
