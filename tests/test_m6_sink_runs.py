"""M6 — sink + run-lifecycle integration tests (spec §10/M6).

Most of this verifies end-to-end against a real MySQL via testcontainers
(skipped without Docker). A small unit slice covers the in-memory shape of
``Row`` and the sink's empty-input handling.
"""
from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit-level (no DB)
# ---------------------------------------------------------------------------


def test_sink_returns_zero_zero_on_empty_input() -> None:
    from bluewave.sink import insert_rows

    # Pass a dummy conn that should never be touched.
    class _Boom:
        def cursor(self):
            raise AssertionError("sink touched the DB for an empty input")

    assert insert_rows(_Boom(), [], ingest_run_id=1) == (0, 0)


def test_terminal_statuses_are_closed_set() -> None:
    """Defensive: the closed status set must match spec §6.4.5."""
    from bluewave.runs import TERMINAL_STATUSES

    expected = {
        "ok",
        "auth_failed",
        "nav_failed",
        "report_timeout",
        "download_failed",
        "parse_failed",
        "ingest_failed",
        "schema_drift",
        "skipped",
    }
    assert TERMINAL_STATUSES == expected


def test_finalize_run_rejects_running() -> None:
    from bluewave.runs import finalize_run

    class _Boom:
        def cursor(self):
            raise AssertionError("finalize touched the DB after invalid status")

    with pytest.raises(ValueError):
        finalize_run(_Boom(), 1, status="running")


# ---------------------------------------------------------------------------
# Integration (testcontainers)
# ---------------------------------------------------------------------------

_docker_missing = shutil.which("docker") is None
pytestmark = pytest.mark.skipif(
    _docker_missing,
    reason="Docker not available — testcontainers cannot spin up MySQL",
)

try:
    from testcontainers.mysql import MySqlContainer  # type: ignore[import-untyped]
except Exception as e:  # pragma: no cover
    pytestmark = pytest.mark.skip(reason=f"testcontainers unavailable: {e}")
    MySqlContainer = None  # type: ignore[assignment]


import pymysql  # noqa: E402

from bluewave.db import MysqlConfig, connect  # noqa: E402
from bluewave.runs import (  # noqa: E402
    find_ingested_dates,
    finalize_run,
    has_successful_scheduled_run,
    reap_stuck_runs,
    start_run,
)
from bluewave.schema import bootstrap  # noqa: E402
from bluewave.sink import insert_rows  # noqa: E402
from bluewave.transform import Row  # noqa: E402


@pytest.fixture(scope="session")
def mysql_container():
    with MySqlContainer("mysql:8.0", charset="utf8mb4") as c:
        yield c


@pytest.fixture
def mysql_config(mysql_container) -> MysqlConfig:
    db_name = f"audit_m6_{id(mysql_container)}_{datetime.now().microsecond}"
    root_conn = pymysql.connect(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user="root",
        password=mysql_container.password,
        charset="utf8mb4",
        autocommit=True,
    )
    with root_conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE {db_name} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    root_conn.close()
    yield MysqlConfig(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user="root",
        password=mysql_container.password,
        database=db_name,
    )


def _mk_row(idx: int, *, hash_seed: str | None = None) -> Row:
    return Row(
        timestamp=datetime(2026, 5, 12, 22, 0, idx, tzinfo=timezone.utc),
        source="Bluewave",
        operation="Admit W1",
        instance="Parts",
        user_name="Borregales, Pedro",
        user_id="6162",
        extra_data_str='{"card_number":"4522","facility_code":"190"}',
        dedup_hash=(hash_seed or f"row{idx:04d}") + "x" * (64 - len(hash_seed or f"row{idx:04d}")),
    )


# --- spec §10/M6 pass criteria ---------------------------------------------


def test_full_cycle_fresh_insert(mysql_config: MysqlConfig) -> None:
    with connect(mysql_config) as conn:
        bootstrap(conn)
        run_id = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12))
        rows = [_mk_row(i) for i in range(5)]
        inserted, duplicate = insert_rows(conn, rows, ingest_run_id=run_id)
        finalize_run(
            conn,
            run_id,
            status="ok",
            rows_in_csv=len(rows),
            rows_inserted=inserted,
            rows_duplicate=duplicate,
        )

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM z_audit_logs_efk")
            n = cur.fetchone()["n"]
        assert n == 5
        assert (inserted, duplicate) == (5, 0)


