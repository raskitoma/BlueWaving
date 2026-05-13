# BlueWeb Audit Ingest Worker

Dockerized headless Selenium worker that scrapes a local BlueWeb v20
*Event* report once a day and writes the rows into a shared MySQL audit
table `z_audit_logs_efk`.

**Full spec:** [`BLUEWEB_AUDIT_INGEST_SPEC.md`](./BLUEWEB_AUDIT_INGEST_SPEC.md).
The spec is the source of truth; this README is a pointer + quickstart.

## Status

Implementation is staged by milestone. See spec §10.

- [x] **M1** — container skeleton + `/healthz` scaffold + `keygen` / `hashpw` CLIs
- [x] **M2** — MySQL schema bootstrap (DDL, validate, drift detection, idempotency)
- [x] **M3** — Selenium login + selector catalog (S1+S2, denylist enforcement)
- [x] **M4** — full Selenium flow + CSV capture (S3–S7, empty-report handling)
- [x] **M5** — CSV→Row transform (golden fixture, DST handling, dedup hash)
- [x] **M6** — idempotent DB writes + run lifecycle (start/finalize/reap)
- [x] **M7** — web GUI: config form, triple-probe, run/catchup/runs routes, Basic Auth + CSRF
- [x] **M8** — APScheduler daily fire + boot-time catch-up + reconfigure on save
- [x] **M9** — hardening: `rotate_config` CLI, screenshot retention GC, structured JSON logs
- [ ] **M4** — Full Selenium flow + CSV capture
- [ ] **M5** — CSV → rows transform
- [ ] **M6** — Idempotent DB writes
- [ ] **M7** — Web GUI
- [ ] **M8** — Scheduler + catch-up
- [ ] **M9** — Hardening + soak

## Quickstart (M1 only)

The container at M1 has no persistence layer yet, so `/healthz` always
returns 503 `unconfigured`. This is by design — it proves the boot path,
env validation, and HTTP surface work end-to-end.

```bash
# 1. Generate a Fernet key
docker run --rm -v "$PWD:/app" -w /app python:3.12-slim \
    sh -c 'pip install --quiet cryptography==43.0.0 && python -m bluewave.keygen'

# 2. Generate a bcrypt hash for the operator password
docker run --rm -it -v "$PWD:/app" -w /app python:3.12-slim \
    sh -c 'pip install --quiet bcrypt==4.2.0 && python -m bluewave.hashpw'

# 3. Copy .env.example to .env, paste the values from steps 1 & 2.
cp .env.example .env
$EDITOR .env

# 4. Build + run.
docker compose build
docker compose up -d

# 5. Verify.
curl -s http://localhost:8080/healthz | jq .
# Expect: status=503, body.status="unconfigured", body.reasons=["no_config"]
```

## Local development (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export CONFIG_ENC_KEYS=$(python -m bluewave.keygen)
export WEB_USER=admin
export WEB_PASS_HASH='$2b$12$stub'    # any non-empty value while M1 doesn't verify it
export WEB_ALLOW_HTTP=1

# Run.
python -m bluewave.web

# Or run tests.
pytest
```

## Layout

```
.
├── BLUEWEB_AUDIT_INGEST_SPEC.md   # design doc — source of truth
├── Dockerfile
├── docker-compose.yml
├── requirements.txt               # runtime deps
├── requirements-dev.txt           # + pytest, httpx
├── pyproject.toml                 # pytest config
├── .env.example
├── bluewave/                      # package
│   ├── __init__.py
│   ├── web.py                     # FastAPI app + /healthz
│   ├── keygen.py                  # Fernet key CLI
│   ├── hashpw.py                  # bcrypt hash CLI
│   ├── db.py                      # pymysql connection factory (kwargs-only)
│   ├── schema.py                  # DDL + bootstrap + drift validation
│   ├── exceptions.py              # RunFailure hierarchy keyed to §6.4.5
│   ├── selectors.py               # Selector catalog + DENYLIST
│   ├── driver.py                  # Chromium builder + SafeDriver
│   ├── login.py                   # S1+S2 + CLI smoke entrypoint
│   ├── scrape.py                  # S3–S7 (Event report → CSV)
│   ├── dedup.py                   # canonical_extra_data + dedup_hash
│   ├── transform.py               # CSV → Row[] with DST handling
│   ├── sink.py                    # idempotent batch INSERT
│   ├── runs.py                    # z_audit_logs_efk_runs lifecycle
│   ├── config.py                  # Fernet-encrypted SQLite config store
│   ├── auth.py                    # Basic Auth + CSRF
│   ├── probes.py                  # BlueWeb HEAD / MySQL / Selenium login
│   ├── routes.py                  # FastAPI routes + minimal HTML
│   ├── orchestrator.py            # in-process run queue + run_job()
│   ├── scheduler.py               # APScheduler daily fire + boot catch-up
│   ├── screenshots.py             # retention GC
│   ├── logging_setup.py           # structured JSON logs
│   └── rotate_config.py           # CLI for key rotation
└── tests/
    ├── test_m1_env.py
    ├── test_m1_healthz.py
    ├── test_m1_cli.py
    ├── test_m2_schema_unit.py        # runs everywhere
    ├── test_m2_schema_integration.py # skipped without Docker
    ├── test_m3_driver.py             # Chromium options + denylist
    ├── test_m3_selectors.py          # greppability + denylist content
    ├── test_m3_login.py              # status taxonomy (mocked)
    └── test_m3_smoke.py              # live BlueWeb, skipped without env
```

## Running integration tests

The M2 (+ later M4/M6/M8) integration tests need a real MySQL via
[testcontainers](https://testcontainers-python.readthedocs.io/).
With Docker running, just:

```bash
pytest                 # all tests (integration ones spin up MySQL on demand)
pytest -m "not skip"   # same effect; skipped tests stay skipped
```

Without Docker, the integration tests are skipped and pytest still passes —
the unit tests verify the pure validation logic, the DDL coherence,
and the connection-factory contract.

## Running the M3 live smoke test

Once Chromium and chromedriver are available locally (the container always
has them), point the smoke test at your live BlueWeb:

```bash
BLUEWAVE_SMOKE_URL=http://blueweb.lan \
BLUEWAVE_SMOKE_USER=admin \
BLUEWAVE_SMOKE_PASS=... \
    pytest tests/test_m3_smoke.py -v
```

Or run the CLI directly:

```bash
python -m bluewave.login http://blueweb.lan admin '<password>'
# Exits 0 on success and prints "ok status=login_ok screenshot=<path>"
# Exits 1 on failure with status= one of auth_failed / nav_failed
```

The first successful smoke run is also where you confirm the placeholder
selectors in `bluewave/selectors.py` actually match the live HTML. If any
selector fails, update `selectors.py` and re-run.
