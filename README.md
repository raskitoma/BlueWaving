# bluewave-worker

Dockerized headless Selenium worker that pulls the daily **BlueWeb v20 Event
report** and writes the rows into the shared MySQL audit table
`z_audit_logs_efk`.

Full design doc: [`BLUEWEB_AUDIT_INGEST_SPEC.md`](./BLUEWEB_AUDIT_INGEST_SPEC.md)

---

## Deploy

```bash
./deploy.sh
```

The wizard walks five steps:

1. **Prerequisites** — confirms Docker + Compose v2 + curl + a reachable daemon
2. **Build** — `docker build -t bluewave-worker:dev .`
3. **Configure** — eight prompts, one per env var:
   1. `CONFIG_ENC_KEYS` — Fernet key (auto-generated if new)
   2. `WEB_USER` — operator username (default `admin`)
   3. `WEB_PASS_HASH` — operator password (entered twice, bcrypt-hashed in-container)
   4. `WEB_ALLOW_HTTP` — forced to `1` (spec L13)
   5. `TZ` — forced to `UTC` (spec L17)
   6. `LOG_LEVEL` — `DEBUG`/`INFO`/`WARNING`/`ERROR`
   7. `CATCH_UP_CAP_DAYS` — max boot-time catch-up window
   8. `BACKFILL_SAFETY_CAP_DAYS` — max age for a manual backfill
4. **Write `.env`** — mode 600; existing file backed up with a timestamp suffix
5. **Start** — `docker compose up -d`, poll `/healthz` for up to 60 s

**Re-running is safe.** Each prompt shows the existing value (passwords as
`prefix…suffix`, keys as `AAAA***ZZZZ (44 chars)`); press Enter to keep, type
to change. To start over, delete `.env` first.

After the script prints `/healthz status=unconfigured`, open
**http://localhost:8080/config**, log in, and fill in BlueWeb / MySQL /
timezone / schedule via the web UI.

### Unattended first deploy

```bash
WEB_USER=admin WEB_PASS='my-strong-pass' ./deploy.sh
```

`CONFIG_ENC_KEYS` is always auto-generated on first run; rotation is a
separate procedure (spec §13.8 via `python -m bluewave.rotate_config`).

---

## Day-to-day

| Action | Command |
|---|---|
| Check status | `curl -s http://localhost:8080/healthz \| jq .` |
| View logs | `docker compose logs -f bluewave-worker` |
| Restart | `docker compose restart bluewave-worker` |
| Upgrade image | `docker compose pull && docker compose up -d` |
| Stop | `docker compose down` |
| Rotate config key | see [`§13.8`](./BLUEWEB_AUDIT_INGEST_SPEC.md) of the spec |

The web UI at **http://localhost:8080/** shows:

- last 20 runs with status + row counts
- "Run now" button (yesterday)
- "Backfill" button (any past date)
- "Catch up missing" button (enqueues every missing day since the last `ok`)

---

## What the worker does

Every day at the configured local time:

1. Headless Chromium logs into BlueWeb
2. Selects the Event report for yesterday's date
3. Downloads the CSV
4. Transforms each row → `(timestamp, source='Bluewave', operation, instance, user_name, user_id, extra_data, dedup_hash)`
5. Inserts into `z_audit_logs_efk` (idempotent via `dedup_hash`)
6. Records the run in `z_audit_logs_efk_runs`
7. Deletes the local CSV

If the container is offline when a scheduled run should fire, **boot-time
catch-up** enqueues every missing day on next start (cap: 14 days).

---

## Status

All milestones complete. 179 unit tests passing, 19 gated by Docker / live
BlueWeb (auto-skip when the resource isn't available).

- [x] M1 — container + `/healthz` + `keygen` / `hashpw`
- [x] M2 — MySQL schema bootstrap + drift detection
- [x] M3 — Selenium login + selector catalog + denylist
- [x] M4 — Event report → CSV download
- [x] M5 — CSV → typed rows (DST handling, golden fixture)
- [x] M6 — idempotent insert + run lifecycle
- [x] M7 — web GUI: config / run / runs / catchup
- [x] M8 — APScheduler daily fire + catch-up
- [x] M9 — key rotation CLI + screenshot GC + JSON logs

---

## Running tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

Tests that need extra resources auto-skip with a clear reason:

| Test family | Requires |
|---|---|
| `test_m2_schema_integration` & `test_m6_sink_runs` (real MySQL via testcontainers) | Docker daemon reachable |
| `test_m3_smoke` (live BlueWeb login) | `BLUEWAVE_SMOKE_URL` / `_USER` / `_PASS` env vars |

---

## Project layout

```
.
├── deploy.sh                  # first-time deploy automation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt           # runtime deps
├── requirements-dev.txt       # + pytest, httpx, testcontainers
├── pyproject.toml             # pytest config
├── BLUEWEB_AUDIT_INGEST_SPEC.md
├── bluewave/                  # ~3.7k LOC production
└── tests/                     # ~3.2k LOC tests + golden CSV
```

For module-level detail see the spec §4.3 + §10 (milestones).
