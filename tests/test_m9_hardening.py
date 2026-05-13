"""M9 — hardening tests (spec §10/M9).

Covers what we can verify without a 7-day soak:

- Screenshot retention GC keeps at most N files (default 60).
- JSON log lines are parseable + carry the required keys (§9.2).
- ``rotate_config`` re-encrypts the SQLite config when a new head key is
  prepended (spec §13.8 / L21).
- The closed RunFailure status set matches the spec's §6.4.5 enum.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from cryptography.fernet import Fernet

from bluewave.config import (
    Config, ConfigStore, build_multifernet, parse_keys_env,
)
from bluewave.exceptions import (
    AuthFailed, DenylistedSelector, DownloadFailed, IngestFailed, NavFailed,
    ParseFailed, ReportTimeout, RunFailure, SchemaDriftError,
)
from bluewave.logging_setup import JsonFormatter, configure_logging
from bluewave.screenshots import gc, list_pngs


# ---------------------------------------------------------------------------
# Screenshot retention
# ---------------------------------------------------------------------------


def test_screenshots_gc_noop_under_cap(tmp_path) -> None:
    for i in range(5):
        (tmp_path / f"shot_{i}.png").write_bytes(b"x")
    removed = gc(tmp_path, keep=10)
    assert removed == 0
    assert len(list_pngs(tmp_path)) == 5


def test_screenshots_gc_trims_oldest(tmp_path) -> None:
    for i in range(10):
        p = tmp_path / f"shot_{i}.png"
        p.write_bytes(b"x")
        # Force monotonically increasing mtimes.
        os.utime(p, (time.time() - (100 - i), time.time() - (100 - i)))
    removed = gc(tmp_path, keep=3)
    assert removed == 7
    survivors = list_pngs(tmp_path)
    assert len(survivors) == 3
    # The survivors are the three NEWEST files.
    names = sorted(p.name for p in survivors)
    assert names == ["shot_7.png", "shot_8.png", "shot_9.png"]


def test_screenshots_gc_ignores_non_png(tmp_path) -> None:
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"x")
    (tmp_path / "c.jpg").write_bytes(b"x")
    removed = gc(tmp_path, keep=0)
    assert removed == 1
    # Non-png files survived.
    assert (tmp_path / "b.txt").exists()
    assert (tmp_path / "c.jpg").exists()


# ---------------------------------------------------------------------------
# Closed RunFailure status set (spec §6.4.5 / §9.2 catalog)
# ---------------------------------------------------------------------------


def test_runfailure_subclass_statuses() -> None:
    expected = {
        AuthFailed: "auth_failed",
        NavFailed: "nav_failed",
        ReportTimeout: "report_timeout",
        DownloadFailed: "download_failed",
        ParseFailed: "parse_failed",
        IngestFailed: "ingest_failed",
        SchemaDriftError: "schema_drift",
        DenylistedSelector: "nav_failed",
    }
    for cls, status in expected.items():
        assert cls.status == status, f"{cls.__name__} status wrong"
    # Base catch-all.
    assert RunFailure.status == "ingest_failed"


# ---------------------------------------------------------------------------
# Structured JSON logs
# ---------------------------------------------------------------------------


def test_json_log_lines_parseable(capsys) -> None:
    import logging

    configure_logging("INFO")
    log = logging.getLogger("bluewave.test")
    log.info("run.started run_id=42 report_date=2026-05-12")
    log.warning("config.save_failed reason=mysql")
    captured = capsys.readouterr().out.strip().splitlines()
    assert len(captured) >= 2
    for line in captured:
        record = json.loads(line)
        for key in JsonFormatter.REQUIRED_KEYS:
            assert key in record, f"missing {key} in {record}"


def test_logs_redact_card_numbers(capsys) -> None:
    """A defensive belt: any log message containing 'card_number' is wiped."""
    import logging

    configure_logging("INFO")
    log = logging.getLogger("bluewave.sink")
    log.info("inserted with card_number=4458 facility=255")
    out = capsys.readouterr().out
    assert "4458" not in out
    assert "redacted" in out


# ---------------------------------------------------------------------------
# rotate_config CLI
# ---------------------------------------------------------------------------


def test_rotate_config_re_encrypts_with_new_head_key(tmp_path) -> None:
    """End-to-end: write a config with key A, prepend key B, run
    rotate_config, then assert that the file decrypts with only key B."""

    key_a = Fernet.generate_key().decode("ascii")
    key_b = Fernet.generate_key().decode("ascii")

    sqlite_path = tmp_path / "config.sqlite"

    # 1) Save under key_a alone.
    mf_a = build_multifernet(parse_keys_env(key_a))
    store_a = ConfigStore(sqlite_path, mf_a)
    store_a.save(
        Config(
            site_label="Test",
            blueweb_url="http://x",
            blueweb_user="u",
            blueweb_password="pw-bw",
            operator_timezone="America/New_York",
            mysql_host="x",
            mysql_port=3306,
            mysql_database="audit",
            mysql_user="u",
            mysql_password="pw-db",
            schedule_local="03:00",
            catch_up_cap_days=14,
        )
    )

    # 2) Run rotate_config with new head + old fallback.
    env = os.environ.copy()
    env["CONFIG_ENC_KEYS"] = f"{key_b},{key_a}"
    env["BLUEWAVE_CONFIG_PATH"] = str(sqlite_path)

    proc = subprocess.run(
        [sys.executable, "-m", "bluewave.rotate_config"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    # 3) After rotation, key_b alone must be able to decrypt.
    mf_b_only = build_multifernet(parse_keys_env(key_b))
    store_b = ConfigStore(sqlite_path, mf_b_only)
    cfg = store_b.load()
    assert cfg is not None
    assert cfg.blueweb_password == "pw-bw"
    assert cfg.mysql_password == "pw-db"


def test_rotate_config_returns_nonzero_when_no_config(tmp_path) -> None:
    sqlite_path = tmp_path / "config.sqlite"
    key = Fernet.generate_key().decode("ascii")
    env = os.environ.copy()
    env["CONFIG_ENC_KEYS"] = key
    env["BLUEWAVE_CONFIG_PATH"] = str(sqlite_path)
    proc = subprocess.run(
        [sys.executable, "-m", "bluewave.rotate_config"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
