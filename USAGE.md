# BlueWeb Ingest Worker Usage Guide

This document explains the architecture, databases, methods, installation, and day-to-day operations of the **BlueWeb Ingest Worker** (`bluewave-worker`). It is intended to serve as a handoff manual and operational guide.

---

## 1. System Overview

The BlueWeb Ingest Worker is a dockerized Python application designed to:
1. Run a headless **Selenium + Chromium** browser session to log into a local *BlueWeb Access Control Administration v20* instance.
2. Download the daily Event report CSV for the previous day (or a selected backfill date).
3. Parse, sanitize, and transform the CSV rows.
4. Idempotently insert the parsed event logs into a shared MySQL/MariaDB audit database table named `z_audit_logs_efk`.
5. Maintain its own operational log and run history in the database.
6. Serve an operator-facing web interface (port `8080`) for configuration, manual triggers, and health monitoring.

### Architecture

```
                                  ┌──────────────────────────────────┐
┌──────────────────────────┐      │   BlueWeb v20 Host (LAN)         │
│  Worker Host (Linux)     │  ──▶ │   HTTP (no TLS) on a known port  │
│  Docker Daemon           │      │   Access Control Administration  │
│   └─ bluewave-worker     │      └──────────────────────────────────┘
│        container         │
│        :8080  ◀── Operator Browser (HTTP, private network only)
│                          │      ┌──────────────────────────────────┐
│                          │ ──▶  │   MySQL Host (LAN)               │
│ ─────────────────────────┘      │   :3306 Plaintext                │
└──────────────────────────┘      │   Database: audit                │
                                  │   Table:    z_audit_logs_efk     │
                                  └──────────────────────────────────┘
```

---

## 2. In-Container Processes & Concurrency

Inside the single docker container, the system uses two main concurrent structures in a single Python process:
- **FastAPI / Uvicorn**: Serves the web dashboard, OpenAPI documentation, and `/healthz` endpoints.
- **APScheduler**: Manages the scheduled daily trigger based on the configured timezone and time.

To ensure stability and simplicity:
- **Run Lock**: A Python `threading.Lock` ensures that at most one execution run (scheduled or manual backfill) happens at any given moment. `POST` requests to trigger runs when a run is active return `409 Conflict`.
- **MySQL Connection Model**: The worker establishes a **fresh connection** (`pymysql.connect()`) per run execution or web dashboard load and closes it immediately after. This avoids stale connection errors and timeout issues.

---

## 3. Database Schemas & Tables Used

### 3.1 Shared Audit Log Table (`z_audit_logs_efk`)
The worker acts as a peer writer to this shared table. It creates it on boot if it does not exist but **never** modifies its columns or schema dynamically. If the schema has drifted (differing collation, charset, or column structures), the worker reports `schema_drift` and stops writing.

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

#### Self-Audit Logging
The worker also writes its own operational events to `z_audit_logs_efk` using `source='bluewave-worker'` (e.g., operator logins, config saves, key rotations, container boot).

### 3.2 Operational Run Tracker (`z_audit_logs_efk_runs`)
Used exclusively by this worker to track historical status, rows processed, duplicate counts, and error metadata.

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
  ok_scheduled_date DATE GENERATED ALWAYS AS
    (CASE WHEN status='ok' AND manual=0 THEN report_date ELSE NULL END) VIRTUAL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ok_scheduled_date (source, ok_scheduled_date),
  KEY ix_source_date (source, report_date),
  KEY ix_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 3.3 Local SQLite Config (`config.sqlite`)
Created inside the container volume path (`/var/lib/bluewave-worker/config.sqlite`). It holds a single-row settings table. Columns storing credentials (like the BlueWeb operator password and MySQL password) are encrypted using Fernet keys provided by the environment variable `CONFIG_ENC_KEYS`.

---

## 4. Key Logic & Handling

