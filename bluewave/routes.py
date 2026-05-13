"""HTTP routes (spec §7).

Surface (every route except ``/healthz`` and ``/docs`` requires Basic Auth;
every state-changing route additionally requires a valid CSRF token):

  GET  /                       dashboard (HTML)
  GET  /healthz                health JSON (no auth)
  GET  /config                 config form (HTML)
  POST /config                 save config (JSON body); runs triple-probe
  POST /config/test/bluewave   stateless BlueWeb probe (JSON body)
  POST /config/test/mysql      stateless MySQL probe   (JSON body)
  POST /run                    enqueue a run
  POST /run/catchup            enqueue every missing day
  GET  /runs                   paginated runs list (JSON or HTML by Accept)
  GET  /runs/{id}              one run detail (JSON)
  GET  /csrf                   issue a fresh CSRF token (auth required)
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import (
    Body, Depends, FastAPI, HTTPException, Query, Request, status,
)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .auth import (
    CSRF_HEADER, issue_csrf_token, require_basic_auth, require_csrf,
)
from .config import Config
from .probes import probe_bluewave_login, probe_mysql, triple_probe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(app: FastAPI):
    return app.state.store


def _orch(app: FastAPI):
    return app.state.orchestrator


def _today_local(cfg: Optional[Config]) -> date:
    tz = ZoneInfo(cfg.operator_timezone) if cfg else timezone.utc
    return datetime.now(tz).date()


def _next_run_utc(cfg: Config) -> Optional[datetime]:
    """Compute next scheduled fire as UTC. Returns None if schedule unparseable."""
    try:
        hh, mm = cfg.schedule_local.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    tz = ZoneInfo(cfg.operator_timezone)
    now_local = datetime.now(tz)
    candidate = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ConfigPayload(BaseModel):
    site_label: str = Field(min_length=1, max_length=80)
    blueweb_url: str
    blueweb_user: str
    blueweb_password: str = ""        # empty = keep existing
    operator_timezone: str
    mysql_host: str
    mysql_port: int = Field(ge=1, le=65535, default=3306)
    mysql_database: str
    mysql_user: str
    mysql_password: str = ""          # empty = keep existing
    schedule_local: str               # HH:MM
    catch_up_cap_days: int = Field(ge=1, le=90, default=14)

    @field_validator("schedule_local")
    @classmethod
    def _schedule_format(cls, v: str) -> str:
        try:
            h, m = v.split(":")
            assert 0 <= int(h) <= 23
            assert 0 <= int(m) <= 59
        except (ValueError, AssertionError) as e:
            raise ValueError(f"schedule_local must be HH:MM, got {v!r}") from e
        return v

    @field_validator("operator_timezone")
    @classmethod
    def _tz_known(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as e:
            raise ValueError(f"unknown IANA timezone: {v!r}") from e
        return v


class RunRequest(BaseModel):
    report_date: Optional[str] = None  # YYYY-MM-DD or None for "yesterday"


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_routes(app: FastAPI) -> None:

    # =====================================================================
    # /healthz — no auth, no CSRF (spec §9.1)
    # =====================================================================

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        store = _store(app)
        cfg = store.load() if store.exists() else None
        boot_ts = getattr(app.state, "boot_ts", time.time())
        version = getattr(app.state, "version", "unknown")
        uptime = int(time.time() - boot_ts)

        # Disk free under /var/lib/bluewave-worker.
        try:
            disk_free = shutil.disk_usage(
                os.path.dirname(getattr(store, "path", os.getcwd())) or "."
            ).free // (1024 * 1024)
        except OSError:
            disk_free = None

        # Unconfigured branch.
        if cfg is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unconfigured",
                    "reasons": ["no_config"],
                    "uptime_seconds": uptime,
                    "configured": False,
                    "schema_ok": None,
                    "disk_free_mb": disk_free,
                    "version": version,
                },
            )

        next_run = _next_run_utc(cfg)
        orch = _orch(app)
        catchup_pending = orch.state()["queue_depth"]

        reasons: list[str] = []
        if disk_free is not None and disk_free < 100:
            reasons.append("disk_low")

        if reasons:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "reasons": reasons,
                    "uptime_seconds": uptime,
                    "configured": True,
                    "site_label": cfg.site_label,
                    "operator_tz": cfg.operator_timezone,
                    "next_run_at_utc": next_run.isoformat() if next_run else None,
                    "disk_free_mb": disk_free,
                    "catchup_pending": catchup_pending,
                    "version": version,
                },
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "uptime_seconds": uptime,
                "configured": True,
                "site_label": cfg.site_label,
                "operator_tz": cfg.operator_timezone,
                "next_run_at_utc": next_run.isoformat() if next_run else None,
                "next_run_at_local": (
                    next_run.astimezone(ZoneInfo(cfg.operator_timezone)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if next_run
                    else None
                ),
                "catchup_pending": catchup_pending,
                "disk_free_mb": disk_free,
                "version": version,
            },
        )

    # =====================================================================
    # /csrf — auth-only, mints a token
    # =====================================================================

    @app.get("/csrf")
    def csrf(_: str = Depends(require_basic_auth)) -> dict:
        return {"token": issue_csrf_token(), "header": CSRF_HEADER}

    # =====================================================================
    # /config — GET (HTML), POST (JSON save)
    # =====================================================================

    @app.get("/config", response_class=HTMLResponse)
    def get_config(_: str = Depends(require_basic_auth)) -> HTMLResponse:
        cfg = _store(app).load()
        token = issue_csrf_token()
        return HTMLResponse(_render_config_html(cfg, token))

    @app.post("/config")
    async def post_config(
        payload: ConfigPayload,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> JSONResponse:
        store = _store(app)
        existing = store.load()

        # Empty password fields → keep existing (spec §7).
        blueweb_pw = payload.blueweb_password or (
            existing.blueweb_password if existing else ""
        )
        mysql_pw = payload.mysql_password or (
            existing.mysql_password if existing else ""
        )
        if not blueweb_pw:
            raise HTTPException(400, "blueweb_password required on first save")
        if not mysql_pw:
            raise HTTPException(400, "mysql_password required on first save")

        new_cfg = Config(
            site_label=payload.site_label,
            blueweb_url=payload.blueweb_url,
            blueweb_user=payload.blueweb_user,
            blueweb_password=blueweb_pw,
            operator_timezone=payload.operator_timezone,
            mysql_host=payload.mysql_host,
            mysql_port=payload.mysql_port,
            mysql_database=payload.mysql_database,
            mysql_user=payload.mysql_user,
            mysql_password=mysql_pw,
            schedule_local=payload.schedule_local,
            catch_up_cap_days=payload.catch_up_cap_days,
        )

        overall_ok, results = triple_probe(new_cfg)
        report = [{"probe": n, "ok": r.ok, "detail": r.detail} for n, r in results]
        if not overall_ok:
            return JSONResponse(status_code=400, content={
                "saved": False, "probes": report,
            })

        store.save(new_cfg)
        # Reschedule the daily cron now that the schedule / timezone may
        # have changed (spec §10/M7 + §10/M8 pass criteria).
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            try:
                scheduler.reconfigure()
            except Exception:  # pragma: no cover - defensive
                pass
        return JSONResponse(status_code=200, content={
            "saved": True, "probes": report,
        })

    # =====================================================================
    # /config/test/{probe} — stateless, JSON body matching ConfigPayload
    # =====================================================================

    @app.post("/config/test/bluewave")
    async def test_bluewave(
        payload: ConfigPayload,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> dict:
        if not payload.blueweb_password:
            raise HTTPException(400, "blueweb_password required for test")
        cfg = _payload_to_cfg(payload)
        r = probe_bluewave_login(cfg)
        return {"result": "ok" if r.ok else "failed", "detail": r.detail}

    @app.post("/config/test/mysql")
    async def test_mysql(
        payload: ConfigPayload,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> dict:
        if not payload.mysql_password:
            raise HTTPException(400, "mysql_password required for test")
        cfg = _payload_to_cfg(payload)
        r = probe_mysql(cfg)
        return {"result": "ok" if r.ok else "failed", "detail": r.detail}

    # =====================================================================
    # /run — enqueue a single date
    # =====================================================================

    @app.post("/run")
    async def post_run(
        body: RunRequest,
        request: Request,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> JSONResponse:
        store = _store(app)
        cfg = store.load()
        if cfg is None:
            raise HTTPException(409, "unconfigured")

        today = _today_local(cfg)

        if body.report_date is None:
            report_date = today - timedelta(days=1)
        else:
            try:
                report_date = datetime.strptime(body.report_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(400, "report_date must be YYYY-MM-DD")

        if report_date >= today:
            raise HTTPException(
                400,
                f"report_date {report_date} is today or future; "
                "manual runs only support past dates (spec L18)",
            )

        cap = int(os.environ.get("BACKFILL_SAFETY_CAP_DAYS", "365"))
        if (today - report_date).days > cap:
            raise HTTPException(
                400,
                f"report_date older than {cap}-day safety cap",
            )

        # Manual = not the daily scheduler. /run is always manual here.
        result = _orch(app).try_enqueue(report_date, manual=True)
        if not result.accepted:
            raise HTTPException(409, f"run not accepted: {result.reason}")
        return JSONResponse(
            status_code=202,
            content={
                "report_date": str(report_date),
                "enqueued": True,
                "queue_depth": result.queue_depth,
            },
        )

    # =====================================================================
    # /run/catchup — enqueue every missing day
    # =====================================================================

    @app.post("/run/catchup")
    async def post_catchup(
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> JSONResponse:
        store = _store(app)
        cfg = store.load()
        if cfg is None:
            raise HTTPException(409, "unconfigured")
        today = _today_local(cfg)
        enqueued, skipped = _orch(app).catchup_missing(
            today_local=today, cap_days=cfg.catch_up_cap_days,
        )
        return JSONResponse(status_code=202, content={
            "enqueued": [d.isoformat() for d in enqueued],
            "skipped_already_queued": [d.isoformat() for d in skipped],
        })

    # =====================================================================
    # /runs — paginated list
    # =====================================================================

    @app.get("/runs")
    def get_runs(
        request: Request,
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
        _: str = Depends(require_basic_auth),
    ) -> Any:
        from .db import MysqlConfig, connect
        cfg = _store(app).load()
        if cfg is None:
            raise HTTPException(409, "unconfigured")
        with connect(MysqlConfig(
            host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
            password=cfg.mysql_password, database=cfg.mysql_database,
        )) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, source, report_date, started_at, finished_at,
                           status, rows_in_csv, rows_inserted, rows_duplicate,
                           manual, error_excerpt
                    FROM z_audit_logs_efk_runs
                    ORDER BY started_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = list(cur.fetchall())

        if "application/json" in request.headers.get("accept", ""):
            return [_serialize_run(r) for r in rows]
        return HTMLResponse(_render_runs_html(cfg, rows, limit, offset))

    @app.get("/runs/{run_id}")
    def get_run(
        run_id: int,
        _: str = Depends(require_basic_auth),
    ) -> dict:
        from .db import MysqlConfig, connect
        cfg = _store(app).load()
        if cfg is None:
            raise HTTPException(409, "unconfigured")
        with connect(MysqlConfig(
            host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
            password=cfg.mysql_password, database=cfg.mysql_database,
        )) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM z_audit_logs_efk_runs WHERE id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "run not found")
        return _serialize_run(row)

    # =====================================================================
    # / — dashboard
    # =====================================================================

    @app.get("/", response_class=HTMLResponse)
    def index(_: str = Depends(require_basic_auth)) -> HTMLResponse:
        cfg = _store(app).load()
        return HTMLResponse(_render_dashboard_html(cfg, _orch(app).state()))


# ---------------------------------------------------------------------------
# Inline templates (keep small — operators want function over fashion)
# ---------------------------------------------------------------------------


def _esc(s: object) -> str:
    import html
    return html.escape(str(s) if s is not None else "")


def _render_dashboard_html(cfg: Optional[Config], state: dict) -> str:
    if cfg is None:
        return (
            "<!doctype html><meta charset=utf-8><title>bluewave-worker</title>"
            "<h1>bluewave-worker</h1>"
            "<p>Container is <b>unconfigured</b>. "
            "<a href='/config'>Open /config</a> to set up.</p>"
        )
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{_esc(cfg.site_label)} — bluewave-worker</title>"
        f"<h1>{_esc(cfg.site_label)}</h1>"
        f"<p>Operator TZ: <code>{_esc(cfg.operator_timezone)}</code> | "
        f"Schedule: <code>{_esc(cfg.schedule_local)} local</code> | "
        f"Queue depth: <code>{state['queue_depth']}</code> | "
        f"In flight: <code>{_esc(state['in_flight'])}</code></p>"
        "<p><a href='/runs'>Run history</a> &middot; "
        "<a href='/config'>Configure</a></p>"
    )


def _render_config_html(cfg: Optional[Config], csrf_token: str) -> str:
    def v(field: str) -> str:
        return _esc(getattr(cfg, field, "")) if cfg else ""
    pw_placeholder = "••••••• (set)" if cfg else ""
    return f"""<!doctype html><meta charset=utf-8><title>Config — bluewave-worker</title>
