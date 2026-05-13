"""M2 — unit tests for the pure schema-validation logic (spec §3 + §10/M2).

These tests do not require MySQL or Docker — they exercise
``validate_columns`` and ``validate_table_charset`` with prebuilt
information_schema-shaped fixtures.

Integration tests against a real MySQL live in
``test_m2_schema_integration.py``.
"""
from __future__ import annotations

import copy

import pytest

from bluewave.schema import (
    AUDIT_TABLE,
    EXPECTED_CHARSET,
    EXPECTED_COLLATION,
    EXPECTED_COLUMNS_AUDIT_LOGS,
    validate_columns,
    validate_table_charset,
)


# ---------------------------------------------------------------------------
# Fixtures — synthesize information_schema rows for a healthy table
# ---------------------------------------------------------------------------


def _healthy_column_rows() -> list[dict]:
    """Build the information_schema.COLUMNS rows MySQL would emit for a
    correctly-created z_audit_logs_efk table."""
    rows = []
    for name, spec in EXPECTED_COLUMNS_AUDIT_LOGS.items():
        rows.append(
            {
                "COLUMN_NAME": name,
                "COLUMN_TYPE": spec["column_type"],
                "IS_NULLABLE": spec["is_nullable"],
                "CHARACTER_SET_NAME": EXPECTED_CHARSET if spec.get("has_charset") else None,
                "COLLATION_NAME": EXPECTED_COLLATION if spec.get("has_charset") else None,
                "EXTRA": spec.get("extra", ""),
            }
        )
    return rows


def _healthy_table_row() -> dict:
    return {"TABLE_COLLATION": EXPECTED_COLLATION, "CHARACTER_SET_NAME": EXPECTED_CHARSET}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_healthy_columns_yield_no_diffs() -> None:
    assert validate_columns(_healthy_column_rows()) == []


def test_healthy_table_charset_yields_no_diffs() -> None:
    assert validate_table_charset(_healthy_table_row()) == []


# ---------------------------------------------------------------------------
# Column-level drift cases
# ---------------------------------------------------------------------------


def test_renamed_column_is_reported_as_missing_and_extra() -> None:
    """Spec §10/M2 pass criterion: rename user_id → userid → schema_drift."""
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "user_id":
            r["COLUMN_NAME"] = "userid"
    diffs = validate_columns(rows)
    joined = " | ".join(diffs)
    assert "missing column: user_id" in joined
    assert "unexpected column: userid" in joined


def test_missing_column_is_reported() -> None:
    rows = [r for r in _healthy_column_rows() if r["COLUMN_NAME"] != "dedup_hash"]
    diffs = validate_columns(rows)
    assert any("missing column: dedup_hash" in d for d in diffs)


def test_wrong_column_type_is_reported() -> None:
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "user_id":
            r["COLUMN_TYPE"] = "varchar(32)"  # too small
    diffs = validate_columns(rows)
    assert any("column user_id" in d and "varchar(32)" in d for d in diffs)


def test_wrong_nullability_is_reported() -> None:
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "comments":
            r["IS_NULLABLE"] = "NO"  # spec says nullable
    diffs = validate_columns(rows)
    assert any("column comments" in d and "nullable" in d for d in diffs)


def test_column_charset_drift_is_reported() -> None:
    """Spec §10/M2 pass criterion: legacy `utf8` charset on a column."""
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "user_name":
            r["CHARACTER_SET_NAME"] = "utf8"  # legacy 3-byte alias
            r["COLLATION_NAME"] = "utf8_general_ci"
    diffs = validate_columns(rows)
    msg = " | ".join(diffs)
    assert "column user_name" in msg
    assert "utf8" in msg
    assert "utf8mb4" in msg  # the expected value also mentioned


def test_column_collation_drift_is_reported() -> None:
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "source":
            r["COLLATION_NAME"] = "utf8mb4_general_ci"  # wrong collation
    diffs = validate_columns(rows)
    assert any("column source" in d and "collation" in d for d in diffs)


def test_missing_auto_increment_on_id_is_reported() -> None:
    rows = _healthy_column_rows()
    for r in rows:
        if r["COLUMN_NAME"] == "id":
            r["EXTRA"] = ""  # missing auto_increment
    diffs = validate_columns(rows)
    assert any("column id" in d and "auto_increment" in d for d in diffs)


