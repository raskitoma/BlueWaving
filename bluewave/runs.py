"""Run row lifecycle for ``z_audit_logs_efk_runs`` (spec §3.4 / §6.4).

Three concerns:

1. Start a run row before any work begins (``status='running'``).
2. Finalize the row on completion with the right ``status``, row counts, and
   optional error excerpt / screenshot path.
3. Boot-time reaper: any ``status='running'`` rows owned by this source are
   stuck (the container died mid-run). Flip them to ``ingest_failed`` so
   catch-up can re-queue the date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import pymysql


RUNS_TABLE = "z_audit_logs_efk_runs"

# Terminal statuses (spec §6.4.5).
TERMINAL_STATUSES = frozenset({
    "ok",
    "auth_failed",
    "nav_failed",
    "report_timeout",
    "download_failed",
    "parse_failed",
    "ingest_failed",
    "schema_drift",
    "skipped",
})

REAP_ERROR = "reaped on boot — container died mid-run"


@dataclass
class RunRow:
    """Convenience view of the columns we touch."""

    id: int
    source: str
    report_date: date
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    rows_in_csv: Optional[int]
    rows_inserted: Optional[int]
    rows_duplicate: Optional[int]
    manual: bool
    error_excerpt: Optional[str]
    screenshot_path: Optional[str]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def start_run(
    conn: pymysql.connections.Connection,
    *,
    source: str,
    report_date: date,
    manual: bool = False,
) -> int:
    """Insert a ``running`` row and return its ``id``."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {RUNS_TABLE}
              (source, report_date, started_at, status, manual)
            VALUES (%s, %s, %s, 'running', %s)
            """,
            (source, report_date, _now_utc(), 1 if manual else 0),
        )
        run_id = cur.lastrowid
    conn.commit()
    return int(run_id)


def finalize_run(
    conn: pymysql.connections.Connection,
    run_id: int,
    *,
    status: str,
    rows_in_csv: Optional[int] = None,
    rows_inserted: Optional[int] = None,
    rows_duplicate: Optional[int] = None,
    error_excerpt: Optional[str] = None,
    screenshot_path: Optional[str] = None,
) -> None:
    """Set the terminal columns. ``status`` must be in :data:`TERMINAL_STATUSES`."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"non-terminal status: {status!r}")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {RUNS_TABLE}
            SET finished_at = %s,
                status = %s,
                rows_in_csv = %s,
                rows_inserted = %s,
                rows_duplicate = %s,
                error_excerpt = %s,
                screenshot_path = %s
            WHERE id = %s
            """,
            (
                _now_utc(),
                status,
                rows_in_csv,
                rows_inserted,
                rows_duplicate,
                (error_excerpt or None) and error_excerpt[:2000],
                screenshot_path,
                run_id,
            ),
        )
    conn.commit()


def reap_stuck_runs(
    conn: pymysql.connections.Connection,
    *,
    source: str,
) -> int:
    """Flip any leftover ``running`` rows for ``source`` to ``ingest_failed``.

    Run at boot. Returns the number of rows reaped.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {RUNS_TABLE}
            SET status = 'ingest_failed',
                finished_at = COALESCE(finished_at, %s),
                error_excerpt = %s
            WHERE source = %s AND status = 'running'
            """,
            (_now_utc(), REAP_ERROR, source),
        )
        affected = cur.rowcount
    conn.commit()
    return int(affected)


def has_successful_scheduled_run(
    conn: pymysql.connections.Connection,
    *,
    source: str,
    report_date: date,
) -> bool:
    """Check whether the generated ``ok_scheduled_date`` constraint would
    already block a new scheduled (``manual=0``) ``ok`` row for this date.

    Used by the scheduler to avoid wasting a run on a date that's already
    ingested.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1 FROM {RUNS_TABLE}
            WHERE source = %s AND report_date = %s
              AND status = 'ok' AND manual = 0
            LIMIT 1
            """,
            (source, report_date),
        )
        return cur.fetchone() is not None


def find_ingested_dates(
    conn: pymysql.connections.Connection,
    *,
    source: str,
    since: date,
) -> set[date]:
    """Return the set of report_dates with a successful (``ok``) or
    ``skipped`` run since ``since`` (inclusive). Used by the catch-up
    algorithm (spec §6.4.3).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT report_date FROM {RUNS_TABLE}
            WHERE source = %s
              AND status IN ('ok', 'skipped')
              AND report_date >= %s
            """,
            (source, since),
        )
        rows = cur.fetchall()
    out: set[date] = set()
    for r in rows:
        # DictCursor → dict; otherwise tuple.
        v = r["report_date"] if isinstance(r, dict) else r[0]
        out.add(v)
    return out
