"""Dry-run: login + scrape + transform without writing to MySQL.

Used by the operator to validate the pipeline end-to-end and preview what
would land in ``z_audit_logs_efk`` before committing data. The Selenium
side runs exactly as a real run does — same selectors, same date inputs,
same CSV download — only the final ``INSERT`` is skipped.

The returned :class:`DryRunResult` carries:

- total CSV row count + first ~10 raw rows
- total transformed row count + first ~10 typed rows (each with the
  ``dedup_hash`` that would land in the DB)
- ``would_insert`` / ``would_skip_duplicate`` counts based on which
  ``dedup_hash`` values are already present in ``z_audit_logs_efk``
"""
from __future__ import annotations

import csv
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .config import ConfigStore
from .db import MysqlConfig, connect
from .driver import SafeDriver, build_driver
from .exceptions import RunFailure
from .login import login_and_navigate
from .scrape import scrape_event_report
from .transform import EXPECTED_HEADER, Row, transform


log = logging.getLogger(__name__)


PREVIEW_LIMIT = 10
EXISTING_HASH_CHUNK = 500


@dataclass
class DryRunResult:
    report_date: date
    status: str                                # "ok" | "empty" | "failed"
    error: Optional[str] = None
    csv_rows_total: int = 0
    csv_preview: list[dict] = field(default_factory=list)
    transformed_rows_total: int = 0
    transformed_preview: list[dict] = field(default_factory=list)
    would_insert: int = 0
    would_skip_duplicate: int = 0

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date.isoformat(),
            "status": self.status,
            "error": self.error,
            "csv_rows_total": self.csv_rows_total,
            "csv_preview": self.csv_preview,
            "transformed_rows_total": self.transformed_rows_total,
            "transformed_preview": self.transformed_preview,
            "would_insert": self.would_insert,
            "would_skip_duplicate": self.would_skip_duplicate,
        }


def _count_csv_rows(csv_path: Path) -> int:
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return sum(1 for _ in reader)


def _csv_preview(csv_path: Path, limit: int) -> list[dict]:
    out: list[dict] = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for i, row in enumerate(reader):
            if i >= limit:
                break
            if len(row) >= len(EXPECTED_HEADER):
                out.append(dict(zip(EXPECTED_HEADER, row)))
    return out


def _row_to_preview(r: Row) -> dict:
    return {
        "timestamp": r.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": r.source,
        "operation": r.operation,
        "instance": r.instance,
        "user_name": r.user_name,
        "user_id": r.user_id,
        "extra_data": r.extra_data_str,
        "dedup_hash": r.dedup_hash[:16] + "…",
    }


def _existing_hashes(
    mysql_cfg: MysqlConfig, rows: list[Row]
) -> set[str]:
    """Return the subset of dedup_hashes that already exist in the table."""
    if not rows:
        return set()
    existing: set[str] = set()
    with connect(mysql_cfg) as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), EXISTING_HASH_CHUNK):
                batch = rows[i:i + EXISTING_HASH_CHUNK]
                hashes = [r.dedup_hash for r in batch]
                placeholders = ",".join(["%s"] * len(hashes))
                cur.execute(
                    f"SELECT dedup_hash FROM z_audit_logs_efk "
                    f"WHERE dedup_hash IN ({placeholders})",
                    hashes,
                )
                for row in cur.fetchall():
                    key = row["dedup_hash"] if isinstance(row, dict) else row[0]
                    existing.add(key)
    return existing


def dry_run(store: ConfigStore, report_date: date) -> DryRunResult:
    """Run the full Selenium + transform pipeline without inserting to MySQL.

    Uses an isolated, ephemeral download directory so concurrent scheduled
    runs (if any) can't collide on the CSV file.
    """
    cfg = store.load()
    if cfg is None:
        return DryRunResult(
            report_date=report_date,
            status="failed",
            error="worker is not configured",
        )

    download_dir = tempfile.mkdtemp(prefix="bluewave-dl-dry-")
    csv_path: Optional[Path] = None
    try:
        try:
            with build_driver(download_dir=download_dir) as raw:
                safe = SafeDriver(raw)
                login_and_navigate(
                    safe,
                    cfg.blueweb_url,
                    cfg.blueweb_user,
                    cfg.blueweb_password,
                )
                csv_path = scrape_event_report(
                    safe, report_date, download_dir=download_dir,
                )
        except RunFailure as e:
            return DryRunResult(
                report_date=report_date,
                status="failed",
                error=f"{e.status}: {e}",
            )
        except Exception as e:  # pragma: no cover — defensive
            log.exception("dry_run: scrape failed")
            return DryRunResult(
                report_date=report_date,
                status="failed",
                error=f"{type(e).__name__}: {e}",
            )

        if csv_path is None:
            return DryRunResult(report_date=report_date, status="empty")

        csv_total = _count_csv_rows(csv_path)
        csv_prev = _csv_preview(csv_path, PREVIEW_LIMIT)

        try:
            rows = transform(str(csv_path), timezone=cfg.operator_timezone)
        except Exception as e:
            return DryRunResult(
                report_date=report_date,
                status="failed",
                error=f"transform failed: {e}",
                csv_rows_total=csv_total,
                csv_preview=csv_prev,
            )

        mysql_cfg = MysqlConfig(
            host=cfg.mysql_host,
            port=cfg.mysql_port,
            user=cfg.mysql_user,
            password=cfg.mysql_password,
            database=cfg.mysql_database,
        )
        try:
            existing = _existing_hashes(mysql_cfg, rows)
        except Exception as e:
            log.warning("dry_run: dedup lookup failed: %s", e)
            existing = set()

        would_skip = sum(1 for r in rows if r.dedup_hash in existing)
        would_insert = len(rows) - would_skip

        return DryRunResult(
            report_date=report_date,
            status="ok",
            csv_rows_total=csv_total,
            csv_preview=csv_prev,
            transformed_rows_total=len(rows),
            transformed_preview=[_row_to_preview(r) for r in rows[:PREVIEW_LIMIT]],
            would_insert=would_insert,
            would_skip_duplicate=would_skip,
        )
    finally:
        if csv_path is not None:
            try:
                Path(csv_path).unlink()
            except OSError:
                pass
        shutil.rmtree(download_dir, ignore_errors=True)
