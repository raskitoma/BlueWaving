"""M2 — integration tests against a real MySQL via testcontainers.

These verify the end-to-end pass criteria from spec §10/M2:

- Bootstrap is idempotent (SHOW CREATE TABLE identical before / after).
- Duplicate ``dedup_hash`` insert is rejected at the unique-constraint level.
- A renamed column triggers ``schema_drift``.
- A pre-existing legacy-``utf8`` table triggers ``schema_drift`` naming the
  charset.
- A user with no ``CREATE`` privilege gets ``mysql_privilege``.
- The generated ``ok_scheduled_date`` column enforces "at most one successful
  scheduled run per (source, date)" while still allowing manual runs.

The whole module is skipped if Docker is unavailable.
"""
from __future__ import annotations

import shutil

import pytest


# Skip the entire module if Docker isn't installed / reachable.
_docker_missing = shutil.which("docker") is None
pytestmark = pytest.mark.skipif(
    _docker_missing,
    reason="Docker not available — testcontainers cannot spin up MySQL",
)


# Importing testcontainers may itself fail in some envs. Wrap to give a
# clearer skip reason.
try:
    from testcontainers.mysql import MySqlContainer  # type: ignore[import-untyped]
except Exception as e:  # pragma: no cover - import-time skip
    pytestmark = pytest.mark.skip(reason=f"testcontainers unavailable: {e}")
    MySqlContainer = None  # type: ignore[assignment]


import pymysql

