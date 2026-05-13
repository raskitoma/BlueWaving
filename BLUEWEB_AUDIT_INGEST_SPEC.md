# BlueWeb → `z_audit_logs_efk` Ingest Worker — Technical Specification

**Status:** Draft v0.3 — Tier 1 hardening + selected Tier 2 features merged (TZ/DST handling, MultiFernet key rotation, self-audit writes, schema charset validation, future-date rejection, site label, test-connection buttons, dashboard pagination, OpenAPI `/docs`, SBOM-pinned image). Implementation may begin once the §11 residual decisions are signed off.
**Owner:** IT
**Shared table:** This worker is a *peer writer* into the shared audit table `z_audit_logs_efk`. Schema conventions adopted here remain compatible with any other module that writes to the same table.
**Target deliverable:** A single self-contained Linux Docker container that (a) serves a small operator-facing web GUI for one-time configuration, (b) runs a headless Selenium + Chromium worker on a daily schedule against a local *BlueWeb Access Control Administration v20* instance, downloads the Event report CSV for the previous calendar day, transforms each row to the common audit schema, and (c) idempotently inserts the resulting rows into the shared `z_audit_logs_efk` table on a remote MySQL server.

---

## 0. How to read this document

### 0.1 The Karpathy method

This spec follows the **Karpathy method** for LLM-assisted coding ([forrestchang/andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills)). Four binding principles:

| Principle | Operational rule for this project |
|---|---|
| **Think Before Coding** | Every assumption is named. Ambiguity surfaces in §11 — *not* silently in code. If a milestone's required behavior depends on a §11 decision, that milestone is blocked until the decision is recorded. |
| **Simplicity First** | One container. One scheduler. One report. One target table. No queue, no message bus, no plugin framework, no second hot/cold tier. A senior reviewer must not be able to call the result overcomplicated. |
| **Surgical Changes** | Each milestone touches only its own files. The shared table schema is defined once in M2 and never silently altered. Other modules write to `z_audit_logs_efk` too — this worker must not `ALTER` or `DROP` it after creation. |
| **Goal-Driven Execution** | Every milestone in §10 ends with **machine-verifiable pass criteria** — a `pytest` test, a `docker run` exit code, or a SQL query whose result is deterministic. A milestone is not "done" until those criteria hold. |

If during implementation a milestone's pass criteria appears unreachable, **stop and surface the conflict** rather than weakening the criteria.

### 0.2 Locked design decisions

These are the prior open questions, now resolved. Each row would have appeared in §11 in v0.1 of this document; preserving them here keeps reviewer context complete.