def test_full_cycle_is_idempotent_on_rerun(mysql_config: MysqlConfig) -> None:
    rows = [_mk_row(i) for i in range(5)]
    with connect(mysql_config) as conn:
        bootstrap(conn)

        run1 = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12))
        insert_rows(conn, rows, ingest_run_id=run1)
        finalize_run(conn, run1, status="ok", rows_in_csv=5, rows_inserted=5,
                     rows_duplicate=0)

        run2 = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12),
                         manual=True)
        inserted2, duplicate2 = insert_rows(conn, rows, ingest_run_id=run2)
        finalize_run(conn, run2, status="ok", rows_in_csv=5,
                     rows_inserted=inserted2, rows_duplicate=duplicate2)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM z_audit_logs_efk")
            assert cur.fetchone()["n"] == 5
    assert (inserted2, duplicate2) == (0, 5)


def test_one_row_mutated_inserts_only_that_row(mysql_config: MysqlConfig) -> None:
    rows = [_mk_row(i) for i in range(5)]
    with connect(mysql_config) as conn:
        bootstrap(conn)
        run1 = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12))
        insert_rows(conn, rows, ingest_run_id=run1)
        finalize_run(conn, run1, status="ok", rows_in_csv=5, rows_inserted=5,
                     rows_duplicate=0)

        # Mutate one row's operation → new dedup_hash.
        mutated = list(rows)
        mutated[2] = Row(
            timestamp=mutated[2].timestamp,
            source=mutated[2].source,
            operation="Reject W1",      # changed
            instance=mutated[2].instance,
            user_name=mutated[2].user_name,
            user_id=mutated[2].user_id,
            extra_data_str=mutated[2].extra_data_str,
            dedup_hash="reject" + "y" * 58,   # new unique hash
        )
        run2 = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12),
                         manual=True)
        inserted, duplicate = insert_rows(conn, mutated, ingest_run_id=run2)
        finalize_run(conn, run2, status="ok", rows_in_csv=5,
                     rows_inserted=inserted, rows_duplicate=duplicate)

    assert (inserted, duplicate) == (1, 4)


def test_reaper_flips_running_to_ingest_failed(mysql_config: MysqlConfig) -> None:
    with connect(mysql_config) as conn:
        bootstrap(conn)
        rid = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12))
        # NOTE: simulate a crash by NOT finalizing.

        reaped = reap_stuck_runs(conn, source="Bluewave")
        assert reaped == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, error_excerpt FROM z_audit_logs_efk_runs WHERE id = %s",
                (rid,),
            )
            row = cur.fetchone()
    assert row["status"] == "ingest_failed"
    assert "reaped" in (row["error_excerpt"] or "")


def test_has_successful_scheduled_run(mysql_config: MysqlConfig) -> None:
    with connect(mysql_config) as conn:
        bootstrap(conn)
        rid = start_run(conn, source="Bluewave", report_date=date(2026, 5, 12))
        finalize_run(conn, rid, status="ok", rows_in_csv=0, rows_inserted=0,
                     rows_duplicate=0)

        assert has_successful_scheduled_run(
            conn, source="Bluewave", report_date=date(2026, 5, 12)
        )
        assert not has_successful_scheduled_run(
            conn, source="Bluewave", report_date=date(2026, 5, 11)
        )


def test_find_ingested_dates_covers_ok_and_skipped(mysql_config: MysqlConfig) -> None:
    today = date(2026, 5, 13)
    with connect(mysql_config) as conn:
        bootstrap(conn)

        for d, status, manual in [
            (today - timedelta(days=1), "ok", False),
            (today - timedelta(days=2), "skipped", False),
            (today - timedelta(days=3), "ingest_failed", False),
            (today - timedelta(days=4), "ok", True),  # manual ok also counts
        ]:
            rid = start_run(conn, source="Bluewave", report_date=d, manual=manual)
            finalize_run(conn, rid, status=status)

        ingested = find_ingested_dates(
            conn, source="Bluewave", since=today - timedelta(days=14)
        )
    assert today - timedelta(days=1) in ingested
    assert today - timedelta(days=2) in ingested
    assert today - timedelta(days=3) not in ingested
    assert today - timedelta(days=4) in ingested
