"""Run orchestration: in-process queue + ``run_job`` (spec §6.4 / §10/M8).

Two concerns kept on purpose tight:

1. :func:`run_job` — synchronous, end-to-end execution of one report_date.
   Pure orchestration: spin driver, login, scrape, transform, sink, finalize.
   Imports the real heavy modules at call time so unit tests can monkeypatch.

2. :class:`Orchestrator` — thread-safe in-process queue. The HTTP layer (M7)
   calls ``try_enqueue`` / ``catchup_missing``. The scheduler (M8) shares the
   same Orchestrator instance and enqueues the daily fire. A single
   background thread drains the queue, one run at a time.
"""
from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional


log = logging.getLogger(__name__)


DEFAULT_DOWNLOAD_DIR = "/tmp/bluewave-dl"
DEFAULT_SCREENSHOT_DIR = "/var/lib/bluewave-worker/screenshots"


# ---------------------------------------------------------------------------
# run_job — the actual end-to-end work
# ---------------------------------------------------------------------------


def run_job(
    store,                           # ConfigStore
    report_date: date,
    *,
    manual: bool,
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
    screenshot_dir: str = DEFAULT_SCREENSHOT_DIR,
) -> str:
    """Execute one full pull. Returns the terminal status string.

    Spec §6.4.1 — the state machine in code. Lazy imports keep this module
    cheap to import (the FastAPI app imports the orchestrator at startup).
    """
    from .db import MysqlConfig, connect
    from .driver import SafeDriver, build_driver
    from .exceptions import RunFailure
    from .login import login_and_navigate, save_screenshot
    from .runs import finalize_run, start_run
    from .scrape import scrape_event_report
    from .sink import insert_rows
    from .transform import transform

    cfg = store.load()
    if cfg is None:
        raise RuntimeError("run_job invoked without a saved config")

    mysql_cfg = MysqlConfig(
        host=cfg.mysql_host,
        port=cfg.mysql_port,
        user=cfg.mysql_user,
        password=cfg.mysql_password,
        database=cfg.mysql_database,
    )

    with connect(mysql_cfg) as conn:
        run_id = start_run(
            conn,
            source="Bluewave",
            report_date=report_date,
            manual=manual,
        )
        log.info("run.started run_id=%d report_date=%s manual=%s",
                 run_id, report_date, manual)

        screenshot_path: Optional[str] = None
        try:
            with build_driver(download_dir=download_dir) as raw:
                safe = SafeDriver(raw)
                login_and_navigate(
                    safe, cfg.blueweb_url, cfg.blueweb_user, cfg.blueweb_password,
                )
                csv_path = scrape_event_report(
                    safe, report_date, download_dir=download_dir,
                )

                if csv_path is None:
                    finalize_run(
                        conn, run_id, status="ok",
                        rows_in_csv=0, rows_inserted=0, rows_duplicate=0,
                    )
                    log.info("run.finished run_id=%d status=ok rows_in_csv=0",
                             run_id)
                    return "ok"

                rows = transform(str(csv_path), timezone=cfg.operator_timezone)
                inserted, duplicate = insert_rows(
                    conn, rows, ingest_run_id=run_id,
                )
                finalize_run(
                    conn, run_id, status="ok",
                    rows_in_csv=len(rows),
                    rows_inserted=inserted,
                    rows_duplicate=duplicate,
                )

                try:
                    Path(csv_path).unlink()
                except OSError:
                    pass

                log.info(
                    "run.finished run_id=%d status=ok rows_in=%d inserted=%d dup=%d",
                    run_id, len(rows), inserted, duplicate,
                )
                return "ok"
        except RunFailure as e:
            # Best-effort screenshot for forensics.
            try:
                from .driver import build_driver  # safety re-import — n/a if driver dead
            except Exception:
                pass
            finalize_run(
                conn, run_id, status=e.status,
                error_excerpt=str(e)[:2000],
                screenshot_path=screenshot_path,
            )
            log.warning("run.failed run_id=%d status=%s", run_id, e.status)
            return e.status
        except Exception as e:
            tb = traceback.format_exc()
            finalize_run(
                conn, run_id, status="ingest_failed",
                error_excerpt=tb[:2000],
            )
            log.exception("run.failed run_id=%d status=ingest_failed", run_id)
            return "ingest_failed"