from bluewave.db import MysqlConfig, connect
from bluewave.schema import (
    AUDIT_TABLE,
    DDL_AUDIT_LOGS,
    bootstrap,
    bootstrap_and_validate,
    validate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mysql_container():
    """Spin up MySQL 8 once per test session."""
    with MySqlContainer("mysql:8.0", charset="utf8mb4") as c:
        yield c


@pytest.fixture
def mysql_config(mysql_container) -> MysqlConfig:
    """A fresh empty DB per test, owned by the container's root user."""
    root_conn = pymysql.connect(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user="root",
        password=mysql_container.password,
        charset="utf8mb4",
        autocommit=True,
    )
    db_name = f"audit_{id(root_conn)}"
    with root_conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    root_conn.close()

    cfg = MysqlConfig(
        host=mysql_container.get_container_host_ip(),
        port=int(mysql_container.get_exposed_port(3306)),
        user="root",
        password=mysql_container.password,
        database=db_name,
    )
    yield cfg


# ---------------------------------------------------------------------------
# Pass criteria
# ---------------------------------------------------------------------------


def test_bootstrap_then_validate_is_clean(mysql_config: MysqlConfig) -> None:
    with connect(mysql_config) as conn:
        result = bootstrap_and_validate(conn, database=mysql_config.database)
    assert result.ok, result.error_message or result.schema_check
    assert result.error_kind is None


def test_bootstrap_is_idempotent(mysql_config: MysqlConfig) -> None:
    """Two bootstrap calls leave SHOW CREATE TABLE byte-identical (spec §10/M2)."""
    with connect(mysql_config) as conn:
        bootstrap(conn)
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE {AUDIT_TABLE}")
            first = cur.fetchone()
        bootstrap(conn)
        with conn.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE {AUDIT_TABLE}")
            second = cur.fetchone()
    assert first == second


def test_dedup_hash_unique_constraint(mysql_config: MysqlConfig) -> None:
    """Inserting the same dedup_hash twice → only one row survives (spec §10/M2)."""
    with connect(mysql_config) as conn:
        bootstrap(conn)
        with conn.cursor() as cur:
            row = (
                "2026-05-12 22:00:00.000",
                "Bluewave",
                "Admit W1",
                "Parts",
                "Borregales, Pedro",
                "6162",
                None,
                None,
                "deadbeef" * 8,  # 64-char hex
            )
            insert = (
                f"INSERT INTO {AUDIT_TABLE} "
                "(timestamp, source, operation, instance, user_name, user_id, "
                " extra_data, comments, dedup_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE id = id"
            )
            cur.execute(insert, row)
            cur.execute(insert, row)
            conn.commit()
            cur.execute(
                f"SELECT COUNT(*) AS n FROM {AUDIT_TABLE} WHERE dedup_hash = %s",
                (row[-1],),
            )
            assert cur.fetchone()["n"] == 1


def test_renamed_column_is_schema_drift(mysql_config: MysqlConfig) -> None:
    """Pre-create the table with userid instead of user_id; bootstrap_and_validate
    must report schema_drift, not silently ALTER (spec §10/M2)."""
    drifted_ddl = DDL_AUDIT_LOGS.replace("user_id         VARCHAR(64)",
                                         "userid          VARCHAR(64)")
    with connect(mysql_config) as conn:
        with conn.cursor() as cur:
            cur.execute(drifted_ddl)
        conn.commit()
        result = bootstrap_and_validate(conn, database=mysql_config.database)
    assert not result.ok
    assert result.error_kind == "schema_drift"
    assert result.schema_check is not None
    joined = " | ".join(result.schema_check.diffs)
    assert "missing column: user_id" in joined
    assert "unexpected column: userid" in joined


def test_legacy_utf8_charset_is_schema_drift(mysql_config: MysqlConfig) -> None:
    """Spec §10/M2 pass criterion: pre-existing utf8 (3-byte) table flagged."""
    legacy_ddl = DDL_AUDIT_LOGS.replace(
        "DEFAULT CHARSET=utf8mb4\n  COLLATE=utf8mb4_unicode_ci",
        "DEFAULT CHARSET=utf8\n  COLLATE=utf8_general_ci",
    )
    with connect(mysql_config) as conn:
        with conn.cursor() as cur:
            cur.execute(legacy_ddl)
        conn.commit()
        result = bootstrap_and_validate(conn, database=mysql_config.database)

    assert not result.ok
    assert result.error_kind == "schema_drift"
    assert result.schema_check is not None
    joined = " | ".join(result.schema_check.diffs)
    assert "charset" in joined
    assert "utf8" in joined and "utf8mb4" in joined


def test_low_privilege_user_yields_mysql_privilege(
    mysql_container, mysql_config: MysqlConfig
) -> None:
    """A user with SELECT/INSERT only (no CREATE) cannot bootstrap (spec §10/M2)."""
    root_conn = pymysql.connect(
        host=mysql_config.host,
        port=mysql_config.port,
        user="root",
        password=mysql_container.password,
        charset="utf8mb4",
        autocommit=True,
    )
    with root_conn.cursor() as cur:
        cur.execute("CREATE USER 'noperm'@'%' IDENTIFIED BY 'pw'")
        cur.execute(
            f"GRANT SELECT, INSERT ON {mysql_config.database}.* TO 'noperm'@'%'"
        )
    root_conn.close()

    low_cfg = MysqlConfig(
        host=mysql_config.host,
        port=mysql_config.port,
        user="noperm",
        password="pw",
        database=mysql_config.database,
    )
    with connect(low_cfg) as conn:
        result = bootstrap_and_validate(conn, database=low_cfg.database)
    assert not result.ok
    assert result.error_kind == "mysql_privilege"


def test_generated_partial_unique_enforces_one_ok_scheduled(
    mysql_config: MysqlConfig,
) -> None:
    """Spec §3.4 / §10/M2: the generated ok_scheduled_date column allows
    only one successful scheduled run per (source, report_date), while a
    manual=1 row can coexist."""
    from bluewave.schema import RUNS_TABLE

    with connect(mysql_config) as conn:
        bootstrap(conn)
        with conn.cursor() as cur:
            ins = (
                f"INSERT INTO {RUNS_TABLE} "
                "(source, report_date, started_at, finished_at, status, manual) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            )

            # First successful scheduled run — OK.
            cur.execute(
                ins,
                ("Bluewave", "2026-05-12",
                 "2026-05-13 07:00:00.000", "2026-05-13 07:00:18.000",
                 "ok", 0),
            )

            # Second successful scheduled run for the same date — duplicate-key.
            with pytest.raises(pymysql.err.IntegrityError):
                cur.execute(
                    ins,
                    ("Bluewave", "2026-05-12",
                     "2026-05-13 07:30:00.000", "2026-05-13 07:30:18.000",
                     "ok", 0),
                )

            # A failed scheduled run for the same date — allowed (its
            # generated column is NULL).
            cur.execute(
                ins,
                ("Bluewave", "2026-05-12",
                 "2026-05-13 08:00:00.000", "2026-05-13 08:00:01.000",
                 "ingest_failed", 0),
            )

            # A successful MANUAL run for the same date — allowed (manual=1
            # → generated column NULL).
            cur.execute(
                ins,
                ("Bluewave", "2026-05-12",
                 "2026-05-13 09:00:00.000", "2026-05-13 09:00:18.000",
                 "ok", 1),
            )

        conn.commit()