| ID | Decision | Resolved |
|---|---|---|
| L1 | Target DBMS dialect | **MySQL 8 / MariaDB 10.6+**. JSON type used for `extra_data`. Upsert idiom is `INSERT … ON DUPLICATE KEY UPDATE id=id` (explicit no-op). DATETIME(3) used for timestamps; UTC by convention (see §3.1). |
| L2 | `user` column name | **Renamed to `user_name`** (Recommended option). The CSV `Person` column maps to `user_name`; downstream consumers expecting a `user` field receive `user_name` — that deviation is documented here, not silently encoded. |
| L3 | Web UI authentication | **HTTP Basic** with `WEB_USER` + `WEB_PASS_HASH` env vars. Single operator account. |
| L4 | BlueWeb transport | **Plain HTTP on LAN.** No TLS to BlueWeb. The worker logs a warning on every run and refuses to use a non-private destination host (see §6.1). |
| L5 | Deployment topology | Worker container on its own host; BlueWeb on a separate LAN host; MySQL on a third (separate) host. Three hosts, all on a trusted private network segment. |
| L6 | Catch-up on missed days | **Auto catch-up on boot**, oldest first, capped at `CATCH_UP_CAP_DAYS` (default 14). Algorithm in §6.4. |
| L7 | Failure notifications | **`/healthz` polling only.** No SMTP, no webhooks in v1. |
| L8 | BlueWeb Event retention | **Unknown — discover empirically.** Backfill UI is unbounded apart from a safety cap (365 days). Empty-report outcome is `status='ok'`, `rows_in_csv=0` (see §6.4.2). |
| L9 | Worker OS | **Linux container** based on `python:3.12-slim` (Debian bookworm). |
| L10 | `source` value | **Literal `"Bluewave"`**. Single instance forever; if a second BlueWeb host appears in v2, this becomes a §11 reopen. |
| L11 | DB bootstrap | **DB + worker user pre-exist.** Worker creates the table the first time it starts. Worker user needs `CREATE, INSERT, SELECT` on the audit database. |
| L12 | Run history retention | **Indefinite** for `z_audit_logs_efk_runs`. No GC job. |
| L13 | Web UI TLS | **HTTP only on a private network.** Operator must set `WEB_ALLOW_HTTP=1` explicitly; container refuses to start otherwise. No reverse proxy assumed (operator's responsibility to add one if desired). |
| L14 | MySQL TLS | **Plaintext.** The MySQL driver is configured without TLS. This is an explicit operational concession to a trusted internal network; flagged in §8 as a residual risk. |
| L15 | Backfill UI | **Single date OR "catch up missing"**. One date picker plus a button that queues every missing day since the last `ok` run (capped at `CATCH_UP_CAP_DAYS`). |
| L16 | Config provisioning | **Web UI only** at first boot. No env-file fallback in v1. |
| L17 | DST handling | Use Python `zoneinfo` with `fold=0` for ambiguous local times (fall-back hour: pick the earlier UTC instance). Local times in the non-existent spring-forward hour raise `MalformedCsvError` — the run fails loud rather than guessing. |
| L18 | Reject future / today as `report_date` | `POST /run` and the Backfill UI return HTTP 400 if `report_date >= today_local`. Today is partial; manual runs for today are explicitly out of scope. |
| L19 | MySQL connection style | **Fresh `pymysql.connect()` per run**, using keyword args (`host`, `port`, `user`, `password`, `database`, `charset='utf8mb4'`). No connection pool, no DSN URL parsing, no idle-timeout problem. |
| L20 | Schema charset / collation validation | The schema check at startup verifies `z_audit_logs_efk.CHARACTER_SET_NAME='utf8mb4'` and `COLLATION_NAME='utf8mb4_unicode_ci'`. Mismatch → `schema_drift`. |
| L21 | `CONFIG_ENC_KEY` rotation | **MultiFernet** with up to 4 keys. Operator rotates by adding a new key at the head of `CONFIG_ENC_KEYS` (plural env var, comma-separated), running `python -m bluewave.rotate_config` to re-encrypt with the new head key, then dropping the old key on the next deploy. |
| L22 | Worker self-audit | The worker writes a row to `z_audit_logs_efk` for every operator action (login, config save, run-now, backfill, catch-up) and for container lifecycle events (boot, shutdown, schema-drift detection), with `source='bluewave-worker'`. Same table, same query surface; no separate operator-audit infrastructure. |
| L23 | Dashboard time display | All rendered timestamps are `YYYY-MM-DD HH:MM:SS UTC`. Dashboard footer shows the configured operator timezone and the next scheduled fire in both local and UTC. No browser-locale ambiguity. |
| L24 | Tier 2 features adopted | Friendly site label (config field, shown on dashboard), `Test BlueWeb` + `Test MySQL` buttons on `/config`, OpenAPI `/docs` gated behind Basic Auth and `LOG_LEVEL=DEBUG`, dashboard pagination (`?limit&offset`), container image SBOM + signed-digest pinning in `docker-compose.yml`. |

---

## 1. Scope

### 1.1 In-scope

- Drive **one** local BlueWeb v20 instance (single host, single tenant) via headless Chromium + Selenium WebDriver.
- Generate one report type: **Event** report, for **the previous calendar day** in the configured operator timezone.
- Download the CSV emitted by the BlueWeb UI (filename verbatim: `BlueWeb - Reports.csv`), parse it, transform each row into the common audit schema, idempotently insert into `z_audit_logs_efk`.
- A web GUI served by the same container for: one-time configuration, run-now (yesterday), single-date backfill, "catch up missing", run-history dashboard, `/healthz`.
- Daily scheduled run at an operator-configured local time. Plus operator-triggered ad-hoc runs.
- Auto catch-up of missed scheduled days on container boot, up to a configurable cap (default 14 days).
- Worker self-audit: every operator action (login, config save, run-now, backfill, catch-up) and every container lifecycle event (boot, shutdown, schema-drift detection) is written to `z_audit_logs_efk` itself, using `source='bluewave-worker'`. See §3.6.

### 1.2 Out-of-scope (explicit)

- Any BlueWeb report type other than Event.
- Multiple BlueWeb instances per container (deploy multiple containers if needed — see §11/D1).
- Real-time / streaming ingestion. BlueWeb has no event push surface; polling the CSV report is the only contract.
- Mutating BlueWeb in any way. Read-only scraping. The worker never touches People, Doors, Holidays, Advanced, or Emergency Lockdown. A selector denylist enforces this.
- OCR, image-based fallbacks, or screen-scraping the rendered HTML table. CSV is the only data surface.
- Alerting, BI dashboards, retention/rollup jobs over `z_audit_logs_efk`. Those belong to consumer modules.
- Cross-source deduplication. Each source manages dedup within its own row population.
- SMTP, webhooks, paging, IaC-driven config bootstrap (see L7, L16).
- TLS termination, certificate management, or any cryptographic protection of the BlueWeb session or MySQL connection (L4, L14). The deployment relies on network-segment trust.

---

## 2. Source surface — what the worker must drive

The flow is exactly what an operator does manually (screenshots provided by the requester confirm). Each step is a discrete Selenium action with a guarded wait. The catalog below is **prescriptive for behavior, descriptive for selectors** — the exact CSS / XPath strings are an M3 deliverable derived from inspecting the live page, not baked into this document.

| Step | Page state | Action | Wait condition (success) | Failure to surface |
|---|---|---|---|---|
| S1 | Login form (BlueWEB / JoshuaTree v20; fields: User Name, Password, Login button) | type username, type password, click Login | URL changes off the login page **and** the string `Welcome Administrator` is present in the page | "Invalid login" or unrecognized response → fail run as `auth_failed` |
| S2 | Main menu (icons: People / Doors / Holidays / Reports / Advanced) | click **Reports** | the literal text `Choose Report:` is visible | menu icon missing → `nav_failed` |
| S3 | Reports page | select **Event** in the `Choose Report` dropdown | caption `Events report filtered by date, door and/or associate.` visible AND Start/End Date inputs rendered | dropdown option missing → `nav_failed` |
| S4 | Date fields default to today (e.g. `05/13/2026`) | overwrite Start Date and End Date with **yesterday's date** in `MM/DD/YYYY` format computed per §2.1 | input values match the target string after JS settle | input rejected → `nav_failed` |
| S5 | Filters: Person / Site / Area / Door default to `(All)` | leave at default | — | — |
| S6 | "Get Report" button | click | results table tbody renders ≥ 1 row **or** an unambiguous "no rows" indicator within 60 s | timeout → `report_timeout` |
| S7 | Export bar (`Copy / CSV / Excel / PDF / Print`) | click **CSV** | file `BlueWeb - Reports.csv` materializes in the configured Chromium download dir, with no `.crdownload` suffix, within 60 s | timeout / file missing → `download_failed` |
| S8 | (worker host, post-download) | parse CSV → transform → insert into MySQL | rows inserted; run row updated to `ok` | parse/DB errors → `parse_failed` / `ingest_failed` |
| S9 | (worker host, post-success) | delete the local CSV file, click **Log Out** in BlueWeb, `driver.quit()` | file gone; browser closed | non-fatal; log only |

**Hard rules:**
- The worker **never** clicks `Emergency Lockdown` (main menu, screenshot 2). The worker's selectors module **must** declare a denylist containing this control; M3's tests assert that no scrape code path can reach it.
- The worker **never** clicks `Log Out` until the run is complete (S9). Logging out mid-run drops the session and forces a re-login that wastes the run's API budget.

### 2.1 Date semantics — the one step that's easy to get wrong

> *"Since we cannot select time, we must select last day (so today 13, we are going to select 12)."*

Formal definition for a scheduled run:

```
run_clock_utc        := now() in UTC
operator_timezone    := config.timezone (IANA, e.g. "America/New_York"); required
report_date_local    := (run_clock_utc → operator_timezone).date() - 1 day
start_date_input     := report_date_local formatted as "MM/DD/YYYY"
end_date_input       := report_date_local formatted as "MM/DD/YYYY"
```

For a manual backfill (single date) or a catch-up entry, `report_date_local` is the operator-supplied or queued date.

`MM/DD/YYYY` because the v20 screenshot shows the format that BlueWeb expects (`05/13/2026`). The worker never sends a non-US date format. If BlueWeb is ever localized differently, that's a new §11 decision.

### 2.2 Date-input injection strategy

BlueWeb's date inputs are HTML inputs with adjacent calendar-picker icons. Two strategies, in order of preference, evaluated during M3:

1. **JS value injection.** `driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));", input_el, "05/12/2026")`. Works for most JS-bound date pickers; preserves the framework's change-event lifecycle.
2. **Send keys.** `input_el.clear(); input_el.send_keys("05/12/2026")`. Fallback for inputs that ignore programmatic `value=` assignment.

The selected strategy is recorded in `selectors.py` alongside the selector itself. We do not attempt the calendar-icon click-and-navigate flow — it is brittle across month boundaries and adds two months × four clicks of complexity for no benefit.

---

## 3. Target schema — `z_audit_logs_efk` on MySQL 8 / MariaDB 10.6+

This table is **shared infrastructure**. Other modules may write to it. Consequently:

- This worker **creates the table only if it does not exist**.
- This worker **never `ALTER`s an existing table**. On startup, the worker queries `information_schema.COLUMNS` for the column list and types **and** `information_schema.TABLES` for `TABLE_COLLATION` / charset, comparing both against the expected set. **Any divergence fails fast** with `schema_drift` in `/healthz` and a banner on the dashboard.
- Charset / collation drift is its own first-class failure: a pre-existing table created with the 3-byte legacy alias `utf8` (instead of `utf8mb4`) will silently truncate 4-byte characters like `ñ` or emoji. The schema check catches this before any insert is attempted.
- The owning user for ingest is `audit_writer` (MySQL user with `CREATE` for first-boot table creation, then `INSERT, SELECT` ongoing). Privilege downscope is documented but not auto-enforced — the DBA grants the broader privilege at provisioning and may revoke `CREATE` once bootstrap is complete.

### 3.1 DDL — `z_audit_logs_efk`

```sql
CREATE TABLE IF NOT EXISTS z_audit_logs_efk (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  timestamp       DATETIME(3)     NOT NULL,
  source          VARCHAR(64)     NOT NULL,
  operation       VARCHAR(128)    NOT NULL,
  instance        VARCHAR(128)    NOT NULL,
  user_name       VARCHAR(256)    DEFAULT NULL,
  user_id         VARCHAR(64)     DEFAULT NULL,
  extra_data      JSON            DEFAULT NULL,
  comments        VARCHAR(1024)   DEFAULT NULL,
  dedup_hash      CHAR(64)        NOT NULL,
  ingested_at     DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  ingest_run_id   BIGINT UNSIGNED DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_dedup_hash (dedup_hash),
  KEY ix_source_ts (source, timestamp),
  KEY ix_ts        (timestamp),
  KEY ix_userid_ts (user_id, timestamp)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
```

Notes:
- `DATETIME(3)` not `TIMESTAMP`: MySQL `TIMESTAMP` displays in the session timezone and has a 2038 problem on 32-bit builds. `DATETIME(3)` stores the literal bytes; we adopt **UTC-by-convention** for every row in this column and document it loudly (also see operator runbook §13.1).
- `JSON` type stores `extra_data` natively; MySQL validates JSON on insert. The `dedup_hash` input uses the canonical string form from §5.2, not MySQL's internal storage, so equivalent-but-differently-formatted JSON cannot accidentally produce different hashes.
- `utf8mb4` is required because the CSV contains characters like `ñ`, `ñ`, accented vowels in real Person names (observed in the supplied sample). Using `utf8` (which is MySQL's three-byte legacy alias) would silently truncate the four-byte sequences.
- Index `ix_source_ts` is intentionally ascending (no `DESC` keyword). MySQL 8's optimizer can scan B-tree indexes backwards for `ORDER BY … DESC` queries with no penalty; MariaDB before 10.8 ignores descending-index syntax anyway.

### 3.2 Column semantics

| Column | Source | Rules |
|---|---|---|
| `id` | DB-generated | Surrogate PK. Not derived from source. |
| `timestamp` | CSV `Date/Time` | UTC. Parsed from `MM/DD/YYYY HH:MM:SS` using `operator_timezone`, then `astimezone(UTC)`. Sub-second is always `.000`. |
| `source` | constant | Always literal `"Bluewave"` for rows produced by this worker. |
| `operation` | CSV `Description` | Stripped. Observed values include `Admit W1`, `Reject W1`, and several `Permissions - …` administrative variants. All flow through verbatim. |
| `instance` | CSV `Door Name` | Stripped of leading/trailing whitespace. Interior whitespace preserved (real door names occasionally contain double spaces). |
| `user_name` | CSV `Person` | Stripped. Empty-after-strip → NULL. Stored as `VARCHAR(256)` because observed names exceed 60 characters when including hyphenation. |
| `user_id` | CSV `Employee ID` | Stripped. Whitespace-only (`" "`) → NULL. Stored as text — values include `XSQ008882` (prefixed alphanumeric) and `9367` (numeric); text preserves leading zeros and prefix. |
| `extra_data` | derived | See §5.2. NULL when both Card Number and Facility Code are empty. |
| `comments` | — | Always NULL from this worker. Reserved for consumer-side annotation. |
| `dedup_hash` | derived | See §5.3. UNIQUE constraint provides idempotency. |
| `ingested_at` | DB-generated | `now()` at insert time. |
| `ingest_run_id` | derived | FK-style reference to `z_audit_logs_efk_runs.id`. No declared foreign key — to avoid coupling the shared table's DDL to this worker's operational table. |

### 3.3 Why a content hash, not a source row ID

The BlueWeb CSV does not include a stable per-event identifier. `(source, timestamp, instance, user_id, operation)` is not sufficient: two badge taps in the same wall-clock second by the same person at the same door are physically possible and must remain distinct events. Including the canonical `extra_data` JSON in the hash distinguishes them when the swipes produce different facility/card-number context; if the two events are byte-for-byte identical including the JSON, they are genuinely duplicates and collapsing them is acceptable.

### 3.4 DDL — `z_audit_logs_efk_runs`

Operational table owned by this worker. Not part of the shared contract.

```sql
CREATE TABLE IF NOT EXISTS z_audit_logs_efk_runs (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source          VARCHAR(64)     NOT NULL,
  report_date     DATE            NOT NULL,
  started_at      DATETIME(3)     NOT NULL,
  finished_at     DATETIME(3)     DEFAULT NULL,
  status          VARCHAR(32)     NOT NULL,
  rows_in_csv     INT UNSIGNED    DEFAULT NULL,
  rows_inserted   INT UNSIGNED    DEFAULT NULL,
  rows_duplicate  INT UNSIGNED    DEFAULT NULL,
  manual          TINYINT(1)      NOT NULL DEFAULT 0,
  error_excerpt   VARCHAR(2000)   DEFAULT NULL,
  screenshot_path VARCHAR(512)    DEFAULT NULL,
  -- Generated column for partial-uniqueness on successful scheduled runs.
  ok_scheduled_date DATE GENERATED ALWAYS AS
    (CASE WHEN status='ok' AND manual=0 THEN report_date ELSE NULL END) VIRTUAL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ok_scheduled_date (source, ok_scheduled_date),
  KEY ix_source_date (source, report_date),
  KEY ix_started (started_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
```

`status` enum (string for portability across MySQL versions, validated in application code):

```
running, ok, auth_failed, nav_failed, report_timeout, download_failed,
parse_failed, ingest_failed, schema_drift, skipped
```

The generated `ok_scheduled_date` column produces NULL for failed or manual runs, and a date for successful scheduled runs. MySQL allows multiple NULLs in a UNIQUE index, so this gives us "at most one successful scheduled run per (source, date)" without blocking failures or manual backfills.

### 3.5 Idempotent insert idiom

```sql
INSERT INTO z_audit_logs_efk
  (timestamp, source, operation, instance, user_name, user_id,
   extra_data, comments, dedup_hash, ingest_run_id)
VALUES (...), (...), (...)
ON DUPLICATE KEY UPDATE id = id;
```

Rationale for `id = id` over `INSERT IGNORE`:
- `INSERT IGNORE` silently masks **all** errors (data-truncation, FK violations, etc.) — too broad.
- `ON DUPLICATE KEY UPDATE id = id` is a no-op only on duplicate-key, preserving real errors.

Batch size: 500 rows per INSERT statement (well under MySQL's `max_allowed_packet` default of 64 MB even with verbose JSON). One transaction wraps the entire run; on rollback, the run row is updated to a failure status outside the transaction.

`rows_inserted` is computed as `cursor.rowcount` post-statement; MySQL returns the count of *modified* rows (2 per inserted, 1 per duplicate by convention with `CLIENT_FOUND_ROWS=False`). The worker uses `SELECT COUNT(*)` on a freshly-tagged `ingest_run_id` instead for an exact count — simpler than reasoning about driver quirks.

### 3.6 Self-audit writes to `z_audit_logs_efk`

This worker dogfoods the shared audit table — every operator action and every container lifecycle event is recorded as a row in `z_audit_logs_efk` with `source='bluewave-worker'` (distinct from `source='Bluewave'` used for scraped BlueWeb events). Rationale: the worker is, itself, a system whose actions belong in the org's audit trail. Reusing the same table means analysts can ask "what did the operator change yesterday?" with the same SQL surface they already use.

#### 3.6.1 Column mapping for self-audit rows

| Column | Value |
|---|---|
| `timestamp` | UTC at the moment the event occurred. |
| `source` | Literal `"bluewave-worker"`. |
| `operation` | One of the events in §3.6.2. |
| `instance` | Container hostname (`socket.gethostname()`). Disambiguates if you ever run two worker containers. |
| `user_name` | The Basic Auth username for operator-initiated events. NULL for system events (boot, scheduled fire, schema drift). |
| `user_id` | NULL. |
| `extra_data` | Event-specific JSON (see §3.6.2). Canonical-JSON rules from §5.2 apply so `dedup_hash` remains deterministic. |
| `comments` | NULL. |
| `dedup_hash` | Same SHA-256 recipe as §5.3, computed over the self-audit row's fields. |
| `ingest_run_id` | NULL — these rows are not part of any scraped-CSV run. |

#### 3.6.2 Event catalog (closed set)

| `operation` | Triggered by | `extra_data` shape |
|---|---|---|
| `worker.boot` | Container startup, after config loaded | `{"version":"X.Y.Z"}` |
| `worker.shutdown` | SIGTERM received, after graceful drain | `{"reason":"sigterm"}` |
| `worker.schema_drift` | Schema validation fails | `{"diff":"<short summary, ≤300 chars>"}` |
| `web.login` | Successful Basic Auth on any route | `{"route":"GET /","client_ip":"…"}` |
| `web.login_failed` | Failed Basic Auth | `{"route":"GET /","client_ip":"…"}` |
| `config.save` | Successful `POST /config` | `{"fields_changed":["mysql_host","schedule"]}` (no values; just keys) |
| `config.test_bluewave` | Operator clicks "Test BlueWeb" | `{"result":"ok"\|"failed","detail":"…"}` |
| `config.test_mysql` | Operator clicks "Test MySQL" | `{"result":"ok"\|"failed","detail":"…"}` |
| `run.requested` | `POST /run` accepted | `{"report_date":"YYYY-MM-DD","manual":true,"run_id":N}` |
| `run.catchup_requested` | `POST /run/catchup` accepted | `{"enqueued_dates":["YYYY-MM-DD",…]}` |
| `key.rotated` | `python -m bluewave.rotate_config` finishes | `{"old_key_fingerprint":"…","new_key_fingerprint":"…"}` |

`web.login` is rate-limited to **one row per operator per 5-minute window** (deduped on the client_ip + operator pair) to prevent log floods from a stuck browser tab polling `/healthz` — `/healthz` itself is exempt from `web.login` audit (it's a machine-facing endpoint, not an operator action).

**Self-audit failures are non-fatal.** If the audit insert raises, the operator action still completes; the failure is logged at `WARNING`. We do not want the dashboard refusing to render because MySQL is unreachable for an unrelated reason.

---

## 4. Architecture

### 4.1 Topology

Three hosts on a trusted private network:

```
                                  ┌──────────────────────────────────┐
┌──────────────────────────┐      │   BlueWeb v20 host  (LAN)        │
│  Worker host (Linux)     │  ──▶ │   HTTP (no TLS) on a known port  │
│  Docker daemon           │      │   "service to be entered"        │
│   └─ bluewave-worker     │      └──────────────────────────────────┘
│        container         │
│        :8080  ◀── operator browser (HTTP, private network only)
│                          │      ┌──────────────────────────────────┐
│                          │ ──▶  │   MySQL host  (separate LAN box) │
└──────────────────────────┘      │   :3306  plaintext               │
                                  │   database: audit                │
                                  │   user:     audit_writer         │
                                  └──────────────────────────────────┘
```

All three legs traverse the LAN in plaintext. This is the explicit posture (L4, L13, L14). The deployment depends on **network-segment trust** — see §8 for the full security framing and accepted risks.

### 4.2 In-container processes

The container runs **two long-running processes inside a single Python interpreter**:

- A **FastAPI** application served by **uvicorn** on `0.0.0.0:8080`.
- An **APScheduler** `BackgroundScheduler` started in the same process during FastAPI's `startup` event handler.

Co-locating them in one interpreter avoids the need for an IPC layer between "the thing the operator clicks" and "the thing that runs the job." The run-lock (§7) is a `threading.Lock` held inside that interpreter, augmented by an advisory DB lock so that a multi-container deployment (not in v1, but cheap to support) would still serialize correctly.

A single supervisor is **not** introduced — uvicorn's lifecycle is the container's lifecycle. SIGTERM from `docker stop` propagates to uvicorn, which calls FastAPI's `shutdown` event, which gracefully stops APScheduler and joins in-flight runs (bounded by a 60 s grace).

**MySQL connection model (L19):** a **fresh `pymysql.connect()` per run** is opened just before the schema validation / insert phase and closed in the same `finally` block as `driver.quit()`. No connection pool, no long-lived sessions. This sidesteps MySQL's `wait_timeout` (default 8 h) entirely — a connection used once and closed cannot go stale. Connections for the web routes (`/healthz`, dashboard reads) are also short-lived: opened per request, closed before the response is sent. At ~1 scheduled run/day plus a handful of dashboard hits, the overhead is irrelevant; the simplification is real.

### 4.3 Container image

- Base: `python:3.12-slim` (Debian bookworm slim).
- Apt-installed (pinned versions, refreshed quarterly): `chromium`, `chromium-driver`, `tini`, `tzdata`, `ca-certificates`, `fonts-dejavu-core`.
- Python deps (pinned in `requirements.txt`): `fastapi`, `uvicorn[standard]`, `apscheduler`, `selenium`, `pymysql` (pure-python, no native build deps), `cryptography` (MultiFernet — see §8.4), `pydantic`, `bcrypt` (or `passlib[bcrypt]`), `jinja2`, `python-multipart`, `itsdangerous`.
- Entry point: `tini -- python -m bluewave.web`.
- Runtime UID: `10001:10001` (non-root user `worker`). The `/var/lib/bluewave-worker` and `/tmp/bluewave-dl` directories are `chown`ed to that UID in the Dockerfile.
- `EXPOSE 8080`.
- `HEALTHCHECK CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1` with a 60 s interval.
- **Supply chain hygiene (L24):** the build emits a CycloneDX SBOM via `docker buildx build --sbom=true --provenance=true`. The image is pushed by digest (`registry/bluewave-worker@sha256:…`) and `docker-compose.yml` pins that digest, not a mutable tag. The SBOM ships next to the image in the registry; the operator runbook (§13.7) covers how to inspect it before deploying a new version.

### 4.4 Volume layout

A single bind-mount or named volume covers everything stateful:

```
/var/lib/bluewave-worker/
├── config.sqlite              # Fernet-encrypted operator config
├── config.sqlite-shm          # SQLite WAL companion files
├── config.sqlite-wal
└── screenshots/
    ├── 2026-05-12_run-42.png  # at most CATCH_UP_CAP_DAYS × 4 retained
    └── ...
```

Plus an ephemeral, container-internal location:

```
/tmp/bluewave-dl/               # Chromium download dir, tmpfs preferred,
                                # wiped before each run (§6.3)
```

**`docker-compose.yml` resource limits (recommended defaults):**

```yaml
services:
  bluewave-worker:
    image: registry.local/bluewave-worker@sha256:...   # pin by digest (§4.3)
    deploy:
      resources:
        limits:
          memory: 1g
          cpus: "1.0"
    tmpfs:
      - /tmp/bluewave-dl:size=128m
    volumes:
      - bluewave-state:/var/lib/bluewave-worker
    environment:
      WEB_ALLOW_HTTP: "1"
      TZ: UTC
      # CONFIG_ENC_KEYS, WEB_USER, WEB_PASS_HASH from a .env file
    ports:
      - "8080:8080"
    restart: unless-stopped
```

Chromium is the only memory-hungry component. The 1 GB limit is comfortable for the volumes observed in the supplied sample (≈1 100 rows/day); a hard ceiling guards against runaway pages. `tmpfs` for the download dir keeps the per-run CSV out of any persistent storage layer.

### 4.5 Environment variable catalog

| Var | Required | Default | Purpose |
|---|---|---|---|
| `CONFIG_ENC_KEYS` | yes | — | Comma-separated list of one or more Fernet keys (each 44-char URL-safe base64). The **first** key encrypts new writes; **all** keys are tried in order on decrypt. This is the MultiFernet construction (L21). Generate via `python -m bluewave.keygen`. Single-key deployments simply set one value. |
| `WEB_USER` | yes | — | Basic Auth username for the operator. |
| `WEB_PASS_HASH` | yes | — | bcrypt hash of the operator password. Generated via `python -m bluewave.hashpw`. |
| `WEB_BIND` | no | `0.0.0.0:8080` | Bind address. May be set to `127.0.0.1:8080` if fronted by a host-level proxy. |
| `WEB_ALLOW_HTTP` | yes | — | Must be `1` (L13). Container refuses to start otherwise, to force the operator to acknowledge the HTTP-only posture. |
| `CATCH_UP_CAP_DAYS` | no | `14` | Maximum number of past calendar days the boot-time and on-demand catch-up routines will enqueue. Range 1–90. |
| `BACKFILL_SAFETY_CAP_DAYS` | no | `365` | Maximum age for an operator-chosen single-date backfill. |
| `LOG_LEVEL` | no | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. Setting `DEBUG` also enables the `/docs` OpenAPI page. |
| `TZ` | no | `UTC` | Container's libc timezone. **Must remain `UTC`** — the worker validates this on startup and refuses to start if `TZ` is anything else, because every internal time math operation assumes UTC. Operator timezone (a different concept) is configured in the web UI and applied in Python via `zoneinfo`, not via libc. |

Anything else (BlueWeb URL/user/password, MySQL host/port/db/user/password, operator timezone, schedule, site label, catch-up cap override) is **not** an env var — it is operator-managed via the web UI (L16).

---

## 5. CSV → row mapping — the precise transform

The CSV produced by BlueWeb v20 (sample `BlueWeb - Reports.csv` reviewed in detail):

- Encoding: **UTF-8**. Contains characters like `ñ`, `ñ`, accented vowels in Person names — observed in the sample (e.g. `Atencio-Añez, Juan J`, `Saldaña-Fernandez, Alba Alici`).
- Quoting: every field double-quoted; commas inside quoted fields are common (Person names are `LAST, FIRST` form, sometimes `LAST-LAST, FIRST M`).
- Header line: `"Date/Time","Door Name","Description","Person","Employee ID","Card Number","Facility Code","Pin"`.
- Empty Employee ID appears as `" "` (literal single space inside quotes), not as empty string.

**Therefore:** use Python's standards-compliant `csv` module (`csv.reader` with `quoting=csv.QUOTE_ALL`). Never `split(',')`.

### 5.1 Field-by-field rules

| CSV field | Target field | Transform |
|---|---|---|
| `Date/Time` | `timestamp` | Parse with `datetime.strptime(value, "%m/%d/%Y %H:%M:%S")` as **naive local**, then attach `ZoneInfo(operator_timezone)` and `.astimezone(timezone.utc)`. DST handling per §5.1.1. Reject (whole-run-fail) if any row fails to parse — do not silently skip. |
| `Door Name` | `instance` | `.strip()`. Preserve interior whitespace. |
| `Description` | `operation` | `.strip()`. Observed values: `Admit W1`, `Reject W1`, `Permissions - Processing`, `Permissions - Queued`, `Permissions - Update Successful`, `Permissions - Updating Controller`. All pass through verbatim. |
| `Person` | `user_name` | `.strip()`. Empty after strip → NULL. |
| `Employee ID` | `user_id` | `.strip()`. Empty after strip → NULL. Stored as text. |
| `Card Number` | `extra_data.card_number` | `.strip()`. Empty → key omitted. |
| `Facility Code` | `extra_data.facility_code` | `.strip()`. Empty → key omitted. |
| `Pin` | **dropped** | Always `****` in observed data; treated as a credential surrogate. Never persisted, never logged. |

`source` is set to `"Bluewave"` for every row. `comments` is NULL.

### 5.1.1 DST handling (L17)

The BlueWeb CSV emits **naive local timestamps** in the operator timezone. Around DST transitions, two pathological wall-clock values exist:

- **Ambiguous (fall-back):** in `America/New_York`, on the fall transition day, `01:30:00` happens twice — once at UTC-4 and once at UTC-5. Python `zoneinfo` resolves this via the `fold` attribute. **Rule: always use `fold=0`** (the earlier, pre-transition UTC instance). Rationale: ordering matters for audit data, and the earlier reading is the safer guess for a system that only emits wall-clock time. Document loudly so analysts know that on the duplicated hour, the second BlueWeb-reported event of the hour will appear *before* the first in UTC order.

```python
naive = datetime.strptime(s, "%m/%d/%Y %H:%M:%S").replace(fold=0)
local = naive.replace(tzinfo=ZoneInfo(operator_tz))
utc   = local.astimezone(timezone.utc)
```

- **Non-existent (spring-forward):** on the spring transition day, `02:30:00` does not exist. A BlueWeb-emitted timestamp in this window indicates either a misconfigured BlueWeb clock or corrupt data. **Rule: raise `MalformedCsvError`** — the whole run fails. We do not guess. The operator investigates the BlueWeb host's clock.

A unit test in M5 includes a synthetic CSV with one ambiguous and one non-existent timestamp and asserts both rules.

The dedup hash uses the resulting UTC time (`%Y-%m-%dT%H:%M:%S.000Z`), so the `fold=0` choice is baked into the hash. Switching the rule later requires a hash-version bump (§11/D2).

### 5.2 `extra_data` canonical form

```json
{"card_number":"5057","facility_code":"190"}
```

Rules, in order — they exist to make `dedup_hash` deterministic:

1. Keys are `snake_case` exactly as listed (`card_number`, `facility_code`).
2. Keys appear in **alphabetical order** in the canonical JSON string.
3. No whitespace between tokens (`json.dumps(..., separators=(",", ":"))`).
4. `ensure_ascii=False` — preserve UTF-8 in the canonical string. (No source values are unicode today, but locking the rule prevents drift.)
5. A key whose source value is empty/whitespace is **omitted entirely** (not `null`, not `""`).
6. If both keys are omitted, `extra_data` is `NULL` in the row and the dedup-hash input for `extra_data_str` is the empty string `""`.

A single helper `canonical_extra_data(card_number, facility_code) -> Optional[str]` is the only place this is constructed. Exercised by §10/M5.

### 5.3 The `dedup_hash`, exactly

```python
def dedup_hash(row) -> str:
    canonical_ts = row.timestamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    parts = [
        "Bluewave",
        canonical_ts,
        row.operation,                 # stripped, raw
        row.instance,                  # stripped, raw
        row.user_id or "",
        row.user_name or "",
        row.extra_data_str or "",      # §5.2 canonical JSON, or "" if NULL
    ]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

`|` is the field separator. No observed source value today contains `|`; if a real collision is ever reported, the response is documented in §11/D2 (escape and bump a hash version). The function is pure and tested by the golden-fixture comparison in M5.

---

## 6. Selenium and run-orchestration mechanics

### 6.1 Browser configuration

`bluewave/driver.py` builds a Chromium driver with these options:

```
--headless=new
--no-sandbox                          # required to run as non-root in many container runtimes; document the tradeoff
--disable-dev-shm-usage               # small /dev/shm in containers
--disable-gpu                         # no GPU available, suppresses log spam
--window-size=1366,900                # matches the operator screenshots
--lang=en-US                          # forces the BlueWeb UI to its English strings
--user-data-dir=/tmp/cr-profile-{pid} # ephemeral profile per run
```

Download prefs (via `add_experimental_option("prefs", …)`):

```
download.default_directory       = /tmp/bluewave-dl
download.prompt_for_download     = False
download.directory_upgrade       = True
safebrowsing.enabled             = False
```

In modern headless Chromium some prefs are ignored; the worker also calls DevTools `Page.setDownloadBehavior` with `behavior=allow, downloadPath=/tmp/bluewave-dl` as a belt-and-suspenders fallback.

`pageLoadStrategy=normal`. All waits are explicit and condition-based.

### 6.2 Wait strategy

- One `WebDriverWait` helper with a 30 s default. S6 and S7 override to 60 s.
- All waits use `EC.element_to_be_clickable`, `EC.text_to_be_present_in_element`, or custom predicates (`lambda d: download_file_ready(...)`).
- Exactly one `time.sleep(0.3)` is allowed after S4 — a 300 ms beat to let the date picker's `change` handler propagate. Documented in code with a comment citing this paragraph.
- Every wait that times out captures a screenshot to `/var/lib/bluewave-worker/screenshots/{report_date}_run-{run_id}.png` plus the current URL and page title. The screenshot path is recorded on the run row.

### 6.3 Session hygiene

- **Fresh ChromeDriver process per run.** ~2 s startup cost; eliminates entire classes of state-leak bugs.
- The download dir (`/tmp/bluewave-dl`) is wiped before every run.
- After a run (success or failure), `driver.quit()` is called in a `finally` block.
- A startup-time **process reaper** kills any orphaned `chrome` or `chromedriver` processes left from a previous container crash. Logs the count if non-zero.

### 6.3.1 BlueWeb host clock-drift check

CSV timestamps are wall-clock-local to the BlueWeb host. If that host's clock drifts from the operator-configured timezone's actual clock, the worker silently mis-converts every row.

After successful login (S1), the worker reads BlueWeb's currently-displayed clock — the post-login page typically shows it; the selector for this is an M3 deliverable (`bw_clock_display` in `selectors.py`). The worker compares that value to `datetime.now(ZoneInfo(operator_tz))`. If the absolute difference exceeds **5 minutes**, the worker logs a `warning` event (`bluewave.clock_drift`, with `extra_data.drift_seconds`) but does **not** fail the run. The threshold is a code constant; if BlueWeb does not expose a clock element this gracefully degrades to "skipped, drift unknown" (also logged).

This check is best-effort observability, not a gate — failing runs over a clock skew of 6 minutes would be worse than ingesting slightly-misaligned timestamps.

### 6.4 Run-orchestration algorithm

#### 6.4.1 The run state machine

```
run_start(run_row):
    transaction:
        UPDATE z_audit_logs_efk_runs SET status='running', started_at=now() WHERE id=run_row.id

    try:
        driver = build_driver()                  # §6.1
        wipe_download_dir()
        login(driver, config)                    # S1, S2          → may raise AuthFailed / NavFailed
        navigate_to_event_report(driver)         # S3              → NavFailed
        set_date_range(driver, report_date)      # S4              → NavFailed
        click_get_report(driver)                 # S6              → ReportTimeout
        csv_path = click_csv_and_wait(driver)    # S7              → DownloadFailed
        rows = transform(csv_path, tz)           # §5              → MalformedCsvError
        n_in, n_dup = sink_insert(rows, run_row.id)                # → IngestFailed
        click_logout(driver)                     # S9
        delete_file(csv_path)
        finalize(run_row, status='ok', rows_in_csv=len(rows),
                 rows_inserted=n_in, rows_duplicate=n_dup)
    except KnownError as e:
        capture_screenshot(driver, run_row)
        finalize(run_row, status=e.status, error_excerpt=str(e)[:2000])
    except Exception as e:
        capture_screenshot(driver, run_row)
        finalize(run_row, status='ingest_failed', error_excerpt=truncate(traceback.format_exc()))
    finally:
        try: driver.quit()
        except Exception: pass
```

#### 6.4.2 Empty-report handling

An "empty" report (Get Report yields a "no results" indicator instead of a table) is a **success**:

```
finalize(run_row, status='ok', rows_in_csv=0, rows_inserted=0, rows_duplicate=0)
```

Rationale: distinguishing "I ran and there was nothing" from "I didn't run" is what `z_audit_logs_efk_runs` exists to do. The downstream catch-up algorithm must treat such a date as ingested.

#### 6.4.3 Catch-up algorithm

Runs on container boot (after config is valid) and when the operator clicks "Catch up missing":

```python
def find_missing_dates(today_local: date, cap_days: int,
                       queued_dates: set[date]) -> list[date]:
    earliest = today_local - timedelta(days=cap_days)
    candidates = [today_local - timedelta(days=i) for i in range(1, cap_days + 1)]

    # Already-ingested dates: ok or skipped scheduled runs, plus successful manual runs.
    rows = db.fetchall("""
        SELECT report_date FROM z_audit_logs_efk_runs
        WHERE source = 'Bluewave'
          AND status IN ('ok', 'skipped')
          AND report_date >= %s
    """, [earliest])
    ingested = {r.report_date for r in rows}

    # Exclude anything already queued (in-flight or pending) so re-clicking
    # "Catch up missing" while the previous batch is processing does not
    # double-enqueue.
    return sorted(d for d in candidates
                  if d not in ingested and d not in queued_dates)
```

`queued_dates` is the union of (a) the report_date of any in-flight run and (b) every report_date in the in-process backfill queue. The function is invoked with the queue lock held so the set is consistent.

Enqueued as `manual=False` backfills, processed serially by the run-lock. The "yesterday" daily fire and the catch-up queue go through the same `run_start()` entry point — only the `report_date` differs. Re-clicking "Catch up missing" is therefore safely idempotent: if the queue still has 8 days outstanding from the previous click, a second click adds nothing new.

#### 6.4.4 Concurrency

- A single in-process `RunLock` enforces at most one run at a time.
- `POST /run` returns HTTP 409 if the lock is held.
- The scheduler's daily fire and the catch-up worker both acquire the same lock; if a long-running backfill is in flight when the daily fire wants to start, the daily fire is queued (logged, then retried after the in-flight run releases).

#### 6.4.5 Failure mode → status mapping

The `status` enum is one-to-one with the failure points in §2. The taxonomy is **closed** — any uncaught exception maps to `ingest_failed` as a catch-all, never to a fresh status string. This keeps the dashboard's status filter UI finite.

---

## 7. Web GUI — exact surface

Ten routes. The Jinja2 templates render server-side; no SPA.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard. Renders runs (default last 20, paginated via `?limit&offset`). Each row shows date, status, rows_inserted, duration, manual flag. Header displays the configured **site label** (e.g. "Easy Foods Inc.") plus operator timezone and next-scheduled-fire (in both local and UTC). Banner if config is incomplete. Controls: "Run now (yesterday)", "Backfill (single date)", "Catch up missing". |
| GET | `/config` | Render the config form. Password fields are write-only — when a value exists, the input renders `placeholder="••••••• (set)"`; the actual value is never sent to the browser. Includes "Test BlueWeb" and "Test MySQL" buttons that POST to the routes below without persisting the form. |
| POST | `/config` | Validate and save. Triple-probe on save: HTTP HEAD to BlueWeb, MySQL `SELECT 1`, and a Selenium login. **Nothing is persisted if any probe fails.** Empty password field on submit = keep existing value. On success, APScheduler's cron job is re-registered in the same transaction so schedule or timezone changes take effect within seconds. |
| POST | `/config/test/bluewave` | Stateless test: opens a temporary Selenium session against the form's BlueWeb URL/user/password (not the persisted ones), logs in, logs out, returns JSON `{"result":"ok"\|"failed","detail":"…"}`. Writes a `config.test_bluewave` self-audit row. |
| POST | `/config/test/mysql` | Stateless test: connects to MySQL with the form's host/port/db/user/pass, runs `SELECT 1`, also checks `CHARACTER_SET_NAME` and `COLLATION_NAME` against `utf8mb4` / `utf8mb4_unicode_ci`. Returns JSON `{"result":"ok"\|"failed","detail":"…"}`. Writes a `config.test_mysql` self-audit row. |
| POST | `/run` | Body `{"report_date": "YYYY-MM-DD"\|null}`. `null` = yesterday. Returns 202 with the new run id, or 409 if a run is in flight, or **400 if `report_date >= today_local`** (L18), or 400 if `report_date` is older than `BACKFILL_SAFETY_CAP_DAYS`. |
| POST | `/run/catchup` | Computes the gap (§6.4.3) and enqueues each missing day not already queued. Returns 202 with `{"enqueued": [dates], "skipped_already_queued": [dates]}`. Returns 409 only if no dates could be enqueued AND a run is in flight (rare). |
| GET | `/runs` | Paginated list. Query params: `limit` (default 20, max 200), `offset` (default 0), `status` filter (optional), `manual` filter (optional bool). Returns HTML for browser navigation; returns JSON when `Accept: application/json`. |
| GET | `/runs/{id}` | Run detail: full status, timing, rows, `error_excerpt` if any, link to `screenshot_path` if any. |
| GET | `/healthz` | JSON. Always 200 OK or 503. See §9 for body shape. |
| GET | `/docs` | FastAPI's auto-generated OpenAPI / Swagger UI page. **Only mounted when `LOG_LEVEL=DEBUG`** and always behind the same Basic Auth gate. Off by default — production deploys do not expose it. |

### 7.1 Config form fields

| Field | Type | Required | Validation |
|---|---|---|---|
| Site label | text | yes | 1–80 chars. Free text; rendered verbatim on the dashboard header. Example: `Easy Foods Inc.` (matches the screenshots). |
| BlueWeb base URL | text | yes | Must parse as `http://host[:port]` (HTTPS allowed but L4 says LAN posture is HTTP). Hostname must resolve from inside the container — checked at save. |
| BlueWeb username | text | yes | non-empty |
| BlueWeb password | password (write-only) | yes on first save | non-empty |
| Operator timezone | dropdown of IANA names | yes | must parse as `ZoneInfo(...)`. No default — operator must choose. Changing this re-registers the APScheduler job in the same `POST /config` transaction so the next-fire-time reflects the new TZ immediately. |
| MySQL host | text | yes | hostname or IP, reachable from container |
| MySQL port | int | no (default 3306) | 1–65535 |
| MySQL database | text | yes | non-empty, ASCII identifier |
| MySQL user | text | yes | non-empty |
| MySQL password | password (write-only) | yes on first save | non-empty |
| Schedule (local time) | text `HH:MM` | yes | 24h. Worker converts to UTC using the operator timezone for the APScheduler cron expression. Single daily fire. The form previews the next-fire-time in both local and UTC before save. |
| Catch-up cap (days) | int | no (default `$CATCH_UP_CAP_DAYS`) | 1–90 |

`POST /config` validation order (each step short-circuits on failure, returning a field-level error):

1. Form syntactic validation (Pydantic).
2. `socket.gethostbyname()` for both BlueWeb host and MySQL host.
3. `mysql.connect(...) ; SELECT 1` against MySQL.
4. `requests.head(bluewave_url, timeout=5)`.
5. A throwaway Selenium login + logout against BlueWeb.

Only after step 5 succeeds is the config persisted. Misconfiguration discovered at 03:00 by a cron miss is much more expensive than at form-submission time.

### 7.2 Authentication of the web UI

HTTP Basic with `WEB_USER` + `WEB_PASS_HASH` (L3). bcrypt verification on every request. The hash is generated once via:

```
docker run --rm bluewave-worker python -m bluewave.hashpw
# Prompts for password, prints "$2b$12$..." to stdout.
```

The operator pastes that into `WEB_PASS_HASH` at deploy. Plaintext passwords are not accepted in env.

### 7.3 CSRF

Synchronizer-token pattern on every state-changing POST. Token is signed via `itsdangerous` with the same Fernet key (separate purpose tag). Token rotated per session.

### 7.4 Anti-clickjacking

`X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'` on every response. The web UI does not render any external content.

---

## 8. Security posture — explicit acceptances

This section is more candid than the v0.1 draft because the locked decisions concentrate risk in the network layer.

### 8.1 What we protect

- `CONFIG_ENC_KEYS` is the only bootstrap secret. The encrypted SQLite file at rest leaks no credentials without it. Multiple keys supported for rotation (§8.4).
- Operator password is stored as a bcrypt hash, never in plaintext.
- Card numbers (`extra_data.card_number`) are excluded from log output at all levels. A test (M9) greps logs for known card numbers from the golden fixture and asserts zero hits.
- The Pin field from the CSV is dropped on the floor, never persisted, never logged.
- The MySQL user has only `INSERT, SELECT` on the audit DB (plus `CREATE` on the bootstrap pass — DBA may revoke after M2).
- The worker is read-only against BlueWeb. The selectors module has a denylist for `Emergency Lockdown` and any element classed `delete`/`remove`/`save`/`apply`.
- CSRF, anti-clickjacking, and Basic Auth gate every POST.

### 8.2 What we do not protect — explicit residual risks

| Risk | Decision | Compensating control |
|---|---|---|
| BlueWeb credentials traverse the LAN in plaintext (HTTP). | L4 — accepted. | Deployment requires the worker host, BlueWeb host, and any operator workstation reaching the worker UI to be on the same trusted private VLAN. Document on the deployment checklist. |
| Operator's web UI session traverses the LAN in plaintext (HTTP). | L13 — accepted. `WEB_ALLOW_HTTP=1` required. | Same as above. Operator browser must be on the trusted segment. Operator should not bookmark the URL on a laptop that ever leaves that segment. |
| MySQL credentials and query traffic traverse the LAN in plaintext. | L14 — accepted. | Same as above. The worker rejects any DSN that includes a non-RFC1918 host **unless** `MYSQL_HOST_ALLOW_PUBLIC=1` (an undocumented escape hatch not intended for production use). |
| A compromised operator workstation has both Basic Auth creds and network reachability. | Accepted. | Out of scope; the worker has only the surface defined in §7 and cannot be made to write outside `z_audit_logs_efk` for sources other than `"Bluewave"`. |
| BlueWeb password rotation. | Not automated. | Operator visits `/config`, supplies the new password; on save, the triple-probe runs and confirms before persisting. Old runs continue to show in the dashboard. |

These are not bugs — they are conscious tradeoffs. The mitigation is **network-segment trust**, not crypto. If that assumption ever weakens (e.g. the corporate network is flattened), revisit L4, L13, L14 together.

### 8.3 Defense-in-depth that *is* implemented despite the LAN posture

- bcrypt for the web password (a stolen `WEB_PASS_HASH` is still costly to crack).
- Fernet / MultiFernet for the at-rest config (a stolen volume snapshot leaks nothing without the env key).
- A non-root container UID.
- Tight selector denylist on Selenium so the worker cannot navigate to mutating BlueWeb pages even if exploited.

### 8.4 Key rotation (`CONFIG_ENC_KEYS`)

The worker uses `cryptography.fernet.MultiFernet` over the list of keys parsed from `CONFIG_ENC_KEYS`. Semantics:

- **Encryption** always uses the **first** (head) key in the list.
- **Decryption** is attempted with each key in order; first key that succeeds wins.

Rotation procedure (also documented in §13.7):

1. Generate a new key: `python -m bluewave.keygen`.
2. Prepend it to `CONFIG_ENC_KEYS` (so the env now contains `<new>,<old>`).
3. Restart the container. It can still decrypt existing data (the old key remains in the list) and now encrypts new writes with the new key.
4. Run `python -m bluewave.rotate_config` (one-shot CLI inside the container). It loads every encrypted column from `config.sqlite`, re-encrypts each with the new head key, and writes back in a single transaction.
5. After verification (the container still loads config, runs still work), drop the old key on the next deploy by setting `CONFIG_ENC_KEYS` to `<new>` alone.

Up to **4 keys** are supported in the env var; beyond that, the worker logs a warning at startup (likely a forgotten rotation).

The `key.rotated` self-audit row (§3.6.2) captures `old_key_fingerprint` and `new_key_fingerprint` — the first 8 chars of a SHA-256 of the public-facing key bytes. The keys themselves are never logged or written to the DB.

---

## 9. Observability

### 9.1 `/healthz`

Always a JSON body. Always one of 200 or 503.

**200 OK** when all of:

- Config is complete (at least one successful `POST /config`).
- Latest run is `ok`, OR is `running`, OR no run has happened yet but the container has been up less than `2 × schedule_interval`.
- Disk free on `/var/lib/bluewave-worker` is ≥ 100 MB.

Body shape (200):

```json
{
  "status": "ok",
  "uptime_seconds": 47215,
  "configured": true,
  "site_label": "Easy Foods Inc.",
  "operator_tz": "America/New_York",
  "next_run_at_utc": "2026-05-13T07:00:00Z",
  "next_run_at_local": "2026-05-13 03:00:00",
  "last_run": {
    "id": 142,
    "report_date": "2026-05-12",
    "status": "ok",
    "rows_inserted": 1093,
    "finished_at_utc": "2026-05-13T07:00:18Z"
  },
  "catchup_pending": 0,
  "disk_free_mb": 18432
}
```

**503** when any of:

- Config is incomplete.
- The last two **scheduled** (`manual=False`) runs both failed.
- The container has been up longer than `2 × schedule_interval` without any `ok` run.
- Schema drift detected at startup.
- Disk free on `/var/lib/bluewave-worker` is < 100 MB.

Body shape (503):

```json
{
  "status": "degraded",
  "reasons": ["last_two_scheduled_runs_failed"],
  "last_run": { "id": 144, "report_date": "2026-05-12", "status": "ingest_failed",
                "error_excerpt": "pymysql.err.OperationalError: (2003, ...)",
                "finished_at_utc": "2026-05-13T07:00:11Z" },
  "configured": true,
  "schema_ok": true,
  "disk_free_mb": 18432
}
```

External monitoring (L7) is expected to poll this endpoint.

### 9.2 Structured stdout logs

One JSON object per line. Required fields on every line: `ts` (ISO 8601 UTC), `level`, `event`, `run_id` (when applicable), `report_date` (when applicable).

Event catalog (closed set):

| `event` | Emitted at | Notable fields |
|---|---|---|
| `app.start` | uvicorn startup | `version`, `tz` |
| `app.config_loaded` | successful config load | `bluewave_url`, `mysql_host`, `schedule_local`, `tz` (no passwords) |
| `app.schema_ok` | schema validation passes | `columns_checked` |
| `app.schema_drift` | schema validation fails | `expected`, `actual` |
| `scheduler.fire` | daily cron fires | `next_run_at_utc` |
| `run.queued` | run row created | `manual`, `report_date` |
| `run.started` | `run_start()` enters | `report_date` |
| `run.step` | each S1–S9 boundary | `step` (`login`, `nav`, `report`, `csv`, `parse`, `insert`, `logout`), `elapsed_ms` |
| `run.parsed` | after transform | `rows_in_csv` |
| `run.inserted` | after sink | `rows_inserted`, `rows_duplicate` |
| `run.finished` | terminal | `status`, `duration_ms` |
| `run.failed` | terminal (failure) | `status`, `error_excerpt` (truncated to 500 chars; no card numbers) |
| `catchup.enqueued` | boot-time / button | `count`, `dates` |
| `http.request` | every request | `method`, `path`, `status`, `latency_ms` (NO query string in case of card numbers; `path` only) |
| `process.reaper` | startup | `killed` (count of orphaned chrome/chromedriver) |
| `bluewave.clock_drift` | post-login during a run | `drift_seconds` (signed; `null` if BlueWeb didn't expose a clock element) |
| `tz.dst_ambiguous` | row parse during a fall-back hour | `local_value`, `chosen_utc` |
| `tz.dst_nonexistent` | row parse fails on a spring-forward hour | `local_value` — followed by the run failing |
| `selfaudit.write` | every self-audit row written (§3.6) | `operation`, `dedup_hash` (first 12 chars) |
| `selfaudit.write_failed` | self-audit DB insert raises | `operation`, `error_excerpt` (non-fatal) |

No event ever emits `card_number`. A unit test (§10/M9) asserts the log schema against an enumeration.

### 9.2.1 Dashboard time-display rules (L23)

Every timestamp rendered in the web UI follows one rule: **`YYYY-MM-DD HH:MM:SS UTC`**. No relative ("3 hours ago"), no locale-formatted ("May 13, 2026 7:00 AM EDT"), no ambiguity. The dashboard footer shows two pieces of context so operators can mentally translate when they need to:

```
Site: Easy Foods Inc.   |   Operator TZ: America/New_York   |
Next run: 2026-05-13 03:00:00 local  /  2026-05-13 07:00:00 UTC
```

Within a row, both "started" and "finished" use UTC. A "duration" column shows `00:01:23` (HH:MM:SS) so the operator does not need to subtract timestamps in their head.

### 9.3 Screenshot retention

`/var/lib/bluewave-worker/screenshots/` is GC'd at startup and at the end of every run, keeping the **most recent 60 files** (sized for a worst case of ~60 days of one-failure-per-day). This bounds disk use even on a long-running pathology.

---

## 10. Milestones and pass criteria

Each milestone is a small, mergeable slice. Pass criteria are **objective and runnable** — every "verify" step is either a `pytest`, a `docker run` invocation, or a SQL query whose result is deterministic.

### M1 — Container skeleton + healthz scaffold

**Deliverables:**
1. `Dockerfile` building a `python:3.12-slim` image with Chromium + chromedriver (pinned apt versions), `tini`, non-root user `worker:10001`, single `EXPOSE 8080`, `HEALTHCHECK`.
2. A FastAPI app with `GET /healthz` only; returns 503 + `{"status":"unconfigured","reasons":["no_config"]}`.
3. `python -m bluewave.keygen` and `python -m bluewave.hashpw` CLI entry points.
4. `docker-compose.yml` for local dev, mounting `/var/lib/bluewave-worker` and `/tmp/bluewave-dl`.

**Pass criteria:**
- `docker build .` succeeds with no apt warnings about missing packages or expired signatures.
- `docker run -e CONFIG_ENC_KEY=… -e WEB_USER=admin -e WEB_PASS_HASH=… -e WEB_ALLOW_HTTP=1 …` boots and `/healthz` returns 503 with valid JSON parseable by `jq` and `status=unconfigured`.
- `docker run` **without** `WEB_ALLOW_HTTP=1` exits non-zero within 5 s with a stderr message naming the missing env var.
- `chromium --version` and `chromedriver --version` inside the container print compatible major versions (test asserts the integer majors are equal).
- `docker inspect` confirms the runtime UID is `10001` (not 0).
- `python -m bluewave.keygen` prints a 44-char base64 string; running it twice produces two distinct strings.

### M2 — MySQL schema bootstrap

**Deliverables:**
1. `bluewave/schema.py` containing both DDL strings from §3.1 and §3.4.
2. A startup hook that:
   - Connects to MySQL using config from `config.sqlite`.
   - Runs `CREATE TABLE IF NOT EXISTS` for both tables.
   - Compares `information_schema.COLUMNS` for `z_audit_logs_efk` against the expected set; on mismatch, marks `schema_ok=False` and surfaces `schema_drift` in `/healthz`.
3. `tests/test_schema.py` spinning up a fresh MySQL via `pytest-docker` (or `testcontainers`).

**Pass criteria:**
- Bootstrap is idempotent. Running it twice against a populated DB produces zero new objects (`SHOW CREATE TABLE` byte-for-byte identical before and after).
- Inserting the same `dedup_hash` twice via the §3.5 idiom: `SELECT COUNT(*) FROM z_audit_logs_efk WHERE dedup_hash = ?` returns 1.
- A test that pre-creates `z_audit_logs_efk` with a renamed column (`userid` instead of `user_id`) causes startup to set `schema_ok=False`. `/healthz` returns 503 with `reasons=["schema_drift"]`.
- A test that pre-creates `z_audit_logs_efk` with the legacy 3-byte `utf8` charset (instead of `utf8mb4`) causes `schema_ok=False` with `reasons=["schema_drift"]`. The drift diff explicitly names the charset / collation mismatch.
- A MySQL user granted only `INSERT, SELECT` (no `CREATE`) cannot bootstrap a fresh DB — the bootstrap raises a privilege error and `/healthz` reports `reasons=["mysql_privilege"]`. (Documents the DBA's responsibility.)
- The `ok_scheduled_date` generated column behaves as designed: two rows with `(source='Bluewave', report_date='2026-05-12', status='ok', manual=0)` cannot coexist; one with `manual=1` can coexist with one `manual=0`.
- A unit test verifies that `pymysql.connect()` is called with **keyword arguments** (host/port/user/password/database/charset) — never with a URL DSN. (Defends against a future regression where a password with `@` or `/` would break DSN parsing.)

### M3 — Selenium login + selector catalog

**Deliverables:**
1. `bluewave/driver.py` per §6.1.
2. `bluewave/selectors.py` recording every CSS / XPath used, with a one-line comment per entry citing the page element. Includes a `DENYLIST` set containing the Emergency Lockdown element.
3. `bluewave/login.py` performing S1 (login) and S2 (click Reports). On success, screenshot to disk and exit 0. On failure, screenshot and exit non-zero with the §6.4.5 status string.

**Pass criteria:**
- Against a live BlueWeb instance with correct credentials, `python -m bluewave.login` exits 0 within 30 s. A follow-up assertion captures `driver.page_source` and asserts the substring `Choose Report:` is present.
- Against wrong credentials, exits non-zero within 30 s with `status=auth_failed`; screenshot shows the login form still visible.
- Against an unreachable host, exits non-zero within 10 s with `status=nav_failed`.
- Every selector in `selectors.py` is referenced by exactly one call site (greppable; CI test enforces).
- Test that constructs a fake page with an `Emergency Lockdown` button and asserts no scrape code path can resolve a clickable element matching it.

### M4 — Full Selenium flow + CSV capture

**Deliverables:**
1. `bluewave/scrape.py` performing S3–S7 end-to-end given a `report_date`.
2. On success, returns the local CSV path.
3. Cleanup on failure (driver quit, partial file removed).

**Pass criteria:**
- Given a working instance and `report_date = today_local - 1`, exits 0 within 90 s, producing a file at the documented path whose first line is exactly the header `"Date/Time","Door Name","Description","Person","Employee ID","Card Number","Facility Code","Pin"`.
- The file decodes as UTF-8 without error.
- On a known-empty day (no events), the worker returns a "no rows" outcome that maps to `status='ok'` with `rows_in_csv=0` (§6.4.2). Verified by either choosing a date BlueWeb has data for and then a date it doesn't.
- A simulated mid-flow failure (driver killed before S7) leaves no `.crdownload` artifacts after cleanup; the download dir is empty.

### M5 — CSV → rows transform (pure function)

**Deliverables:**
1. `bluewave/transform.py` exposing `transform(csv_path, *, timezone) -> list[Row]`.
2. `bluewave/dedup.py` with `canonical_extra_data` and `dedup_hash`.
3. `tests/golden/2026-05-12.csv` (the supplied sample) and `tests/golden/2026-05-12.expected.json` of every transformed row.

**Pass criteria:**
- `transform("tests/golden/2026-05-12.csv", timezone="America/New_York")` equals `tests/golden/2026-05-12.expected.json` byte-for-byte after canonical-JSON serialization.
- Calling `transform` on the same input twice yields lists with element-wise identical `dedup_hash` values.
- A row with `Employee ID = " "` (single space) yields `user_id=None`.
- A row with both `Card Number` and `Facility Code` empty yields `extra_data=None` and `extra_data_str=""`.
- A row whose `Person` contains an embedded comma + unicode (real example: `"Atencio-Añez, Juan J"`) is preserved verbatim in `user_name`.
- A malformed `Date/Time` raises `MalformedCsvError`; partial output is never returned.
- For 100 randomly-generated rows, `canonical_extra_data` produces strings whose keys are alphabetically ordered and whitespace-free.
- **DST fall-back test:** a synthetic CSV with two rows at `11/01/2026 01:30:00` (the duplicated hour in `America/New_York`) yields two distinct UTC timestamps, both using `fold=0` semantics; the earlier UTC time is chosen for both rows (deterministic). A `tz.dst_ambiguous` log event is emitted for each.
- **DST spring-forward test:** a synthetic CSV with a row at `03/08/2026 02:30:00` (non-existent in `America/New_York`) causes `transform` to raise `MalformedCsvError` with a message naming the offending row and timestamp.

### M6 — Idempotent DB writes

**Deliverables:**
1. `bluewave/sink.py` inserting transformed rows with the §3.5 idiom in batches of 500, wrapped in a single transaction per run.
2. `bluewave/runs.py` managing the `z_audit_logs_efk_runs` row lifecycle.
3. `tests/test_sink.py` covering insert / re-insert / partial-conflict against a transient MySQL.

**Pass criteria:**
- Fresh DB + golden CSV → exactly `len(golden_rows)` rows in `z_audit_logs_efk`, one `z_audit_logs_efk_runs` row with `status='ok'`, `rows_inserted=len(golden_rows)`, `rows_duplicate=0`.
- Re-running same CSV → `rows_inserted=0`, `rows_duplicate=len(golden_rows)`; `SELECT COUNT(*)` on `z_audit_logs_efk` unchanged.
- Modify one row's `operation` and re-run → `rows_inserted=1`, `rows_duplicate=len(golden_rows)`.
- Killing the worker mid-insert (raise after batch *k*) leaves zero rows for that run and the `ingest_run` row with `status='ingest_failed'`. Verified by manual injection plus rollback assertion.
- A successful run for `(source='Bluewave', report_date='2026-05-12', manual=0)` followed by a second scheduled attempt for the same date raises a duplicate-key error against `uk_ok_scheduled_date`; the second attempt is caught and marked `status='skipped'`.

### M7 — Web GUI (config, dashboard, run-now, single-date backfill)

**Deliverables:**
1. All routes in §7 except `/run/catchup`, including Basic-Auth gate and CSRF.
2. MultiFernet-encrypted SQLite config store, parsing `CONFIG_ENC_KEYS` as a comma-separated list.
3. The triple-probe on `POST /config`.
4. The two stateless test routes `POST /config/test/bluewave` and `POST /config/test/mysql`.
5. Dashboard with site-label header, operator-TZ footer, and pagination (`?limit&offset`) on the runs list.
6. `/docs` mounted iff `LOG_LEVEL=DEBUG`, behind the same Basic Auth gate.

**Pass criteria:**
- Fresh container, no config: `/healthz` 503 (`unconfigured`); dashboard shows a banner pointing to `/config`.
- Saving config with deliberately wrong BlueWeb password: field-level error, **no** config row persisted (verified by checking `config.sqlite` is unchanged).
- After successful save, `GET /config` renders the form with the password as a write-only placeholder; reading `config.sqlite` directly shows ciphertext.
- The site label entered in the form (e.g. `Easy Foods Inc.`) appears verbatim in the dashboard header and in the `/healthz` JSON `site_label` field.
- Every rendered timestamp on the dashboard matches the pattern `\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC` — no locale formatting, no relative times. Asserted by an end-to-end test that scrapes the rendered HTML.
- `POST /run` while no run is in-flight: new run row within 2 s. Second `POST /run` within the same second: HTTP 409.
- **`POST /run {"report_date":"<today_local>"}`: HTTP 400 with body naming the rule.**
- **`POST /run {"report_date":"<today_local + 1>"}`: HTTP 400.**
- `POST /run {"report_date":"YYYY-MM-DD"}` outside `BACKFILL_SAFETY_CAP_DAYS`: HTTP 400.
- `POST /config/test/bluewave` with correct creds returns `{"result":"ok"}` and writes a `config.test_bluewave` row to `z_audit_logs_efk` with `source='bluewave-worker'` (verifies M7's self-audit hookup).
- `POST /config/test/mysql` against a `utf8` (legacy) DB returns `{"result":"failed","detail":"…charset…"}`.
- Changing the schedule field: APScheduler's next-fire-time reflects the new value within 60 s. Changing the operator timezone field (same form submission) re-translates the schedule's wall-clock time correctly — verifiable by inspecting `scheduler.get_jobs()`.
- A request without valid Basic Auth: HTTP 401, no CSRF token issued.
- A POST without a valid CSRF token: HTTP 403.
- `GET /docs` returns 404 when `LOG_LEVEL=INFO`. Returns 200 (the Swagger UI) when `LOG_LEVEL=DEBUG` AND valid Basic Auth is supplied. Returns 401 when `LOG_LEVEL=DEBUG` without auth.
- `GET /runs?limit=5&offset=10` returns rows 11–15 in stable order; `GET /runs` returns the most recent 20 by default. `GET /runs` with `Accept: application/json` returns a JSON array.

### M8 — Scheduler + catch-up

**Deliverables:**
1. APScheduler firing `run_job(report_date=yesterday_local)` at the configured local time, persisted across container restarts (jobstore = the same encrypted SQLite).
2. Boot-time catch-up routine (§6.4.3) enqueuing missing days (`manual=False`).
3. `POST /run/catchup` endpoint.
4. Catch-up UI button on dashboard.

**Pass criteria:**
- A test schedule set to "5 minutes from now" fires within ±15 s. Three consecutive ticks at 5-minute intervals fire within ±15 s each, with drift logged but not accumulating.
- Boot-time catch-up: pre-seed `z_audit_logs_efk_runs` with `ok` rows for dates D-1 and D-3 only. Start container; assert that runs for D-2, D-4, D-5, …, D-14 are enqueued in chronological order, and D-1 / D-3 are skipped.
- Boot-time catch-up does not enqueue today's date — today is not "yesterday or older" yet.
- `POST /run/catchup` while a run is in flight: returns 202 with a non-empty `enqueued` list (for dates not already in the queue); enqueued runs are processed after the in-flight run completes.
- **Catch-up idempotency:** with 8 missing dates and a backfill in progress, two rapid clicks of "Catch up missing" result in exactly 8 queue entries, not 16. The second response carries `enqueued=[]` and `skipped_already_queued` listing all 8 dates.
- Container restart mid-run: on next boot, the prior run row is found in `running` state and reaper-flipped to `ingest_failed` with `error_excerpt='reaped on boot'`. Catch-up then enqueues that date again.

### M9 — Hardening, observability assertions, soak

**Deliverables:**
1. Process-table audit: every run ends with zero orphan chrome/chromedriver processes inside the container.
2. Log line schema documented in `docs/logs.md`; `tests/test_logs.py` parses captured stdout from a full run, asserts every required event in §9.2 appears in order, and asserts no card number from the golden fixture appears anywhere.
3. Screenshot retention GC.
4. A 7-day soak on a live BlueWeb instance.
5. README + operator runbook covering §13.

**Pass criteria:**
- 7/7 scheduled runs complete with `status='ok'`. Any failure has a documented post-mortem in `error_excerpt` and a follow-up issue.
- For one date in the soak window, `SELECT COUNT(*) FROM z_audit_logs_efk WHERE source='Bluewave' AND DATE(timestamp) = <date>` equals `wc -l < BlueWeb-Reports.csv - 1` exactly. (Flat-file source, no out-of-order delivery — drift here is a bug.)
- `pgrep -c -f 'chrome|chromedriver'` returns 0 inside the container at least 5 s after each run finishes.
- `grep -F "$known_card_number" /proc/1/fd/1` (the container's stdout) returns no matches across the entire 7-day window.
- `/healthz` returns 503 within 60 s of two consecutive scheduled failures and returns 200 within 60 s of the next `ok`.
- The screenshots directory contains at most 60 files at any time, even after deliberately failing 100 runs.
- **MultiFernet rotation:** start the container with `CONFIG_ENC_KEYS=<key1>`. Save a config. Stop, set `CONFIG_ENC_KEYS=<key2>,<key1>`, start. Verify the existing config still loads (decrypted via the old key). Save a new value; verify it is encrypted with `<key2>` (asserted by attempting to decrypt with `<key1>` alone — must fail). Run `python -m bluewave.rotate_config`; verify all encrypted columns decrypt with `<key2>` alone after restart with `CONFIG_ENC_KEYS=<key2>`. A `key.rotated` row appears in `z_audit_logs_efk`.
- **Self-audit completeness:** every operator action during the soak (config saves, run-nows, backfills, catch-ups, both test-connection buttons) appears as exactly one row in `z_audit_logs_efk` with `source='bluewave-worker'`. Container boot + shutdown also appear. No card number from the golden fixture ever appears in any self-audit `extra_data` JSON.
- **SBOM presence:** the built image's CycloneDX SBOM is fetched via `docker buildx imagetools inspect --raw <digest>` and contains entries for `chromium`, `chromium-driver`, and every Python dep in `requirements.txt`.
- **TZ env guard:** starting the container with `TZ=America/New_York` exits non-zero within 5 s with a stderr message naming the violated invariant.

---

## 11. Residual open decisions

Most of the v0.1 decisions are now locked (§0.2). What remains are smaller policy choices and v2 considerations.

| # | Decision | Options & tradeoffs | Blocks |
|---|---|---|---|
| D1 | **Multiple BlueWeb instances in v2** | When/if a second BlueWeb host appears, do we (a) run two container instances each writing `source='Bluewave'` to separate DBs, (b) parameterize the source string (`Bluewave/<label>`), or (c) merge writes into one DB and disambiguate via `instance`-derived columns? Today: single instance per L10. | v2 only |
| D2 | **Pipe-character handling in dedup_hash** | No observed source value contains `|`. If a real collision is ever reported, (a) escape (`|` → `\|`) and bump a hash version, or (b) switch to a length-prefixed framing. Decision deferred until the case is real. | nothing today |
| D3 | **Manual backfill of a date that already has an `ok` scheduled run** | (a) Allow — the new manual run is also `ok`, both rows persist, dedup_hash prevents duplicate row creation. (b) Refuse with a clear UI message. (Recommended: a — manual runs are an explicit operator action.) | M8 if (b) chosen |
| D4 | **DB-side `source` lock** | Should `z_audit_logs_efk` enforce that this worker only inserts rows with `source='Bluewave'`? (a) No, trust the application (current). (b) MySQL trigger checking the writer's user matches the source. Adds operational coupling to the shared table; rejected unless other peer writers also adopt it. | informational |
| D5 | **Retention of `z_audit_logs_efk` data** | This worker does not delete from the shared table. Who does, on what cadence? Cross-module decision; belongs in a separate document. v1 of this worker is append-only by design. | informational |
| D6 | **Future move to TLS** | If the corporate network ever flattens (or this worker is ever deployed across segments), L4 / L13 / L14 must all be reconsidered together. Spec a TLS migration plan ahead of need? Currently: no. | future trigger |
| D7 | **Multi-operator support** | Single Basic Auth user covers v1. If multiple operators ever need distinct identities (for audit / accountability of who clicked Run Now), the route is OIDC against a corporate identity provider. | v2 only |

Until D3 is recorded, M8 has a small ambiguity (does manual=backfill on an `ok` date succeed or 409?). Default to (a) unless decided otherwise.

---

## 12. Non-goals reminder (anti-scope)

Repeating because they will tempt scope creep:

- No real-time / streaming pull.
- No reports other than the Event report.
- No BlueWeb mutations (people, doors, holidays, lockdown).
- No data-quality fix-ups for operator-entered Person names.
- No alerting / paging — operator polls `/healthz` or watches the dashboard.
- No second container, microservices, message queue.
- No multi-tenant / multi-instance. Multiple BlueWeb hosts = multiple container deployments (v2).
- No retention enforcement on the shared audit table.
- No TLS anywhere (BlueWeb, web UI, MySQL). Network-segment trust is the perimeter (L4, L13, L14).
- No automated credential rotation — operator-managed via the web UI.
- No IaC-driven bootstrap. Web UI is the only configuration surface in v1.

Any of these can be a v2 conversation.

---

## 13. Operator runbook (lives in `README.md`, summarized here)

### 13.1 First deploy

1. `python -m bluewave.keygen` on the worker host → store output safely as `CONFIG_ENC_KEYS` (single value; can hold multiple later).
2. `python -m bluewave.hashpw` → enter chosen operator password → store hash as `WEB_PASS_HASH`. Decide `WEB_USER`.
3. DBA creates MySQL DB (e.g. `audit`) and user `audit_writer` with `CREATE, INSERT, SELECT` on `audit.*`. The DB must be created with `CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`. (DBA may revoke `CREATE` after the first successful boot.)
4. Confirm host NTP on the worker host (and on the BlueWeb host). Container clock = host clock = UTC.
5. Confirm: worker host can reach BlueWeb host (HTTP) and MySQL host (TCP 3306) on the private network. Confirm none of the three are on a public segment.
6. Pin the worker image digest in `docker-compose.yml` (not a mutable tag). Optionally inspect the SBOM (§13.7).
7. `docker compose up -d`. Verify `/healthz` returns 503 `unconfigured` from the operator workstation.
8. Open `http://worker-host:8080/`. Authenticate. Open `/config`. Fill in: site label, BlueWeb URL/user/password, operator timezone, MySQL host/port/db/user/password, schedule (HH:MM in operator TZ). Click "Test BlueWeb" and "Test MySQL" first to validate. Click Save.
9. On successful save, `/healthz` returns 200 with `last_run=null` and `next_run_at_utc`/`next_run_at_local` populated. The first scheduled fire at the configured local time inserts yesterday's data. Boot-time catch-up will also enqueue any missing days within the cap.
10. Verify by visiting the dashboard after the next scheduled fire.

### 13.2 Password rotation (BlueWeb)

1. Operator visits `/config`, supplies the new password, clicks Save.
2. The triple-probe (including a Selenium login) runs before persistence; a wrong new password fails the save and the old config remains intact.
3. After save, subsequent runs use the new password. No restart required.

### 13.3 Operator password rotation (web UI)

1. Operator generates a new `WEB_PASS_HASH` via `python -m bluewave.hashpw`.
2. Update the container's env (e.g. `docker compose up -d --force-recreate`).
3. The web UI session is invalidated; the operator re-authenticates with the new password.

### 13.4 Schema drift recovery

If another module ever alters `z_audit_logs_efk` in a backward-incompatible way:

1. `/healthz` returns 503 with `reasons=["schema_drift"]`. The dashboard shows the expected-vs-actual diff.
2. Operator and DBA reconcile schema. Either roll the DDL back, or update this worker's expected-columns set (a code change here).
3. After reconciliation, container restart (or hit a debug endpoint) re-runs schema validation. `/healthz` returns 200.

### 13.5 Catch-up after extended downtime

1. Container has been down for > 14 days (the default `CATCH_UP_CAP_DAYS`).
2. On boot, auto-catch-up enqueues the most recent 14 days. Older gaps remain.
3. To fill them, operator either (a) raises `CATCH_UP_CAP_DAYS` via env and restarts, or (b) uses the single-date Backfill UI per missing day (up to `BACKFILL_SAFETY_CAP_DAYS=365`, bounded by BlueWeb's actual retention).

### 13.6 Container upgrade

1. Update the digest pinned in `docker-compose.yml` to the new image (see §13.7 for SBOM inspection of the candidate).
2. `docker compose pull && docker compose up -d`.
3. SIGTERM is propagated; uvicorn drains in-flight requests; APScheduler finishes any active run within the 60 s grace, or the run is reaped on next boot.
4. The `/var/lib/bluewave-worker` volume survives the recreate; config persists; run history persists in MySQL.

### 13.7 Inspect the image SBOM before upgrade

```
docker buildx imagetools inspect --format '{{ json .SBOM }}' \
    registry.local/bluewave-worker@sha256:<new-digest> | jq .
```

Confirm `chromium`, `chromium-driver`, and the Python deps from `requirements.txt` are present with the expected versions. If a major Chromium version jump appears, expect possible selector breakage — run M3's smoke test against staging before promoting.

### 13.8 Rotate the config encryption key

1. On the worker host: `python -m bluewave.keygen` → new 44-char key.
2. Update `CONFIG_ENC_KEYS` in the env to `<new>,<old>` (new key **first**).
3. `docker compose up -d` to restart with the updated env. The container still decrypts existing data with `<old>`; new writes use `<new>`.
4. `docker compose exec bluewave-worker python -m bluewave.rotate_config`. This re-encrypts every encrypted column in `config.sqlite` with the new head key. The command writes a `key.rotated` self-audit row on success.
5. Verify `/healthz` returns 200, sign in to the dashboard, run a Test BlueWeb / Test MySQL.
6. On the next deploy, set `CONFIG_ENC_KEYS=<new>` alone and `docker compose up -d`.
7. If step 4 fails for any column, **do not drop the old key**. The MultiFernet decrypt fallback to `<old>` keeps the container running. Re-run after the underlying issue is fixed.

### 13.9 Restore from a lost `CONFIG_ENC_KEYS`

If the operator loses every key, `config.sqlite` becomes unreadable. The MySQL data in `z_audit_logs_efk` is untouched.

1. Stop the container.
2. Remove `config.sqlite` from the mounted volume.
3. Generate a new key, set `CONFIG_ENC_KEYS=<new>`.
4. Start the container; it enters the unconfigured state.
5. Re-enter all configuration via the web UI. Run history and ingested audit rows survive.

---

## Appendix A — Risk register (concise)

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| BlueWeb HTML / selector changes after an upgrade | medium | high (worker stops ingesting) | `selectors.py` is the single change-surface; M3 catalogs every selector; failure shows in `/healthz` within one day. |
| BlueWeb session expires mid-run | low | low | Fresh driver per run; one run is short. |
| MySQL host unreachable | medium | high (no ingest until restored) | Run marks `ingest_failed`; cursor (`ok_scheduled_date`) does not advance for that date; catch-up re-runs it next time. |
| Disk full on worker volume | low | medium | `/healthz` includes `disk_free_mb`; threshold 100 MB → 503; screenshot retention is bounded. |
| Container clock drift | low | medium (schedule fires off-time) | Operator must run host NTP; documented in §13.1 prerequisites. |
| Operator typo in BlueWeb password | high | low | Triple-probe on save catches it; old config retained on failure. |
| Card-number leakage in logs | low | medium | M9 grep test; closed log-event catalog. |
| BlueWeb retention shorter than catch-up cap | medium | medium (some days yield empty reports — recorded as `ok` with 0 rows, which is by design) | `report_date_local` older than retention yields an empty report; the gap is permanent. Document `BACKFILL_SAFETY_CAP_DAYS` as a soft upper bound. |
| Selenium download stuck in `.crdownload` | low | medium | S7 has a 60 s wait, asserts no `.crdownload` suffix; failure surfaces as `download_failed`. |
| Operator forgets every key in `CONFIG_ENC_KEYS` | low | high (config DB unreadable) | §13.9 recovery procedure: wipe `config.sqlite`, generate a new key, reconfigure via web UI. Ingested data in MySQL survives. MultiFernet (§8.4) makes accidental single-key loss recoverable as long as one historical key is retained. |
| DST fall-back (ambiguous local hour) | annual | low | `fold=0` rule documented (§5.1.1); deterministic; `tz.dst_ambiguous` event logged each occurrence. |
| DST spring-forward (non-existent local hour in a CSV row) | very low | medium (run fails) | Run fails loud as `parse_failed` with a `tz.dst_nonexistent` log event. Operator investigates BlueWeb host clock. |
| Operator changes operator-timezone after data exists | low | low | Existing rows are UTC and unaffected. APScheduler job is re-registered in the same `POST /config` transaction; next-fire-time updates within seconds. |
| BlueWeb host clock drifts from operator timezone | medium | medium (rows offset by drift) | Post-login drift check (§6.3.1) logs `bluewave.clock_drift` warning when |drift| > 5 min. Operator investigates. |
| `TZ` env var accidentally set to non-UTC | low | high (internal time math wrong) | Container refuses to start; clear stderr message. Asserted in M9. |
| Schema charset is legacy `utf8` (3-byte), silently truncates `ñ` and similar | medium (first deploys) | high (data loss) | Startup schema check fails with `schema_drift` naming the charset mismatch. DBA recreates the DB with `utf8mb4 / utf8mb4_unicode_ci`. |
| Operator double-clicks "Catch up missing" | high | nil | Queue dedupes against in-flight + queued dates; second click's response shows `enqueued=[]`. |
| Self-audit insert fails (e.g. MySQL down) | medium | low | Non-fatal; operator action still completes; `selfaudit.write_failed` logged at WARNING. |

---

## Appendix B — References

- [Andrej Karpathy coding skills (forrestchang/andrej-karpathy-skills)](https://github.com/forrestchang/andrej-karpathy-skills)
- [Selenium 4 Python — Chrome options](https://www.selenium.dev/documentation/webdriver/browsers/chrome/)
- [Chromium headless `--headless=new`](https://developer.chrome.com/docs/chromium/new-headless)
- [Chrome DevTools `Page.setDownloadBehavior`](https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-setDownloadBehavior)
- [APScheduler 3 docs](https://apscheduler.readthedocs.io/en/3.x/)
- [Python `cryptography` Fernet](https://cryptography.io/en/latest/fernet/)
- [MySQL 8 — `JSON` data type](https://dev.mysql.com/doc/refman/8.0/en/json.html)
- [MySQL 8 — Generated columns](https://dev.mysql.com/doc/refman/8.0/en/create-table-generated-columns.html)
- [RFC 4180 — Common Format and MIME Type for CSV Files](https://datatracker.ietf.org/doc/html/rfc4180)
