"""Idempotent batch insert into ``z_audit_logs_efk`` (spec §3.5 / §10/M6).

Public surface:

* :func:`insert_rows` — given an open ``pymysql`` connection and an iterable
  of :class:`bluewave.transform.Row`, insert with
  ``INSERT … ON DUPLICATE KEY UPDATE id = id``. Returns
  ``(rows_inserted, rows_duplicate)`` computed from row-count deltas around
  the wrapping ``ingest_run_id``.

The function is wrapping-transaction-aware: it does not start or commit a
transaction itself. The caller (``run_job()``) owns the transaction so that
a mid-insert crash leaves zero rows for that run (spec §10/M6).
"""
from __future__ import annotations

from typing import Iterable, Sequence

import pymysql

from .transform import Row


AUDIT_TABLE = "z_audit_logs_efk"

# Spec §3.5 — batch size kept well under MySQL's max_allowed_packet default
# (64 MB) even with verbose JSON columns.
DEFAULT_BATCH_SIZE = 500


def _chunks(seq: Sequence[Row], size: int) -> Iterable[Sequence[Row]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def insert_rows(
    conn: pymysql.connections.Connection,
    rows: Sequence[Row],
    *,
    ingest_run_id: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[int, int]:
    """Insert rows with idempotent-on-dedup_hash semantics.

    Returns ``(rows_inserted, rows_duplicate)``.

    ``rows_inserted`` is computed by ``SELECT COUNT(*)`` against the freshly
    tagged ``ingest_run_id`` after the insert batch — simpler than reasoning
    about driver-specific ``rowcount`` semantics for
    ``ON DUPLICATE KEY UPDATE``.
    """
    if not rows:
        return 0, 0

    insert_sql = (
        f"INSERT INTO {AUDIT_TABLE} "
        "(timestamp, source, operation, instance, user_name, user_id, "
        " extra_data, comments, dedup_hash, ingest_run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE id = id"
    )

    with conn.cursor() as cur:
        for batch in _chunks(rows, batch_size):
            params = [
                (
                    r.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],  # millisecond precision
                    r.source,
                    r.operation,
                    r.instance,
                    r.user_name,
                    r.user_id,
                    r.extra_data_str,
                    None,                # comments — always NULL for this worker
                    r.dedup_hash,
                    ingest_run_id,
                )
                for r in batch
            ]
            cur.executemany(insert_sql, params)

        cur.execute(
            f"SELECT COUNT(*) AS n FROM {AUDIT_TABLE} WHERE ingest_run_id = %s",
            (ingest_run_id,),
        )
        result = cur.fetchone()
        inserted = int(result["n"] if isinstance(result, dict) else result[0])

    duplicate = len(rows) - inserted
    return inserted, duplicate