def test_extra_column_is_reported() -> None:
    rows = _healthy_column_rows()
    rows.append(
        {
            "COLUMN_NAME": "legacy_pin",
            "COLUMN_TYPE": "varchar(4)",
            "IS_NULLABLE": "YES",
            "CHARACTER_SET_NAME": EXPECTED_CHARSET,
            "COLLATION_NAME": EXPECTED_COLLATION,
            "EXTRA": "",
        }
    )
    diffs = validate_columns(rows)
    assert any("unexpected column: legacy_pin" in d for d in diffs)


def test_column_types_are_compared_case_insensitively() -> None:
    """MariaDB sometimes returns COLUMN_TYPE in uppercase."""
    rows = copy.deepcopy(_healthy_column_rows())
    for r in rows:
        r["COLUMN_TYPE"] = str(r["COLUMN_TYPE"]).upper()
    assert validate_columns(rows) == []


# ---------------------------------------------------------------------------
# Table-level charset cases
# ---------------------------------------------------------------------------


def test_missing_table_yields_a_diff() -> None:
    diffs = validate_table_charset(None)
    assert diffs == [f"table {AUDIT_TABLE!r} does not exist"]


def test_legacy_utf8_table_charset_is_reported() -> None:
    """Spec §10/M2 pass criterion: pre-existing table with 3-byte `utf8`."""
    row = {"TABLE_COLLATION": "utf8_general_ci", "CHARACTER_SET_NAME": "utf8"}
    diffs = validate_table_charset(row)
    msg = " | ".join(diffs)
    assert "table charset" in msg
    assert "utf8" in msg and "utf8mb4" in msg
    assert "table collation" in msg


def test_table_collation_drift_only() -> None:
    row = {
        "TABLE_COLLATION": "utf8mb4_general_ci",
        "CHARACTER_SET_NAME": EXPECTED_CHARSET,
    }
    diffs = validate_table_charset(row)
    msg = " | ".join(diffs)
    assert "collation" in msg
    assert "utf8mb4_general_ci" in msg


# ---------------------------------------------------------------------------
# Smoke: DDL contains the column names we expect
# ---------------------------------------------------------------------------


def test_ddl_mentions_every_expected_column() -> None:
    """If someone edits EXPECTED_COLUMNS_AUDIT_LOGS without editing the DDL
    (or vice-versa), this catches it."""
    from bluewave.schema import DDL_AUDIT_LOGS

    for name in EXPECTED_COLUMNS_AUDIT_LOGS:
        assert name in DDL_AUDIT_LOGS, f"DDL missing column reference: {name}"


def test_ddl_runs_table_has_generated_partial_unique() -> None:
    """Spec §3.4 — the generated column and its UNIQUE index keep duplicate
    successful scheduled runs out."""
    from bluewave.schema import DDL_AUDIT_LOGS_RUNS

    assert "ok_scheduled_date" in DDL_AUDIT_LOGS_RUNS
    assert "GENERATED ALWAYS AS" in DDL_AUDIT_LOGS_RUNS
    assert "uk_ok_scheduled_date" in DDL_AUDIT_LOGS_RUNS


# ---------------------------------------------------------------------------
# db.connect signature — defends against future DSN regression
# ---------------------------------------------------------------------------


def test_db_connect_uses_kwargs_only(monkeypatch) -> None:
    """Spec L19 / §10/M2 pass criterion: pymysql.connect is called with
    keyword args including charset='utf8mb4', not a DSN."""
    captured: dict = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()  # we never use the return

    # Reach into the imported module so monkeypatch applies to the actual call.
    from bluewave import db

    monkeypatch.setattr(db.pymysql, "connect", fake_connect)

    db.connect(
        db.MysqlConfig(
            host="db.local",
            port=3306,
            user="audit_writer",
            password="p@ss/with:weird@chars",
            database="audit",
        )
    )

    # Every required parameter must be present as a keyword.
    for key in ("host", "port", "user", "password", "database", "charset"):
        assert key in captured, f"missing keyword arg: {key}"

    assert captured["charset"] == "utf8mb4"
    # The weird password survives intact (no DSN parsing).
    assert captured["password"] == "p@ss/with:weird@chars"
    # Defensive: no positional args were used.
    # (fake_connect only accepts **kwargs, so any positional would have raised.)
