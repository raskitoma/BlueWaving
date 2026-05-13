"""M8 — scheduler tests (spec §10/M8).

Covers:

- Daily job is registered with the correct cron expression in the operator TZ.
- ``reconfigure()`` re-registers when schedule / TZ change.
- Boot-time catch-up enqueues missing dates (mocked orchestrator).
- ``next_run_at_utc()`` returns a sensible UTC datetime.
- Firing the job invokes ``orchestrator.try_enqueue`` with yesterday-local + manual=False.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from bluewave.config import Config, ConfigStore, build_multifernet, parse_keys_env
from bluewave.scheduler import JOB_ID_DAILY, Scheduler


def _store(tmp_path):
    import os
    mf = build_multifernet(parse_keys_env(os.environ["CONFIG_ENC_KEYS"]))
    return ConfigStore(tmp_path / "config.sqlite", mf)


def _cfg(**overrides) -> Config:
    base = dict(
        site_label="Easy Foods Inc.",
        blueweb_url="http://blueweb.lan",
        blueweb_user="admin",
        blueweb_password="pw",
        operator_timezone="America/New_York",
        mysql_host="mysql.lan",
        mysql_port=3306,
        mysql_database="audit",
        mysql_user="u",
        mysql_password="p",
        schedule_local="03:00",
        catch_up_cap_days=14,
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path)


@pytest.fixture
def orch():
    o = MagicMock()
    o.catchup_missing.return_value = ([], [])
    return o


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_no_job_registered_when_unconfigured(store, orch) -> None:
    sch = Scheduler(store, orch)
    sch.start()
    try:
        assert sch.next_run_at_utc() is None
    finally:
        sch.shutdown()


def test_daily_job_registered_when_configured(store, orch) -> None:
    store.save(_cfg(schedule_local="03:00"))
    sch = Scheduler(store, orch)
    sch.start()
    try:
        next_run = sch.next_run_at_utc()
        assert next_run is not None

        ny = ZoneInfo("America/New_York")
        local = next_run.astimezone(ny)
        assert local.hour == 3 and local.minute == 0
        # The next fire is in the future.
        assert next_run > datetime.now(timezone.utc)
    finally:
        sch.shutdown()


def test_reconfigure_re_registers_with_new_schedule(store, orch) -> None:
    store.save(_cfg(schedule_local="03:00"))
    sch = Scheduler(store, orch)
    sch.start()
    try:
        first = sch.next_run_at_utc()

        # Save a new schedule and reconfigure.
        store.save(_cfg(schedule_local="07:30"))
        sch.reconfigure()

        second = sch.next_run_at_utc()
        assert second is not None
        ny = ZoneInfo("America/New_York")
        assert second.astimezone(ny).hour == 7
        assert second.astimezone(ny).minute == 30
        # And the fire-time genuinely changed.
        assert first != second
    finally:
        sch.shutdown()


def test_reconfigure_removes_job_when_config_deleted(store, orch) -> None:
    store.save(_cfg())
    sch = Scheduler(store, orch)
    sch.start()
    try:
        assert sch.next_run_at_utc() is not None

        # Simulate config removal.
        import sqlite3
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM config")
            conn.commit()

        sch.reconfigure()
        assert sch.next_run_at_utc() is None
    finally:
        sch.shutdown()


# ---------------------------------------------------------------------------
# Boot-time catch-up
# ---------------------------------------------------------------------------


def test_boot_catchup_invokes_orchestrator(store, orch) -> None:
    store.save(_cfg(catch_up_cap_days=7))
    orch.catchup_missing.return_value = (
        [date(2026, 5, 8), date(2026, 5, 9)],
        [],
    )
    sch = Scheduler(store, orch)
    sch.start()
    try:
        orch.catchup_missing.assert_called_once()
        _, kwargs = orch.catchup_missing.call_args
        assert kwargs["cap_days"] == 7
        # today_local is in the operator's TZ
        assert isinstance(kwargs["today_local"], date)
    finally:
        sch.shutdown()


def test_boot_catchup_tolerates_failure(store, orch) -> None:
    """If catchup raises, scheduler.start() must still succeed (best-effort)."""
    store.save(_cfg())
    orch.catchup_missing.side_effect = RuntimeError("MySQL unreachable")
    sch = Scheduler(store, orch)
    sch.start()  # should not raise
    try:
        # Daily job still registered.
        assert sch.next_run_at_utc() is not None
    finally:
        sch.shutdown()


# ---------------------------------------------------------------------------
# Daily-fire enqueues yesterday with manual=False
# ---------------------------------------------------------------------------


def test_daily_fire_enqueues_yesterday(store, orch) -> None:
    """Patch the scheduler's daily-fire trigger to fire immediately, then
    verify what was enqueued."""
    store.save(_cfg(schedule_local="03:00", operator_timezone="America/New_York"))
    sch = Scheduler(store, orch)
    sch.start()
    try:
        # Pull the registered callable and invoke it directly.
        job = sch._sched.get_job(JOB_ID_DAILY)
        assert job is not None
        job.func()

        orch.try_enqueue.assert_called_once()
        args, kwargs = orch.try_enqueue.call_args
        # try_enqueue(date, manual=False)
        report_date = args[0] if args else kwargs.get("report_date")
        assert isinstance(report_date, date)
        # yesterday in NY tz
        ny_today = datetime.now(ZoneInfo("America/New_York")).date()
        assert report_date == ny_today - timedelta(days=1)
        assert kwargs.get("manual") is False
    finally:
        sch.shutdown()
