"""MySQL schema for the shared audit table + runs table (spec §3).

This module is the single source of truth for the DDL. It performs only two
operations against a target DB:

1. ``bootstrap(conn)`` — ``CREATE TABLE IF NOT EXISTS`` for both tables.
2. ``validate(conn, database)`` — compare ``information_schema`` against the
   expected column set / charset / collation and return a ``SchemaCheck``.

The module **never** issues ``ALTER`` or ``DROP``. The shared table is owned
collectively; drift is surfaced, never silently fixed (spec §3 intro).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

import pymysql


# ---------------------------------------------------------------------------
# DDL — exactly the form in spec §3.1 / §3.4
# ---------------------------------------------------------------------------

DDL_AUDIT_LOGS = """
CREATE TABLE IF NOT EXISTS z_audit_logs_efk (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  timestamp       DATETIME(3)     NOT NULL,
  source          VARCHAR(64)     NOT NULL,
  operation       VARCHAR(128)    NOT NULL,
  instance        VARCHAR(128)    NOT NULL,
  user_name       VARCHAR(256)    DEFAULT NULL,
  user_id         VARCHAR(64)     DEFAULT NULL,
  extra_data      JSON            DEFAULT NULL,
  comments        VARCHAR(1024)   DEFAULT NULL,
  dedup_hash      CHAR(64)        NOT NULL,
  ingested_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  ingest_run_id   BIGINT UNSIGNED DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_dedup_hash (dedup_hash),
  KEY ix_source_ts (source, timestamp),
  KEY ix_ts        (timestamp),
  KEY ix_userid_ts (user_id, timestamp)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
""".strip()


DDL_AUDIT_LOGS_RUNS = """
CREATE TABLE IF NOT EXISTS z_audit_logs_efk_runs (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source          VARCHAR(64)     NOT NULL,
  report_date     DATE            NOT NULL,
  started_at      DATETIME(3)     NOT NULL,
  finished_at     DATETIME(3)     DEFAULT NULL,
  status          VARCHAR(32)     NOT NULL,
  rows_in_csv     INT UNSIGNED    DEFAULT NULL,
  rows_inserted   INT UNSIGNED    DEFAULT NULL,
  rows_duplicate  INT UNSIGNED    DEFAULT NULL,
  manual          TINYINT(1)      NOT NULL DEFAULT 0,
  error_excerpt   VARCHAR(2000)   DEFAULT NULL,
  screenshot_path VARCHAR(512)    DEFAULT NULL,
  ok_scheduled_date DATE GENERATED ALWAYS AS
    (CASE WHEN status='ok' AND manual=0 THEN report_date ELSE NULL END) VIRTUAL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ok_scheduled_date (source, ok_scheduled_date),
  KEY ix_source_date (source, report_date),
  KEY ix_started (started_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
""".strip()


AUDIT_TABLE = "z_audit_logs_efk"
RUNS_TABLE = "z_audit_logs_efk_runs"


# ---------------------------------------------------------------------------
# Expected shape of z_audit_logs_efk (spec §3.1)
# ---------------------------------------------------------------------------

EXPECTED_CHARSET = "utf8mb4"
EXPECTED_COLLATION = "utf8mb4_unicode_ci"


# Column-type expectations. ``column_type`` matches MySQL's COLUMN_TYPE value
# (lowercased). ``is_nullable`` matches IS_NULLABLE ('YES' / 'NO'). String
# columns also enforce charset/collation (set ``has_charset=True``).
EXPECTED_COLUMNS_AUDIT_LOGS: dict[str, dict] = {
    "id":            {"column_type": "bigint unsigned", "is_nullable": "NO",  "extra": "auto_increment"},
    "timestamp":     {"column_type": "datetime(3)",     "is_nullable": "NO"},
    "source":        {"column_type": "varchar(64)",     "is_nullable": "NO",  "has_charset": True},
    "operation":     {"column_type": "varchar(128)",    "is_nullable": "NO",  "has_charset": True},
    "instance":      {"column_type": "varchar(128)",    "is_nullable": "NO",  "has_charset": True},
    "user_name":     {"column_type": "varchar(256)",    "is_nullable": "YES", "has_charset": True},
    "user_id":       {"column_type": "varchar(64)",     "is_nullable": "YES", "has_charset": True},
    "extra_data":    {"column_type": "json",            "is_nullable": "YES"},
    "comments":      {"column_type": "varchar(1024)",   "is_nullable": "YES", "has_charset": True},
    "dedup_hash":    {"column_type": "char(64)",        "is_nullable": "NO",  "has_charset": True},
    "ingested_at":   {"column_type": "datetime(3)",     "is_nullable": "NO"},
    "ingest_run_id": {"column_type": "bigint unsigned", "is_nullable": "YES"},
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SchemaCheck:
    """Outcome of validate(). ``ok`` is true iff ``diffs`` is empty and there
    was no fetch error."""

    ok: bool
    diffs: list[str] = field(default_factory=list)
    error: str | None = None

    def reason_key(self) -> str | None:
        """Stable key for /healthz ``reasons`` list."""
        if self.ok:
            return None
        return "schema_drift"


@dataclass
class BootstrapResult:
    """Outcome of bootstrap_and_validate(). Carries enough context for /healthz
    to pick the right ``reasons`` key (see spec §10/M2)."""

    ok: bool
    error_kind: str | None = None       # 'mysql_privilege', 'mysql_error', 'schema_drift'
    error_message: str | None = None
    schema_check: SchemaCheck | None = None


# ---------------------------------------------------------------------------
# Pure validation logic (unit-testable without a live DB)
# ---------------------------------------------------------------------------


def validate_columns(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Compare information_schema.COLUMNS rows against the expected set.

    ``rows`` is a list of mapping-like records as returned by a DictCursor for::

        SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
               CHARACTER_SET_NAME, COLLATION_NAME, EXTRA
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = 'z_audit_logs_efk'

    Returns a list of human-readable diff strings (empty list = OK).
    """
    diffs: list[str] = []
    found: dict[str, Mapping[str, object]] = {}

    for r in rows:
        name = str(r["COLUMN_NAME"])
        found[name] = r

    expected_names = set(EXPECTED_COLUMNS_AUDIT_LOGS)
    actual_names = set(found)

    for missing in sorted(expected_names - actual_names):
        diffs.append(f"missing column: {missing}")
    for extra in sorted(actual_names - expected_names):
        diffs.append(f"unexpected column: {extra}")

    for name in sorted(expected_names & actual_names):
        spec = EXPECTED_COLUMNS_AUDIT_LOGS[name]
        actual = found[name]

        # COLUMN_TYPE check (case-insensitive — MySQL is consistent
        # lowercase, but defensive against MariaDB / older versions).
        actual_type = str(actual["COLUMN_TYPE"]).lower()
        if actual_type != spec["column_type"]:
            diffs.append(
                f"column {name}: type {actual_type!r}, expected {spec['column_type']!r}"
            )

        # IS_NULLABLE check.
        if str(actual["IS_NULLABLE"]) != spec["is_nullable"]:
            diffs.append(
                f"column {name}: nullable={actual['IS_NULLABLE']!r}, "
                f"expected {spec['is_nullable']!r}"
            )

        # Charset / collation for string columns.
        if spec.get("has_charset"):
            cs = actual.get("CHARACTER_SET_NAME")
            co = actual.get("COLLATION_NAME")
            if cs != EXPECTED_CHARSET:
                diffs.append(
                    f"column {name}: charset={cs!r}, expected {EXPECTED_CHARSET!r} "
                    f"(legacy `utf8` silently truncates 4-byte chars; spec §3)"
                )
            if co != EXPECTED_COLLATION:
                diffs.append(
                    f"column {name}: collation={co!r}, expected {EXPECTED_COLLATION!r}"
                )

        # EXTRA check (auto_increment, generated, etc.).
        expected_extra = spec.get("extra", "")
        actual_extra = str(actual.get("EXTRA") or "").lower()
        if expected_extra and expected_extra not in actual_extra:
            diffs.append(
                f"column {name}: extra={actual_extra!r}, expected to contain "
                f"{expected_extra!r}"
            )

    return diffs


def validate_table_charset(table_row: Mapping[str, object] | None) -> list[str]:
    """Compare information_schema.TABLES row against expected table charset.

    ``table_row`` is the single record (or None if the table is absent) of::

        SELECT t.TABLE_COLLATION, c.CHARACTER_SET_NAME
        FROM information_schema.TABLES t
        JOIN information_schema.COLLATIONS c
          ON c.COLLATION_NAME = t.TABLE_COLLATION
        WHERE t.TABLE_SCHEMA = ? AND t.TABLE_NAME = 'z_audit_logs_efk'
    """
    if table_row is None:
        return [f"table {AUDIT_TABLE!r} does not exist"]

    diffs: list[str] = []
    cs = table_row.get("CHARACTER_SET_NAME")
    co = table_row.get("TABLE_COLLATION")
    if cs != EXPECTED_CHARSET:
        diffs.append(
            f"table charset={cs!r}, expected {EXPECTED_CHARSET!r} "
            f"(legacy `utf8` silently truncates 4-byte chars; spec §3)"
        )
    if co != EXPECTED_COLLATION:
        diffs.append(
            f"table collation={co!r}, expected {EXPECTED_COLLATION!r}"
        )
    return diffs


# ---------------------------------------------------------------------------
# Live-DB operations
# ---------------------------------------------------------------------------


def bootstrap(conn: pymysql.connections.Connection) -> None:
    """``CREATE TABLE IF NOT EXISTS`` for both tables.

    Idempotent. Raises ``pymysql.err.OperationalError`` if the user lacks
    ``CREATE`` privilege (errno 1142).
    """
    with conn.cursor() as cur:
        cur.execute(DDL_AUDIT_LOGS)
        cur.execute(DDL_AUDIT_LOGS_RUNS)
    conn.commit()


def _fetch_columns(
    conn: pymysql.connections.Connection, database: str
) -> list[Mapping[str, object]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
                   CHARACTER_SET_NAME, COLLATION_NAME, EXTRA
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database, AUDIT_TABLE),
        )
        return list(cur.fetchall())


def _fetch_table_charset(
    conn: pymysql.connections.Connection, database: str
) -> Mapping[str, object] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.TABLE_COLLATION, c.CHARACTER_SET_NAME
            FROM information_schema.TABLES t
            JOIN information_schema.COLLATIONS c
              ON c.COLLATION_NAME = t.TABLE_COLLATION
            WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s
            """,
            (database, AUDIT_TABLE),
        )
        row = cur.fetchone()
        return row


def validate(conn: pymysql.connections.Connection, database: str) -> SchemaCheck:
    """Query information_schema and compare to the expected shape."""
    try:
        col_rows = _fetch_columns(conn, database)
        table_row = _fetch_table_charset(conn, database)
    except pymysql.err.Error as e:
        return SchemaCheck(ok=False, error=f"information_schema query failed: {e}")

    diffs = list(validate_table_charset(table_row))
    # Only check columns if the table exists.
    if table_row is not None:
        diffs.extend(validate_columns(col_rows))

    return SchemaCheck(ok=not diffs, diffs=diffs)


# MySQL error number for "access denied" on a privilege check.
# https://dev.mysql.com/doc/mysql-errors/8.0/en/server-error-reference.html#error_er_tableaccess_denied_error
_MYSQL_ERRNO_ACCESS_DENIED = 1142
_MYSQL_ERRNO_INSUFFICIENT_PRIVS = 1227


def bootstrap_and_validate(
    conn: pymysql.connections.Connection, database: str
) -> BootstrapResult:
    """Run bootstrap then validate. Classify errors for /healthz consumption.

    Returns a ``BootstrapResult`` whose ``error_kind`` is one of:

    - ``None`` on success
    - ``'mysql_privilege'`` when the user lacks ``CREATE``
    - ``'mysql_error'`` for any other DB error during bootstrap
    - ``'schema_drift'`` when validation reports diffs
    """
    try:
        bootstrap(conn)
    except pymysql.err.OperationalError as e:
        errno = e.args[0] if e.args else None
        if errno in (_MYSQL_ERRNO_ACCESS_DENIED, _MYSQL_ERRNO_INSUFFICIENT_PRIVS):
            return BootstrapResult(
                ok=False, error_kind="mysql_privilege", error_message=str(e)
            )
        return BootstrapResult(ok=False, error_kind="mysql_error", error_message=str(e))
    except pymysql.err.Error as e:
        return BootstrapResult(ok=False, error_kind="mysql_error", error_message=str(e))

    check = validate(conn, database=database)
    if not check.ok:
        return BootstrapResult(
            ok=False, error_kind="schema_drift", schema_check=check
        )
    return BootstrapResult(ok=True, schema_check=check)
