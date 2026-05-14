"""APScheduler wrapper: daily fire + boot-time catch-up (spec §6.4 / §10/M8).

Three concerns:

1. Register a single cron job that fires at ``cfg.schedule_local`` in
   ``cfg.operator_timezone``, enqueueing ``today_local - 1`` as a non-manual run.
2. Re-register the job whenever the operator changes the schedule or timezone
   (called from ``POST /config`` after a successful save).
3. Boot-time catch-up: at startup, query the runs table for ``ok`` dates and
   enqueue every missing day within the cap (spec §6.4.3).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


log = logging.getLogger(__name__)


JOB_ID_DAILY = "bluewave.daily"


class Scheduler:
    """Owns the APScheduler instance and the daily-fire registration."""

    def __init__(self, store, orchestrator) -> None:
        self.store = store
        self.orchestrator = orchestrator
        self._sched: Optional[BackgroundScheduler] = None

    # ----- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Start APScheduler in the background. Registers the daily job if
        a config is present, runs the boot-time catch-up, and bootstraps
        the audit table (idempotent — `CREATE TABLE IF NOT EXISTS`)."""
        self._sched = BackgroundScheduler(timezone="UTC")
        self._sched.start()

        cfg = self.store.load()
        if cfg is None:
            log.info("scheduler.start unconfigured — no daily job registered")
            return

        self._bootstrap_audit_table(cfg)
        self._register_daily(cfg)
        self._boot_catchup(cfg)

    def shutdown(self, *, wait: bool = False) -> None:
        if self._sched is not None:
            self._sched.shutdown(wait=wait)
            self._sched = None

    # ----- public API used by /config save --------------------------------

    def reconfigure(self) -> None:
        """Re-register the daily job from the latest config. Idempotent."""
        if self._sched is None:
            return
        try:
            self._sched.remove_job(JOB_ID_DAILY)
        except Exception:
            pass

        cfg = self.store.load()
        if cfg is None:
            log.info("scheduler.reconfigure config removed; no job re-registered")
            return
        self._register_daily(cfg)

    # ----- introspection (for tests / /healthz wiring) --------------------

    def next_run_at_utc(self) -> Optional[datetime]:
        if self._sched is None:
            return None
        job = self._sched.get_job(JOB_ID_DAILY)
        if job is None:
            return None
        return job.next_run_time

    # ----- internals ------------------------------------------------------

    def _register_daily(self, cfg) -> None:
        assert self._sched is not None
        hh, mm = cfg.schedule_local.split(":")
        tz = ZoneInfo(cfg.operator_timezone)
        trigger = CronTrigger(
            hour=int(hh),
            minute=int(mm),
            timezone=tz,
        )

        def _fire() -> None:
            today_local = datetime.now(tz).date()
            yesterday = today_local - timedelta(days=1)
            self.orchestrator.try_enqueue(yesterday, manual=False)

        self._sched.add_job(
            _fire,
            trigger=trigger,
            id=JOB_ID_DAILY,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        log.info(
            "scheduler.daily_registered schedule_local=%s tz=%s",
            cfg.schedule_local,
            cfg.operator_timezone,
        )

    def _bootstrap_audit_table(self, cfg) -> None:
        """Idempotent: CREATE TABLE IF NOT EXISTS + schema validation.
        Logged and tolerated — /healthz surfaces persistent schema_drift."""
        try:
            from .db import MysqlConfig, connect
            from .schema import bootstrap_and_validate

            mysql_cfg = MysqlConfig(
                host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
                password=cfg.mysql_password, database=cfg.mysql_database,
            )
            with connect(mysql_cfg) as conn:
                result = bootstrap_and_validate(
                    conn, database=cfg.mysql_database,
                )
            if result.ok:
                log.info("scheduler.bootstrap_audit_table ok")
            else:
                log.warning(
                    "scheduler.bootstrap_audit_table failed kind=%s msg=%s",
                    result.error_kind, result.error_message,
                )
        except Exception:
            log.exception("scheduler.bootstrap_audit_table raised")

    def _boot_catchup(self, cfg) -> None:
        try:
            today_local = datetime.now(ZoneInfo(cfg.operator_timezone)).date()
            enqueued, _ = self.orchestrator.catchup_missing(
                today_local=today_local,
                cap_days=cfg.catch_up_cap_days,
            )
            if enqueued:
                log.info(
                    "scheduler.boot_catchup enqueued=%d oldest=%s newest=%s",
                    len(enqueued), enqueued[0], enqueued[-1],
                )
        except Exception:
            # Catch-up is best-effort. The container still serves /healthz.
            log.exception("scheduler.boot_catchup failed")
