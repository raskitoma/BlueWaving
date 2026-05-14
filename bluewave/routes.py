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


# Slim payloads for the per-probe test endpoints — they only require the
# subset of fields each probe actually uses, so the operator can click
# `Test BlueWeb` after filling in just the BlueWeb section, without yet
# having entered MySQL / schedule fields.
class BlueWebTestPayload(BaseModel):
    blueweb_url: str = Field(min_length=1)
    blueweb_user: str = Field(min_length=1)
    blueweb_password: str = Field(min_length=1)


class MysqlTestPayload(BaseModel):
    mysql_host: str = Field(min_length=1)
    mysql_port: int = Field(ge=1, le=65535, default=3306)
    mysql_database: str = Field(min_length=1)
    mysql_user: str = Field(min_length=1)
    mysql_password: str = Field(min_length=1)


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
        payload: BlueWebTestPayload,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> dict:
        r = probe_bluewave_login(
            url=payload.blueweb_url,
            user=payload.blueweb_user,
            password=payload.blueweb_password,
        )
        return {"result": "ok" if r.ok else "failed", "detail": r.detail}

    @app.post("/config/test/mysql")
    async def test_mysql(
        payload: MysqlTestPayload,
        _: str = Depends(require_basic_auth),
        __: None = Depends(require_csrf),
    ) -> dict:
        r = probe_mysql(
            host=payload.mysql_host,
            port=payload.mysql_port,
            database=payload.mysql_database,
            user=payload.mysql_user,
            password=payload.mysql_password,
        )
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
        from .db import MysqlConfig, connect as mysql_connect

        cfg = _store(app).load()
        if cfg is None:
            return HTMLResponse(
                _render_dashboard_html(None, {"queue_depth": 0, "in_flight": None})
            )

        # Best-effort fetch of the last 10 runs for the dashboard. If MySQL
        # is unreachable we still render the page (better than 500'ing).
        recent_runs: list[dict] = []
        try:
            with mysql_connect(MysqlConfig(
                host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
                password=cfg.mysql_password, database=cfg.mysql_database,
            )) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, source, report_date, started_at, finished_at,
                               status, rows_in_csv, rows_inserted, rows_duplicate,
                               manual
                        FROM z_audit_logs_efk_runs
                        ORDER BY started_at DESC
                        LIMIT 10
                        """
                    )
                    recent_runs = [_serialize_run(r) for r in cur.fetchall()]
        except Exception:
            # Surface the issue in the banner area instead of crashing.
            pass

        token = issue_csrf_token()
        return HTMLResponse(
            _render_dashboard_html(
                cfg, _orch(app).state(),
                recent_runs=recent_runs, csrf_token=token,
            )
        )


# ---------------------------------------------------------------------------
# Templates — inline so there's no separate static-files surface to host.
# Operator-facing, deliberately minimal styling. Spec §9.2.1 — all rendered
# timestamps are `YYYY-MM-DD HH:MM:SS UTC`.
# ---------------------------------------------------------------------------


def _esc(s: object) -> str:
    import html
    return html.escape(str(s) if s is not None else "")


_BASE_CSS = """
*,*::before,*::after { box-sizing: border-box; }
body { font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background: #f3f4f6; color: #1f2937; margin: 0; line-height: 1.45; }
header { background: #1e3a8a; color: #fff; padding: 1.1rem 1.6rem;
         box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
header h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
header .meta { font-size: 0.85rem; margin-top: 0.3rem; opacity: 0.9; }
main { max-width: 880px; margin: 1.5rem auto; padding: 1.6rem;
       background: #fff; border-radius: 6px;
       box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
h2 { margin-top: 0; font-size: 1.15rem; color: #111827; }
h2:not(:first-child) { margin-top: 2rem; border-top: 1px solid #e5e7eb;
                       padding-top: 1.2rem; }
nav { margin-bottom: 1rem; }
nav a { color: #1e3a8a; text-decoration: none; margin-right: 1rem;
        font-weight: 500; }
nav a:hover { text-decoration: underline; }
.muted { color: #6b7280; font-size: 0.88rem; }
.banner { padding: 0.85rem 1rem; border-radius: 5px; margin-bottom: 1rem;
          background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
.field { display: grid; grid-template-columns: 220px 1fr; gap: 0.9rem;
         align-items: center; margin-bottom: 0.75rem; }
.field label { font-weight: 500; color: #374151; font-size: 0.92rem; }
.field input { width: 100%; padding: 0.5rem 0.65rem; border: 1px solid #d1d5db;
               border-radius: 4px; font-size: 0.95rem; font-family: inherit; }
.field input:focus { outline: 2px solid #93c5fd; outline-offset: -1px;
                     border-color: #3b82f6; }
.button-row { display: flex; gap: 0.6rem; margin-top: 1.5rem;
              flex-wrap: wrap; }
button { padding: 0.5rem 1.15rem; border: 1px solid transparent;
         border-radius: 4px; cursor: pointer; font-size: 0.95rem;
         font-weight: 500; font-family: inherit; }
button.primary { background: #1e3a8a; color: #fff; }
button.primary:hover { background: #1e40af; }
button.secondary { background: #e5e7eb; color: #374151;
                   border-color: #d1d5db; }
button.secondary:hover { background: #d1d5db; }
button:disabled { opacity: 0.6; cursor: not-allowed; }
.status { margin-top: 1.25rem; padding: 0.85rem 1rem; border-radius: 5px;
          display: none; font-size: 0.93rem; }
.status.show { display: block; }
.status.ok { background: #d1fae5; color: #065f46; border: 1px solid #6ee7b7; }
.status.fail { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
.status.pending { background: #fef3c7; color: #92400e;
                  border: 1px solid #fde68a; }
.status ul { margin: 0.5rem 0 0; padding-left: 1.5rem; }
.status pre { background: rgba(0,0,0,0.08); padding: 0.5rem;
              border-radius: 3px; overflow-x: auto; font-size: 0.85rem; }
table.runs { width: 100%; border-collapse: collapse; margin-top: 0.5rem;
             font-size: 0.9rem; }
table.runs th, table.runs td { padding: 0.5rem 0.6rem;
                                border-bottom: 1px solid #e5e7eb;
                                text-align: left; vertical-align: top; }
table.runs th { background: #f9fafb; font-weight: 600; color: #374151; }
table.runs tr.ok td.status-cell { color: #047857; font-weight: 500; }
table.runs tr.fail td.status-cell { color: #b91c1c; font-weight: 500; }
table.runs tr.running td.status-cell { color: #d97706; font-weight: 500; }
table.runs a { color: #1e3a8a; text-decoration: none; }
table.runs a:hover { text-decoration: underline; }
.footer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #e5e7eb;
          font-size: 0.85rem; color: #6b7280; display: flex;
          justify-content: space-between; }
code { background: #f3f4f6; padding: 0.1rem 0.35rem; border-radius: 3px;
       font-size: 0.88em; }
""".strip()


# Vanilla-JS form submission with CSRF header. No external deps.
#
# Also includes a localStorage "draft" feature: every non-password field
# auto-saves on input change, and is restored on page load into any field
# the server didn't already pre-populate. Passwords are deliberately
# excluded from localStorage — they get re-entered each session.
_CONFIG_JS = r"""
(function() {
  const form = document.getElementById('cfg');
  const status = document.getElementById('status');
  const csrfToken = form.querySelector('input[name=csrf_token]').value;
  const NUMERIC = new Set(['mysql_port', 'catch_up_cap_days']);
  const SENSITIVE = new Set(['blueweb_password', 'mysql_password', 'csrf_token']);
  const DRAFT_KEY = 'bluewave.cfg.draft.v1';

  // ----- localStorage draft ------------------------------------------------

  function saveDraft() {
    const fd = new FormData(form);
    const draft = {};
    for (const [k, v] of fd.entries()) {
      if (SENSITIVE.has(k)) continue;
      if (v !== '' && v !== null) draft[k] = v;
    }
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch (e) {}
  }
  function restoreDraft() {
    let draft;
    try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || '{}'); }
    catch (e) { return false; }
    let restored = 0;
    for (const [k, v] of Object.entries(draft || {})) {
      if (SENSITIVE.has(k)) continue;
      const input = form.querySelector('[name="' + k + '"]');
      if (input && !input.value) { input.value = v; restored++; }
    }
    return restored > 0;
  }
  function clearDraft() {
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
    const ind = document.getElementById('draft-indicator');
    if (ind) ind.remove();
  }
  function showDraftIndicator() {
    if (document.getElementById('draft-indicator')) return;
    const note = document.createElement('div');
    note.id = 'draft-indicator';
    note.className = 'muted';
    note.style.marginBottom = '0.75rem';
    note.innerHTML =
      '↻ Restored from local draft. ' +
      '<a href="#" id="draft-discard">Discard draft</a> · ' +
      '<span class="muted">(passwords are never stored)</span>';
    form.parentNode.insertBefore(note, form);
    document.getElementById('draft-discard').addEventListener('click', (ev) => {
      ev.preventDefault();
      if (!confirm('Discard local draft? The form will reset to server-saved values.')) return;
      clearDraft();
      location.reload();
    });
  }

  // Restore on load.
  if (restoreDraft()) showDraftIndicator();

  // Save on every input change.
  form.addEventListener('input', saveDraft);

  // ----- submit + test helpers --------------------------------------------

  function payloadFromForm() {
    const fd = new FormData(form);
    fd.delete('csrf_token');
    const obj = {};
    for (const [k, v] of fd.entries()) {
      obj[k] = NUMERIC.has(k) ? parseInt(v, 10) : v;
    }
    return obj;
  }
  function show(cls, html) {
    status.className = 'status show ' + cls;
    status.innerHTML = html;
    status.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
  function esc(s) {
    const d = document.createElement('div'); d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }
  function probes(list) {
    if (!list || !list.length) return '';
    return '<ul>' + list.map(p =>
      '<li><strong>' + esc(p.probe) + '</strong> ' +
      (p.ok ? '✓ ' : '✗ ') + esc(p.detail) + '</li>'
    ).join('') + '</ul>';
  }
  async function postJson(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
        body: JSON.stringify(body)
      });
      const data = await r.json().catch(() => ({}));
      return {resp: r, data};
    } catch (e) { return {resp: null, data: {error: e.message}}; }
  }
  function disable(state) {
    form.querySelectorAll('button').forEach(b => b.disabled = state);
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    disable(true);
    show('pending', 'Running triple-probe (BlueWeb HEAD &rarr; MySQL SELECT 1 &rarr; Selenium login)...');
    const {resp, data} = await postJson('/config', payloadFromForm());
    disable(false);
    if (!resp) { show('fail', '<strong>Network error.</strong>'); return; }
    if (resp.ok && data.saved) {
      clearDraft();  // server now has the canonical values
      show('ok', '<strong>Saved.</strong>' + probes(data.probes) +
        ' <a href="/">&larr; Back to dashboard</a>');
    } else if (data.probes) {
      show('fail', '<strong>Not saved — a probe failed:</strong>' + probes(data.probes) +
        '<p class="muted">Your entries are kept locally. Fix the failing probe and try again.</p>');
    } else {
      show('fail', '<strong>Validation error.</strong><pre>' +
        esc(JSON.stringify(data, null, 2)) + '</pre>');
    }
  });

  // Each test endpoint only requires its own fields. Build a slim payload
  // so unfilled fields elsewhere on the form don't trigger 422.
  async function runTest(button, url, requiredFields) {
    const full = payloadFromForm();
    const slim = {};
    const missing = [];
    for (const f of requiredFields) {
      const v = full[f];
      if (v === undefined || v === null || v === '') {
        missing.push(f);
      } else {
        slim[f] = v;
      }
    }
    if (missing.length) {
      show('fail', 'Fill in <strong>' + missing.map(esc).join(', ') +
        '</strong> before testing.');
      return;
    }
    disable(true);
    show('pending', 'Testing ' + esc(button.textContent) + '...');
    const {resp, data} = await postJson(url, slim);
    disable(false);
    if (resp && resp.ok && data.result === 'ok') {
      show('ok', '<strong>' + esc(button.textContent) + ' OK.</strong> ' + esc(data.detail));
    } else {
      const msg = (data && (data.detail || data.error)) || 'unknown error';
      show('fail', '<strong>' + esc(button.textContent) + ' failed.</strong> ' + esc(msg));
    }
  }
  document.getElementById('test-bluewave').addEventListener('click', function() {
    runTest(this, '/config/test/bluewave',
            ['blueweb_url', 'blueweb_user', 'blueweb_password']);
  });
  document.getElementById('test-mysql').addEventListener('click', function() {
    runTest(this, '/config/test/mysql',
            ['mysql_host', 'mysql_port', 'mysql_database',
             'mysql_user', 'mysql_password']);
  });
})();
""".strip()


_DASHBOARD_JS = r"""
(function() {
  const status = document.getElementById('status');
  const tokenEl = document.querySelector('meta[name=csrf-token]');
  if (!tokenEl) return;
  const csrfToken = tokenEl.content;

  function show(cls, html) {
    status.className = 'status show ' + cls;
    status.innerHTML = html;
  }
  function esc(s) {
    const d = document.createElement('div'); d.textContent = String(s == null ? '' : s);
    return d.innerHTML;
  }
  async function postJson(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
        body: body == null ? null : JSON.stringify(body)
      });
      const data = await r.json().catch(() => ({}));
      return {resp: r, data};
    } catch (e) { return {resp: null, data: {error: e.message}}; }
  }

  const runNow = document.getElementById('run-now');
  if (runNow) runNow.addEventListener('click', async () => {
    show('pending', 'Enqueueing yesterday’s run...');
    const {resp, data} = await postJson('/run', {report_date: null});
    if (resp && resp.ok) {
      show('ok', '<strong>Enqueued.</strong> Queue depth: <code>' +
        esc(data.queue_depth) + '</code>. Refresh to see progress.');
    } else {
      show('fail', '<strong>Refused.</strong> ' + esc((data && data.detail) || 'unknown'));
    }
  });

  const catchup = document.getElementById('catchup');
  if (catchup) catchup.addEventListener('click', async () => {
    show('pending', 'Computing missing days...');
    const {resp, data} = await postJson('/run/catchup', null);
    if (resp && resp.ok) {
      const e = data.enqueued || [];
      const s = data.skipped_already_queued || [];
      let html = '<strong>Enqueued ' + e.length + ' day(s).</strong>';
      if (e.length) html += '<ul>' + e.map(d => '<li>' + esc(d) + '</li>').join('') + '</ul>';
      if (s.length) html += '<p class="muted">Already queued/in-flight: ' +
        s.map(esc).join(', ') + '</p>';
      show('ok', html);
    } else {
      show('fail', '<strong>Refused.</strong> ' + esc((data && data.detail) || 'unknown'));
    }
  });
})();
""".strip()


def _page_shell(title: str, body_html: str, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_BASE_CSS}</style>
{head_extra}
</head>
<body>
{body_html}
</body>
</html>"""


def _render_dashboard_html(
    cfg: Optional[Config],
    state: dict,
    recent_runs: list[dict] | None = None,
    csrf_token: str = "",
    healthz_status: str = "",
) -> str:
    if cfg is None:
        body = (
            "<header><h1>bluewave-worker</h1>"
            "<div class='meta'>Setup required</div></header>"
            "<main>"
            "<div class='banner'>Container is <strong>unconfigured</strong>. "
            "Open <a href='/config'>/config</a> to enter BlueWeb / MySQL / "
            "schedule.</div>"
            "</main>"
        )
        return _page_shell("Setup — bluewave-worker", body)

    queue_depth = state.get("queue_depth", 0)
    in_flight = state.get("in_flight")
    in_flight_html = _esc(in_flight) if in_flight else "<span class='muted'>idle</span>"
    status_pill = ""
    if healthz_status:
        status_pill = f" &middot; status <code>{_esc(healthz_status)}</code>"

    head_extra = f"<meta name='csrf-token' content='{_esc(csrf_token)}'>"

    rows_html: list[str] = []
    if recent_runs:
        rows_html.append(
            "<table class='runs'>"
            "<thead><tr>"
            "<th>id</th><th>report_date</th><th>started (UTC)</th>"
            "<th>finished (UTC)</th><th>status</th>"
            "<th>rows in</th><th>inserted</th><th>dup</th><th>manual</th>"
            "</tr></thead><tbody>"
        )
        for r in recent_runs:
            css = "ok" if r.get("status") == "ok" else (
                "running" if r.get("status") == "running" else "fail"
            )
            rows_html.append(
                f"<tr class='{css}'>"
                f"<td><a href='/runs/{_esc(r['id'])}'>#{_esc(r['id'])}</a></td>"
                f"<td>{_esc(r.get('report_date'))}</td>"
                f"<td>{_esc(r.get('started_at'))}</td>"
                f"<td>{_esc(r.get('finished_at'))}</td>"
                f"<td class='status-cell'>{_esc(r.get('status'))}</td>"
                f"<td>{_esc(r.get('rows_in_csv'))}</td>"
                f"<td>{_esc(r.get('rows_inserted'))}</td>"
                f"<td>{_esc(r.get('rows_duplicate'))}</td>"
                f"<td>{'yes' if r.get('manual') else 'no'}</td>"
                f"</tr>"
            )
        rows_html.append("</tbody></table>")
    else:
        rows_html.append("<p class='muted'>No runs yet.</p>")

    body = f"""<header>
<h1>{_esc(cfg.site_label)}</h1>
<div class='meta'>BlueWeb audit ingest worker &middot;
 TZ <code>{_esc(cfg.operator_timezone)}</code> &middot;
 schedule <code>{_esc(cfg.schedule_local)} local</code> &middot;
 queue <code>{queue_depth}</code> &middot;
 in flight {in_flight_html}{status_pill}</div>
</header>
<main>
<nav>
<a href='/runs'>Full run history</a>
<a href='/config'>Configure</a>
</nav>

<h2>Actions</h2>
<div class='button-row'>
<button id='run-now' class='primary' type='button'>Run now (yesterday)</button>
<button id='catchup' class='secondary' type='button'>Catch up missing</button>
</div>
<div id='status' class='status'></div>

<h2>Recent runs</h2>
{''.join(rows_html)}
</main>
<script>{_DASHBOARD_JS}</script>
"""
    return _page_shell(f"{cfg.site_label} — bluewave-worker", body, head_extra)


def _render_config_html(cfg: Optional[Config], csrf_token: str) -> str:
    def v(field: str, default: str = "") -> str:
        return _esc(getattr(cfg, field, default) if cfg else default)
    has_blueweb_pw = bool(cfg)
    has_mysql_pw = bool(cfg)
    bw_pw_placeholder = "••••••• (set, leave blank to keep)" if has_blueweb_pw else ""
    db_pw_placeholder = "••••••• (set, leave blank to keep)" if has_mysql_pw else ""

    sl_value = v('schedule_local') or "03:00"
    cap_value = (str(cfg.catch_up_cap_days) if cfg else "14")
    port_value = (str(cfg.mysql_port) if cfg else "3306")

    site_banner = ""
    if cfg is None:
        site_banner = (
            "<div class='banner'>First-time setup. "
            "Fill all required fields, then <strong>Test</strong> and "
            "<strong>Save</strong>. Probes (BlueWeb HEAD &rarr; MySQL &rarr; "
            "Selenium login) run before any data is persisted.</div>"
        )

    body = f"""<header>
<h1>Configure</h1>
<div class='meta'>{_esc(cfg.site_label) if cfg else 'Initial setup'}</div>
</header>
<main>
{site_banner}
<nav>
<a href='/'>&larr; Dashboard</a>
</nav>

<form id='cfg' autocomplete='off'>
<input type='hidden' name='csrf_token' value='{_esc(csrf_token)}'>

<h2>Site</h2>
<div class='field'><label>Site label</label>
<input name='site_label' value='{v('site_label')}' required
       placeholder='e.g. Easy Foods Inc.  (shown on dashboard header)'></div>

<h2>BlueWeb</h2>
<div class='field'><label>URL</label>
<input name='blueweb_url' value='{v('blueweb_url')}' required
       placeholder='e.g. http://blueweb.lan  or  http://10.102.1.50:8080'></div>
<div class='field'><label>Username</label>
<input name='blueweb_user' value='{v('blueweb_user')}' required
       placeholder='e.g. Administrator'></div>
<div class='field'><label>Password</label>
<input name='blueweb_password' type='password'
       placeholder='{_esc(bw_pw_placeholder) or "BlueWeb account password"}'></div>
<div class='field'><label>Operator timezone (IANA)</label>
<input name='operator_timezone' value='{v('operator_timezone')}' required
       placeholder='e.g. America/New_York, America/Chicago, Europe/Madrid'></div>

<h2>MySQL (target audit DB)</h2>
<div class='field'><label>Host</label>
<input name='mysql_host' value='{v('mysql_host')}' required
       placeholder='e.g. mysql.internal.example  or  10.102.1.20'></div>
<div class='field'><label>Port</label>
<input name='mysql_port' type='number' value='{_esc(port_value)}' required
       min='1' max='65535' placeholder='3306'></div>
<div class='field'><label>Database</label>
<input name='mysql_database' value='{v('mysql_database')}' required
       placeholder='e.g. audit  (the schema, not the table)'></div>
<div class='field'><label>User</label>
<input name='mysql_user' value='{v('mysql_user')}' required
       placeholder='e.g. audit_writer  (needs CREATE+INSERT+SELECT on first boot)'></div>
<div class='field'><label>Password</label>
<input name='mysql_password' type='password'
       placeholder='{_esc(db_pw_placeholder) or "DB user password"}'></div>

<h2>Schedule</h2>
<div class='field'><label>Daily fire (HH:MM, operator TZ)</label>
<input name='schedule_local' value='{_esc(sl_value)}' required
       pattern='[0-2][0-9]:[0-5][0-9]'
       placeholder='e.g. 03:00  (runs daily at 3:00 in the operator timezone)'></div>
<div class='field'><label>Catch-up cap (days)</label>
<input name='catch_up_cap_days' type='number' value='{_esc(cap_value)}' required
       min='1' max='90'
       placeholder='14  (boot-time catch-up backfills up to N missed days)'></div>

<div class='button-row'>
<button id='test-bluewave' type='button' class='secondary'>Test BlueWeb</button>
<button id='test-mysql' type='button' class='secondary'>Test MySQL</button>
<button type='submit' class='primary'>Save</button>
</div>
</form>

<div id='status' class='status'></div>

<div class='footer'>
<span>Passwords are write-only — leave blank to keep the existing value.</span>
<span>Spec: <code>BLUEWEB_AUDIT_INGEST_SPEC.md</code> &sect;7</span>
</div>
</main>
<script>{_CONFIG_JS}</script>
"""
    return _page_shell("Configure — bluewave-worker", body)


def _render_runs_html(cfg: Config, rows: list[dict], limit: int, offset: int) -> str:
    rows_html: list[str] = []
    rows_html.append(
        "<table class='runs'>"
        "<thead><tr>"
        "<th>id</th><th>report_date</th><th>started (UTC)</th>"
        "<th>finished (UTC)</th><th>status</th>"
        "<th>rows in</th><th>inserted</th><th>dup</th><th>manual</th>"
        "</tr></thead><tbody>"
    )
    for r in rows:
        css = "ok" if r.get("status") == "ok" else (
            "running" if r.get("status") == "running" else "fail"
        )
        rows_html.append(
            f"<tr class='{css}'>"
            f"<td><a href='/runs/{_esc(r['id'])}'>#{_esc(r['id'])}</a></td>"
            f"<td>{_esc(r.get('report_date'))}</td>"
            f"<td>{_esc(r.get('started_at'))}</td>"
            f"<td>{_esc(r.get('finished_at'))}</td>"
            f"<td class='status-cell'>{_esc(r.get('status'))}</td>"
            f"<td>{_esc(r.get('rows_in_csv'))}</td>"
            f"<td>{_esc(r.get('rows_inserted'))}</td>"
            f"<td>{_esc(r.get('rows_duplicate'))}</td>"
            f"<td>{'yes' if r.get('manual') else 'no'}</td>"
            f"</tr>"
        )
    rows_html.append("</tbody></table>")

    prev_off = max(0, offset - limit)
    next_off = offset + limit

    body = f"""<header>
<h1>{_esc(cfg.site_label)} — runs</h1>
<div class='meta'>showing {offset + 1}–{offset + len(rows)} (limit {limit})</div>
</header>
<main>
<nav><a href='/'>&larr; Dashboard</a> <a href='/config'>Configure</a></nav>
{''.join(rows_html)}
<div class='button-row'>
<a href='/runs?limit={limit}&offset={prev_off}'><button class='secondary' type='button'>&larr; Previous</button></a>
<a href='/runs?limit={limit}&offset={next_off}'><button class='secondary' type='button'>Next &rarr;</button></a>
</div>
</main>
"""
    return _page_shell(f"Runs — {cfg.site_label}", body)


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