# ---------------------------------------------------------------------------
# Orchestrator — the in-process queue + worker thread
# ---------------------------------------------------------------------------


@dataclass
class EnqueueResult:
    accepted: bool
    reason: str
    queue_depth: int


class Orchestrator:
    """Single-flight queue. Drains serially on one background thread.

    The HTTP layer calls :meth:`try_enqueue` and :meth:`catchup_missing`.
    The scheduler (M8) calls the same methods.
    """

    def __init__(
        self,
        store,                       # ConfigStore
        job_fn: Callable[[object, date, bool], str] | None = None,
    ) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._queue: deque[tuple[date, bool]] = deque()
        self._in_flight: Optional[date] = None
        self._worker: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        # Injection point for tests — bypass real Selenium / MySQL.
        self._job_fn = job_fn or (
            lambda store, d, manual: run_job(store, d, manual=manual)
        )

    # --- introspection ----------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "queued": [d for d, _ in self._queue],
                "queue_depth": len(self._queue),
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._in_flight is not None

    def queued_dates(self) -> set[date]:
        with self._lock:
            return {d for d, _ in self._queue}

    # --- enqueue paths ----------------------------------------------------

    def try_enqueue(
        self, report_date: date, *, manual: bool,
    ) -> EnqueueResult:
        """Add a run to the queue. Returns ``EnqueueResult``."""
        with self._lock:
            in_flight_or_queued = (
                report_date == self._in_flight
                or any(d == report_date for d, _ in self._queue)
            )
            if in_flight_or_queued:
                return EnqueueResult(False, "already_queued", len(self._queue))
            self._queue.append((report_date, manual))
            self._ensure_worker_locked()
            return EnqueueResult(True, "queued", len(self._queue))

    def catchup_missing(
        self, *, today_local: date, cap_days: int,
    ) -> tuple[list[date], list[date]]:
        """Find dates without an ``ok``/``skipped`` run and enqueue them
        oldest first. Returns ``(enqueued, skipped_already_queued)``.

        Uses :func:`bluewave.runs.find_ingested_dates` for the DB query so
        the result reflects the persisted history, not just the in-memory queue.
        """
        from .db import MysqlConfig, connect
        from .runs import find_ingested_dates

        cfg = self.store.load()
        if cfg is None:
            return [], []

        earliest = today_local - timedelta(days=cap_days)
        candidates = sorted(
            today_local - timedelta(days=i)
            for i in range(1, cap_days + 1)
        )

        mysql_cfg = MysqlConfig(
            host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
            password=cfg.mysql_password, database=cfg.mysql_database,
        )
        with connect(mysql_cfg) as conn:
            ingested = find_ingested_dates(
                conn, source="Bluewave", since=earliest,
            )

        with self._lock:
            queued_now = {d for d, _ in self._queue}
            in_flight = self._in_flight

            enqueued: list[date] = []
            skipped: list[date] = []
            for d in candidates:
                if d in ingested:
                    continue
                if d in queued_now or d == in_flight:
                    skipped.append(d)
                    continue
                self._queue.append((d, False))
                enqueued.append(d)

            if enqueued:
                self._ensure_worker_locked()
        return enqueued, skipped

    # --- worker thread ----------------------------------------------------

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._consume, daemon=True, name="bluewave-runner",
            )
            self._worker.start()

    def _consume(self) -> None:
        while not self._shutdown.is_set():
            with self._lock:
                if not self._queue:
                    return
                self._in_flight, manual = self._queue.popleft()
            try:
                self._job_fn(self.store, self._in_flight, manual)
            except Exception:
                log.exception("orchestrator.job_failed date=%s", self._in_flight)
            finally:
                with self._lock:
                    self._in_flight = None

    def shutdown(self, *, join_timeout_s: float = 60.0) -> None:
        self._shutdown.set()
        if self._worker is not None:
            self._worker.join(timeout=join_timeout_s)
