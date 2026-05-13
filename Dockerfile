# syntax=docker/dockerfile:1.7
#
# BlueWeb audit ingest worker — container image.
# See BLUEWEB_AUDIT_INGEST_SPEC.md §4.3.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Apt deps: Chromium + driver for Selenium (M3+), tini for signal handling,
# tzdata so ZoneInfo lookups work, ca-certificates for HTTPS to MySQL if ever
# enabled, fonts for legible Chromium screenshots, curl for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        tini \
        tzdata \
        ca-certificates \
        fonts-dejavu-core \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root user (spec §4.3 — UID 10001).
RUN groupadd --system --gid 10001 worker \
 && useradd  --system --uid 10001 --gid 10001 \
        --home-dir /home/worker --create-home worker

# State + ephemeral dirs.
RUN mkdir -p /var/lib/bluewave-worker/screenshots /tmp/bluewave-dl /app \
 && chown -R worker:worker /var/lib/bluewave-worker /tmp/bluewave-dl /app

WORKDIR /app

# Install Python deps as root, then drop privs.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=worker:worker bluewave/ /app/bluewave/

USER worker:worker

EXPOSE 8080

# /healthz returns 200 when configured + healthy, 503 otherwise.
# At M1 the container has no persistence layer, so it stays "unhealthy"
# until M7 lands. This is by design — see spec §10/M1.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bluewave.web"]