<h1>Configure</h1>
<form id=cfg>
<input type=hidden name=csrf_token value="{_esc(csrf_token)}">
<p>Site label: <input name=site_label value="{v('site_label')}" required></p>
<p>BlueWeb URL: <input name=blueweb_url value="{v('blueweb_url')}" required></p>
<p>BlueWeb user: <input name=blueweb_user value="{v('blueweb_user')}" required></p>
<p>BlueWeb password: <input type=password name=blueweb_password placeholder="{_esc(pw_placeholder)}"></p>
<p>Operator timezone (IANA): <input name=operator_timezone value="{v('operator_timezone')}" required></p>
<p>MySQL host: <input name=mysql_host value="{v('mysql_host')}" required></p>
<p>MySQL port: <input name=mysql_port type=number value="{v('mysql_port') or '3306'}" required></p>
<p>MySQL database: <input name=mysql_database value="{v('mysql_database')}" required></p>
<p>MySQL user: <input name=mysql_user value="{v('mysql_user')}" required></p>
<p>MySQL password: <input type=password name=mysql_password placeholder="{_esc(pw_placeholder)}"></p>
<p>Schedule (HH:MM local): <input name=schedule_local value="{v('schedule_local') or '03:00'}" required></p>
<p>Catch-up cap (days): <input type=number name=catch_up_cap_days value="{v('catch_up_cap_days') or 14}" required></p>
<p><button type=submit>Save</button></p>
</form>
<p><a href='/'>Back to dashboard</a></p>
"""


def _render_runs_html(cfg: Config, rows: list[dict], limit: int, offset: int) -> str:
    body = ["<!doctype html><meta charset=utf-8><title>Runs — bluewave-worker</title>",
            f"<h1>{_esc(cfg.site_label)} — runs</h1>",
            "<table border=1 cellspacing=0 cellpadding=4>",
            "<tr><th>id</th><th>date</th><th>started_at (UTC)</th>"
            "<th>finished_at (UTC)</th><th>status</th><th>rows_in</th>"
            "<th>inserted</th><th>duplicate</th><th>manual</th></tr>"]
    for r in rows:
        body.append(
            "<tr>"
            f"<td><a href='/runs/{_esc(r['id'])}'>{_esc(r['id'])}</a></td>"
            f"<td>{_esc(r['report_date'])}</td>"
            f"<td>{_esc(r['started_at'])}</td>"
            f"<td>{_esc(r.get('finished_at'))}</td>"
            f"<td>{_esc(r['status'])}</td>"
            f"<td>{_esc(r.get('rows_in_csv'))}</td>"
            f"<td>{_esc(r.get('rows_inserted'))}</td>"
            f"<td>{_esc(r.get('rows_duplicate'))}</td>"
            f"<td>{_esc(r.get('manual'))}</td>"
            "</tr>"
        )
    body.append("</table>")
    prev_off = max(0, offset - limit)
    next_off = offset + limit
    body.append(
        f"<p><a href='/runs?limit={limit}&offset={prev_off}'>prev</a> &middot; "
        f"<a href='/runs?limit={limit}&offset={next_off}'>next</a></p>"
    )
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Helpers exposed for testability
# ---------------------------------------------------------------------------


def _payload_to_cfg(p: ConfigPayload) -> Config:
    return Config(
        site_label=p.site_label,
        blueweb_url=p.blueweb_url,
        blueweb_user=p.blueweb_user,
        blueweb_password=p.blueweb_password,
        operator_timezone=p.operator_timezone,
        mysql_host=p.mysql_host,
        mysql_port=p.mysql_port,
        mysql_database=p.mysql_database,
        mysql_user=p.mysql_user,
        mysql_password=p.mysql_password,
        schedule_local=p.schedule_local,
        catch_up_cap_days=p.catch_up_cap_days,
    )


def _serialize_run(row: dict) -> dict:
    """Convert DB row dict to JSON-safe shape."""
    out = dict(row)
    for k in ("started_at", "finished_at", "report_date"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    if "manual" in out and out["manual"] is not None:
        out["manual"] = bool(out["manual"])
    return out
