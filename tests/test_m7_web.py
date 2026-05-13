"""M7 — web GUI tests (spec §10/M7).

Covers:
- Basic Auth gate (401 without, 200 with valid)
- CSRF gate on state-changing endpoints
- /config GET renders + masks passwords; POST runs triple-probe before save
- /run rejects today/future report_date; rejects concurrent attempts
- /run/catchup returns enqueued + already-queued split
- /docs gated by LOG_LEVEL=DEBUG
- /healthz reflects configured state
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from bluewave.auth import CSRF_HEADER, issue_csrf_token
from bluewave.config import Config, ConfigStore, build_multifernet, parse_keys_env
from bluewave.orchestrator import EnqueueResult, Orchestrator
from bluewave.web import create_app

from tests.conftest import TEST_WEB_PASS, TEST_WEB_USER


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers() -> dict:
    creds = f"{TEST_WEB_USER}:{TEST_WEB_PASS}".encode("ascii")
    return {"Authorization": "Basic " + base64.b64encode(creds).decode("ascii")}


def _fresh_store(tmp_path):
    import os
    mf = build_multifernet(parse_keys_env(os.environ["CONFIG_ENC_KEYS"]))
    return ConfigStore(tmp_path / "config.sqlite", mf)


def _sample_config() -> Config:
    return Config(
        site_label="Easy Foods Inc.",
        blueweb_url="http://blueweb.lan",
        blueweb_user="admin",
        blueweb_password="bw-pass",
        operator_timezone="America/New_York",
        mysql_host="mysql.lan",
        mysql_port=3306,
        mysql_database="audit",
        mysql_user="audit_writer",
        mysql_password="db-pass",
        schedule_local="03:00",
        catch_up_cap_days=14,
    )


@pytest.fixture
def store(tmp_path):
    return _fresh_store(tmp_path)


@pytest.fixture
def configured_store(tmp_path):
    s = _fresh_store(tmp_path)
    s.save(_sample_config())
    return s


@pytest.fixture
def fake_orch():
    orch = MagicMock(spec=Orchestrator)
    orch.state.return_value = {"in_flight": None, "queued": [], "queue_depth": 0}
    orch.try_enqueue.return_value = EnqueueResult(True, "queued", 1)
    orch.catchup_missing.return_value = ([date(2026, 5, 11)], [])
    return orch


@pytest.fixture
def client(store, fake_orch):
    app = create_app(store=store, orchestrator=fake_orch)
    return TestClient(app)


@pytest.fixture
def cfg_client(configured_store, fake_orch):
    app = create_app(store=configured_store, orchestrator=fake_orch)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Basic Auth
# ---------------------------------------------------------------------------


def test_root_requires_basic_auth(client) -> None:
    r = client.get("/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_root_accepts_correct_basic_auth(cfg_client, auth_headers) -> None:
    r = cfg_client.get("/", headers=auth_headers)
    assert r.status_code == 200
    assert "Easy Foods Inc." in r.text


def test_root_rejects_wrong_password(cfg_client) -> None:
    bad = base64.b64encode(b"admin:wrong").decode("ascii")
    r = cfg_client.get("/", headers={"Authorization": f"Basic {bad}"})
    assert r.status_code == 401


def test_healthz_does_not_require_auth(client) -> None:
    r = client.get("/healthz")
    assert r.status_code in (200, 503)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_post_run_without_csrf_returns_403(cfg_client, auth_headers) -> None:
    r = cfg_client.post("/run", json={"report_date": None}, headers=auth_headers)
    assert r.status_code == 403


def test_post_run_with_invalid_csrf_returns_403(cfg_client, auth_headers) -> None:
    headers = dict(auth_headers, **{CSRF_HEADER: "not-a-real-token"})
    r = cfg_client.post("/run", json={"report_date": None}, headers=headers)
    assert r.status_code == 403


def test_csrf_endpoint_issues_token(cfg_client, auth_headers) -> None:
    body = cfg_client.get("/csrf", headers=auth_headers).json()
    assert body["header"] == CSRF_HEADER
    assert isinstance(body["token"], str) and body["token"]


# ---------------------------------------------------------------------------
# /healthz — configured vs unconfigured
# ---------------------------------------------------------------------------


def test_healthz_unconfigured(client) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "unconfigured"
    assert body["configured"] is False


def test_healthz_configured_returns_200(cfg_client) -> None:
    r = cfg_client.get("/healthz")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "ok"
    assert body["site_label"] == "Easy Foods Inc."
    assert body["operator_tz"] == "America/New_York"
    assert body["next_run_at_utc"] is not None
    assert body["next_run_at_local"] is not None


# ---------------------------------------------------------------------------
# /config — GET form, POST save with triple-probe
# ---------------------------------------------------------------------------


def test_config_get_renders_form(cfg_client, auth_headers) -> None:
    r = cfg_client.get("/config", headers=auth_headers)
    assert r.status_code == 200
    assert "csrf_token" in r.text
    # Password field never inlines the value.
    assert "bw-pass" not in r.text
    assert "db-pass" not in r.text


def test_config_post_runs_triple_probe_and_saves(cfg_client, auth_headers, store) -> None:
    csrf = issue_csrf_token()
    payload = {
        "site_label": "Other Site",
        "blueweb_url": "http://blueweb-2.lan",
        "blueweb_user": "admin",
        "blueweb_password": "",  # empty = keep existing
        "operator_timezone": "America/Los_Angeles",
        "mysql_host": "mysql.lan",
        "mysql_port": 3306,
        "mysql_database": "audit",
        "mysql_user": "audit_writer",
        "mysql_password": "",
        "schedule_local": "04:00",
        "catch_up_cap_days": 7,
    }
    # Patch triple_probe so we don't touch network/Selenium.
    with patch("bluewave.routes.triple_probe") as tp:
        from bluewave.probes import ProbeResult
        tp.return_value = (True, [
            ("blueweb_http", ProbeResult(True, "ok")),
            ("mysql", ProbeResult(True, "ok")),
            ("bluewave_login", ProbeResult(True, "ok")),
        ])
        r = cfg_client.post(
            "/config",
            json=payload,
            headers={**auth_headers, CSRF_HEADER: csrf},
        )
    assert r.status_code == 200
    assert r.json()["saved"] is True

    # Side-effect: store reflects the new site label.
    refreshed = cfg_client.app.state.store.load()
    assert refreshed.site_label == "Other Site"
    assert refreshed.operator_timezone == "America/Los_Angeles"
    # Empty password preserved the existing one.
    assert refreshed.blueweb_password == "bw-pass"


def test_config_post_rejects_on_probe_failure(cfg_client, auth_headers) -> None:
    csrf = issue_csrf_token()
    payload = {
        "site_label": "Other",
        "blueweb_url": "http://blueweb.lan",
        "blueweb_user": "admin",
        "blueweb_password": "",
        "operator_timezone": "America/New_York",
        "mysql_host": "mysql.lan",
        "mysql_port": 3306,
        "mysql_database": "audit",
        "mysql_user": "audit_writer",
        "mysql_password": "",
        "schedule_local": "03:00",
        "catch_up_cap_days": 14,
    }
    with patch("bluewave.routes.triple_probe") as tp:
        from bluewave.probes import ProbeResult
        tp.return_value = (False, [
            ("blueweb_http", ProbeResult(True, "ok")),
            ("mysql", ProbeResult(False, "auth failed")),
        ])
        r = cfg_client.post(
            "/config",
            json=payload,
            headers={**auth_headers, CSRF_HEADER: csrf},
        )
    assert r.status_code == 400
    assert r.json()["saved"] is False

    # Existing config untouched.
    refreshed = cfg_client.app.state.store.load()
    assert refreshed.site_label == "Easy Foods Inc."


def test_config_invalid_timezone_rejected(cfg_client, auth_headers) -> None:
    csrf = issue_csrf_token()
    payload = {
        "site_label": "x",
        "blueweb_url": "http://x",
        "blueweb_user": "u",
        "blueweb_password": "p",
        "operator_timezone": "Mars/Olympus",
        "mysql_host": "x",
        "mysql_port": 3306,
        "mysql_database": "x",
        "mysql_user": "u",
        "mysql_password": "p",
        "schedule_local": "03:00",
        "catch_up_cap_days": 14,
    }
    r = cfg_client.post(
        "/config",
        json=payload,
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 422


def test_config_invalid_schedule_rejected(cfg_client, auth_headers) -> None:
    csrf = issue_csrf_token()
    payload = {
        "site_label": "x", "blueweb_url": "http://x", "blueweb_user": "u",
        "blueweb_password": "p", "operator_timezone": "America/New_York",
        "mysql_host": "x", "mysql_port": 3306, "mysql_database": "x",
        "mysql_user": "u", "mysql_password": "p",
        "schedule_local": "25:99",
        "catch_up_cap_days": 14,
    }
    r = cfg_client.post(
        "/config", json=payload,
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /run
# ---------------------------------------------------------------------------


def test_post_run_rejects_today(cfg_client, auth_headers) -> None:
    csrf = issue_csrf_token()
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    r = cfg_client.post(
        "/run", json={"report_date": today},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 400


def test_post_run_rejects_future(cfg_client, auth_headers) -> None:
    csrf = issue_csrf_token()
    from zoneinfo import ZoneInfo

    tomorrow = (datetime.now(ZoneInfo("America/New_York")).date()
                + timedelta(days=1)).isoformat()
    r = cfg_client.post(
        "/run", json={"report_date": tomorrow},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 400


def test_post_run_rejects_beyond_safety_cap(cfg_client, auth_headers, monkeypatch) -> None:
    monkeypatch.setenv("BACKFILL_SAFETY_CAP_DAYS", "30")
    csrf = issue_csrf_token()
    long_ago = (date.today() - timedelta(days=400)).isoformat()
    r = cfg_client.post(
        "/run", json={"report_date": long_ago},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 400


def test_post_run_returns_409_when_orchestrator_refuses(
    cfg_client, auth_headers, fake_orch,
) -> None:
    fake_orch.try_enqueue.return_value = EnqueueResult(False, "already_queued", 1)
    csrf = issue_csrf_token()
    r = cfg_client.post(
        "/run", json={"report_date": None},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 409


def test_post_run_happy_path_returns_202(cfg_client, auth_headers, fake_orch) -> None:
    csrf = issue_csrf_token()
    r = cfg_client.post(
        "/run", json={"report_date": None},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["enqueued"] is True
    # Spec §7 — manual via UI sets manual=true.
    args, kwargs = fake_orch.try_enqueue.call_args
    assert kwargs["manual"] is True


def test_post_run_when_unconfigured_returns_409(client, auth_headers) -> None:
    csrf = issue_csrf_token()
    r = client.post(
        "/run", json={"report_date": None},
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# /run/catchup
# ---------------------------------------------------------------------------


def test_catchup_returns_enqueued_and_skipped(cfg_client, auth_headers, fake_orch) -> None:
    fake_orch.catchup_missing.return_value = (
        [date(2026, 5, 10), date(2026, 5, 11)],
        [date(2026, 5, 9)],
    )
    csrf = issue_csrf_token()
    r = cfg_client.post(
        "/run/catchup",
        headers={**auth_headers, CSRF_HEADER: csrf},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["enqueued"] == ["2026-05-10", "2026-05-11"]
    assert body["skipped_already_queued"] == ["2026-05-09"]


# ---------------------------------------------------------------------------
# /docs gating
# ---------------------------------------------------------------------------


def test_docs_disabled_at_info_level(cfg_client) -> None:
    r = cfg_client.get("/docs")
    assert r.status_code == 404


def test_docs_enabled_at_debug_level_requires_auth(store, fake_orch, monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    app = create_app(store=store, orchestrator=fake_orch)
    client = TestClient(app)
    # Without auth → 401.
    r_no = client.get("/docs")
    assert r_no.status_code == 401


# ---------------------------------------------------------------------------
# Orchestrator state-machine unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_orchestrator_blocks_duplicate_while_in_flight(tmp_path, store) -> None:
    """While a job is executing, re-enqueueing the same date is rejected.

    Concurrent /run pressed twice for yesterday must not double-run.
    """
    import threading

    job_running = threading.Event()
    job_may_finish = threading.Event()

    def slow_job(_store, _d, _manual):
        job_running.set()
        # Block until the test releases us — keeps the date "in flight".
        job_may_finish.wait(timeout=5)
        return "ok"

    orch = Orchestrator(store, job_fn=slow_job)
    try:
        r1 = orch.try_enqueue(date(2026, 5, 12), manual=True)
        assert r1.accepted

        # Wait until the worker thread is actually inside slow_job — i.e.
        # the date is now `_in_flight` (queue empty).
        assert job_running.wait(timeout=2)

        # Now re-enqueue the same date: must be rejected because it's
        # currently in flight.
        r2 = orch.try_enqueue(date(2026, 5, 12), manual=True)
        assert not r2.accepted, "in-flight duplicate was not blocked"
        assert r2.reason == "already_queued"
    finally:
        job_may_finish.set()
        orch.shutdown(join_timeout_s=2)


def test_orchestrator_blocks_duplicate_while_queued(tmp_path, store) -> None:
    """While a date is sitting in the queue (different date in flight),
    re-enqueueing the queued date is rejected."""
    import threading

    job_running = threading.Event()
    job_may_finish = threading.Event()

    def slow_job(_s, _d, _manual):
        job_running.set()
        job_may_finish.wait(timeout=5)
        return "ok"

    orch = Orchestrator(store, job_fn=slow_job)
    try:
        # First date occupies the worker.
        assert orch.try_enqueue(date(2026, 5, 11), manual=True).accepted
        assert job_running.wait(timeout=2)

        # Second date — queued behind it.
        assert orch.try_enqueue(date(2026, 5, 12), manual=True).accepted
        # Third enqueue of the SAME second date — should be rejected.
        r3 = orch.try_enqueue(date(2026, 5, 12), manual=True)
        assert not r3.accepted
        assert r3.reason == "already_queued"
    finally:
        job_may_finish.set()
        orch.shutdown(join_timeout_s=2)