### 4.1 Timezone & DST Handling
BlueWeb emits naive local timestamps. The worker converts these timestamps to UTC based on the operator's configured timezone:
- **Ambiguous Hours (Fall-back)**: When time rolls back (e.g., standard to daylight time), the worker forces `fold=0` to resolve the naive hour to the earlier UTC instance.
- **Non-existent Hours (Spring-forward)**: If a time falls into the skipped spring-forward window, the worker throws a `MalformedCsvError` and fails the run rather than guessing.

### 4.2 Deduplication Logic
To prevent duplicate logs from being entered on repeated scraper runs or catch-ups, the worker generates a unique, deterministic hash `dedup_hash` for each row before writing to MySQL:

```python
# Hash Input Fields Concatenation:
payload = "|".join([
    "Bluewave",
    formatted_utc_timestamp, # YYYY-MM-DDTHH:MM:SS.000Z
    operation,               # stripped
    instance,                # stripped
    user_id or "",           # stripped
    user_name or "",         # stripped
    canonical_extra_data_json or "", # alphabetical JSON keys, no spaces
]).encode("utf-8")
dedup_hash = hashlib.sha256(payload).hexdigest()
```

MySQL uses `ON DUPLICATE KEY UPDATE id=id` to silently ignore rows with existing `dedup_hash` strings while executing batch inserts.

---

## 5. Installation & Deployment

### 5.1 Prerequisites
Ensure the host has the following:
- **Docker** and **Docker Compose v2** installed.
- A running Docker daemon.
- `curl` available.
- Network routing permissions to contact the target BlueWeb LAN server and MySQL database.

### 5.2 Step-by-Step Installation Wizard
1. Clone the repository onto the worker server.
2. Run the interactive deployment wizard script:
   ```bash
   ./deploy.sh
   ```
   The script walks you through:
   - Verifying dependencies.
   - Building the Docker image (`bluewave-worker:dev`).
   - Prompting for env vars (generating encryption keys, operator user/pass hash, setting log level, catch-up capacities).
   - Writing the `worker.env` file.
   - Starting the containers.

3. **Unattended Deployment**: If you want to bypass the prompts (e.g., in CI/CD or automated setups), provide the operator credentials as environment variables:
   ```bash
   WEB_USER=admin WEB_PASS='my-strong-pass' ./deploy.sh
   ```

### 5.3 Post-Deploy Configuration
1. After deployment, the container is up but unconfigured.
2. Navigate to **http://localhost:8080/config** (or your mapped host port).
3. Log in with the operator credentials set during deployment.
4. Input the BlueWeb UI URL, username, password, target MySQL database connection credentials, and local timezone/schedule.
5. Use the **Test BlueWeb** and **Test MySQL** buttons to confirm connectivity before saving.

---

## 6. Daily Operations Cheat Sheet

| Task | Command / Action |
|---|---|
| **Check Health & Status** | `curl -s http://localhost:8080/healthz \| jq .` |
| **Inspect Container Logs** | `docker compose logs -f bluewave-worker` |
| **Restart Ingest Worker** | `docker compose restart bluewave-worker` |
| **Stop Daemon** | `docker compose down` |
| **Force Upgrade/Rebuild** | `docker compose pull && docker compose up -d` |
| **Rotate Configuration Key** | See details below |

### Config Key Rotation
When rotating cryptographic encryption keys (`CONFIG_ENC_KEYS` env variable):
1. Prepend a new Fernet key to the comma-separated list of `CONFIG_ENC_KEYS` in your env settings.
2. Run the rotation tool in the container to re-encrypt SQLite passwords:
   ```bash
   docker compose exec bluewave-worker python -m bluewave.rotate_config
   ```
3. Remove the old key from the end of the `CONFIG_ENC_KEYS` variable and restart the container.

---

## 7. Running Tests

To run the unit and integration tests locally:
1. Initialize a Python virtual environment and install dev dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-dev.txt
   ```
2. Execute pytest:
   ```bash
   pytest
   ```
   *Note: Tests requiring a running Docker daemon (for MySQL testcontainers) or live BlueWeb credentials will automatically skip if those environments are missing.*
