"""Connectivity probes for ``POST /config`` save-time validation (spec §7.1).

Three probes, run in order; if any fails, the form is rejected and no
config is persisted:

1. BlueWeb HEAD (HTTP reachability)
2. MySQL ``SELECT 1`` + charset/collation check (§3 / §10/M2)
3. Selenium login round-trip (catches wrong creds / changed selectors)

Probes return ``ProbeResult`` so callers (form save, ``Test BlueWeb`` /
``Test MySQL`` buttons) can render a clear status without seeing the
raw exception.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pymysql
import urllib.error
import urllib.request

from .config import Config
from .db import MysqlConfig, connect
from .driver import SafeDriver, build_driver
from .exceptions import AuthFailed, RunFailure
from .login import login_and_navigate


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


def probe_blueweb_http(*, url: str, timeout_s: float = 5.0) -> ProbeResult:
    """HEAD ``url``. Failure → ``ok=False`` with the error name."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = resp.getcode()
        if code >= 500:
            return ProbeResult(False, f"HTTP {code} from BlueWeb")
        return ProbeResult(True, f"HEAD {code}")
    except urllib.error.URLError as e:
        return ProbeResult(False, f"unreachable: {e.reason}")
    except Exception as e:  # pragma: no cover — defensive
        return ProbeResult(False, f"{type(e).__name__}: {e}")


def probe_mysql(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> ProbeResult:
    """``SELECT 1`` + charset/collation sanity check on the audit DB."""
    try:
        with connect(
            MysqlConfig(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
            )
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
                cur.execute(
                    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                    "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
                    (database,),
                )
                row = cur.fetchone()
        if not row:
            return ProbeResult(False, f"database {database!r} not visible")
        # DictCursor — keys uppercased.
        cs = row.get("DEFAULT_CHARACTER_SET_NAME")
        co = row.get("DEFAULT_COLLATION_NAME")

        # Strict at the *charset* level — `utf8` (3-byte legacy alias)
        # silently truncates 4-byte characters (spec L20).
        if cs != "utf8mb4":
            return ProbeResult(
                False,
                f"database charset {cs!r}, expected 'utf8mb4' "
                f"(legacy `utf8` truncates 4-byte chars; spec L20)",
            )

        # Lenient at the DB-default *collation* level — any utf8mb4_* is
        # safe because our CREATE TABLE DDL pins the audit table itself to
        # `utf8mb4_unicode_ci` regardless of the DB default. The strict
        # collation check runs against the *table* at worker startup
        # (see bluewave/schema.py).
        if not isinstance(co, str) or not co.startswith("utf8mb4_"):
            return ProbeResult(
                False,
                f"database collation {co!r}, expected a utf8mb4_* collation",
            )
        if co != "utf8mb4_unicode_ci":
            return ProbeResult(
                True,
                f"SELECT 1 OK (DB collation {co!r}; the audit table will be "
                f"created with utf8mb4_unicode_ci regardless)",
            )
        return ProbeResult(True, "SELECT 1 OK, charset + collation OK")
    except pymysql.err.Error as e:
        return ProbeResult(False, f"{type(e).__name__}: {e}")


def probe_bluewave_login(*, url: str, user: str, password: str) -> ProbeResult:
    """Spin a one-shot headless Chromium and run S1+S2.

    Slow (~10–20 s) but catches the cases other probes can't: wrong creds,
    BlueWeb returning a different login form after an upgrade, selectors
    drifting.
    """
    try:
        with build_driver() as raw:
            safe = SafeDriver(raw)
            login_and_navigate(
                safe, url, user, password,
                login_wait_s=20, nav_wait_s=10,
            )
    except AuthFailed as e:
        return ProbeResult(False, f"auth_failed: {e}")
    except RunFailure as e:
        return ProbeResult(False, f"{e.status}: {e}")
    except Exception as e:  # pragma: no cover — defensive
        return ProbeResult(False, f"{type(e).__name__}: {e}")
    return ProbeResult(True, "login + Reports navigation OK")


def triple_probe(cfg: Config) -> tuple[bool, list[tuple[str, ProbeResult]]]:
    """Run all three probes in order. Short-circuit on the first failure
    so the operator sees one clear error, not a stack of them.

    Returns ``(overall_ok, [(name, ProbeResult), ...])``.
    """
    results: list[tuple[str, ProbeResult]] = []

    r = probe_blueweb_http(url=cfg.blueweb_url)
    results.append(("blueweb_http", r))
    if not r.ok:
        return False, results

    r = probe_mysql(
        host=cfg.mysql_host,
        port=cfg.mysql_port,
        database=cfg.mysql_database,
        user=cfg.mysql_user,
        password=cfg.mysql_password,
    )
    results.append(("mysql", r))
    if not r.ok:
        return False, results

    r = probe_bluewave_login(
        url=cfg.blueweb_url,
        user=cfg.blueweb_user,
        password=cfg.blueweb_password,
    )
    results.append(("bluewave_login", r))
    if not r.ok:
        return False, results

    return True, results
