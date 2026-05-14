# bluewave-worker

Dockerized headless Selenium worker that pulls the daily **BlueWeb v20 Event
report** and writes the rows into the shared MySQL audit table
`z_audit_logs_efk`.

Full design doc: [`BLUEWEB_AUDIT_INGEST_SPEC.md`](./BLUEWEB_AUDIT_INGEST_SPEC.md)

---

## Deploy (first time)

```bash
./deploy.sh
```

That's it. The script will:

1. Check Docker is installed and running
2. Build the image (`bluewave-worker:dev`)
3. Generate `.env` with a fresh encryption key + your operator password hash
4. Bring up the container
5. Wait for `/healthz` to respond

Then open **http://localhost:8080/config** in a browser, log in with the
username/password you just entered, and fill in BlueWeb / MySQL / timezone /
schedule. Click **Save** — the daily cron registers and boot-time catch-up
runs automatically.

To regenerate the `.env` (rotate encryption key + operator password):

```bash
FORCE_REGEN=1 ./deploy.sh
```

To run unattended in scripts:

```bash
WEB_USER=admin WEB_PASS='my-strong-pass' ./deploy.sh
```

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
