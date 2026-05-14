#!/usr/bin/env bash
# Step-by-step deploy wizard for bluewave-worker.
#
# Builds the image (via plain `docker build` because `docker compose build`
# interpolates required-env :? guards even at build time), then walks every
# environment variable interactively. If $ENV_FILE already exists, each
# value is offered for reuse — press Enter to keep, type to change.
#
# Usage:
#   ./deploy.sh
#
# Unattended:
#   WEB_USER=admin WEB_PASS='strong-pass' ./deploy.sh   # only first-time
#
# Requirements: docker (Engine 20.10+) with compose v2, bash 4+, curl.

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env"
IMAGE_TAG="bluewave-worker:dev"
SERVICE_NAME="bluewave-worker"
WORKER_URL="${WORKER_URL:-http://localhost:8080}"
HEALTHZ_TIMEOUT_S="${HEALTHZ_TIMEOUT_S:-60}"

# ---------- ui helpers (all to stderr so $() captures stay clean) ----------

c_info() { printf '\033[36m[deploy]\033[0m %s\n' "$*" >&2; }
c_ok()   { printf '\033[32m[deploy]\033[0m %s\n' "$*" >&2; }
c_warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*" >&2; }
c_err()  { printf '\033[31m[deploy]\033[0m %s\n' "$*" >&2; }
c_step() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*" >&2; }
fatal()  { c_err "$*"; exit 1; }

# ---------- step 1: prerequisites -----------------------------------------

c_step "Step 1/5 — Prerequisites"
command -v docker >/dev/null 2>&1 || fatal "docker not found in PATH"
docker compose version >/dev/null 2>&1 \
    || fatal "'docker compose' (v2) not found"
command -v curl >/dev/null 2>&1 || fatal "curl not found in PATH"
docker info >/dev/null 2>&1 || fatal "docker daemon not reachable (engine running?)"
c_ok "docker $(docker --version | awk '{print $3}' | sed 's/,//')"

# ---------- step 2: build (direct — bypasses compose env interpolation) ---

c_step "Step 2/5 — Build image"
c_info "Running: docker build -t $IMAGE_TAG ."
docker build -t "$IMAGE_TAG" . >&2
c_ok "image $IMAGE_TAG built"

# ---------- step 3: read existing .env (if any) ---------------------------

c_step "Step 3/5 — Configure environment"
declare -A existing
if [[ -f "$ENV_FILE" ]]; then
    c_info "Found existing $ENV_FILE — will offer to reuse each value"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "${line// }" ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        key="${key// }"
        existing["$key"]="$val"
    done < "$ENV_FILE"
else
    c_info "No existing $ENV_FILE — will generate fresh values"
fi

# ---------- prompt helpers -------------------------------------------------

mask_secret() {
    local v="$1"
    local n=${#v}
    if (( n <= 12 )); then echo "***"; else echo "${v:0:4}***${v: -4} (${n} chars)"; fi
}

# returns exit 0 if user wants to keep existing value, exit 1 if replace.
prompt_keep_or_replace() {
    local label="$1"
    local ans
    read -r -p "  Keep existing ${label}? [Y/n] " ans
    [[ -z "$ans" || "$ans" =~ ^[Yy] ]]
}

# echoes the chosen value to stdout. prompt prints to stderr.
prompt_text() {
    local desc="$1" default="$2" current="${3:-}"
    local hint="$default"
    [[ -n "$current" ]] && hint="$current"
    local ans
    read -r -p "  ${desc} [${hint}]: " ans
    if [[ -z "$ans" ]]; then
        echo "${current:-$default}"
    else
        echo "$ans"
    fi
}

# echoes the bcrypt hash to stdout. interactive (twice + confirm).
prompt_password_to_hash() {
    local pw1 pw2 hash
    while true; do
        read -rs -p "  New password (min 8 chars): " pw1; echo >&2
        read -rs -p "  Confirm:                    " pw2; echo >&2
        if [[ "$pw1" != "$pw2" ]]; then c_warn "  passwords don't match — try again"; continue; fi
        if [[ ${#pw1} -lt 8 ]];   then c_warn "  must be at least 8 chars — try again"; continue; fi
        break
    done
    hash=$(printf '%s\n' "$pw1" | docker run --rm -i "$IMAGE_TAG" python -m bluewave.hashpw --stdin)
    unset pw1 pw2
    echo "$hash"
}

# ---------- 3a. CONFIG_ENC_KEYS -------------------------------------------

c_info ""
c_info "[1/8] CONFIG_ENC_KEYS — Fernet key for the encrypted config DB"
config_enc_keys=""
if [[ -n "${existing[CONFIG_ENC_KEYS]:-}" ]]; then
    c_info "  Current: $(mask_secret "${existing[CONFIG_ENC_KEYS]}")"
    c_warn "  Replacing this key wipes the existing config.sqlite (passwords"
    c_warn "  become undecryptable). For safe rotation use rotate_config (spec §13.8)."
    if prompt_keep_or_replace "encryption key"; then
        config_enc_keys="${existing[CONFIG_ENC_KEYS]}"
    else
        config_enc_keys=$(docker run --rm "$IMAGE_TAG" python -m bluewave.keygen)
        c_warn "  New key generated: $(mask_secret "$config_enc_keys")"
    fi
else
    config_enc_keys=$(docker run --rm "$IMAGE_TAG" python -m bluewave.keygen)
    c_ok "  Generated: $(mask_secret "$config_enc_keys")"
fi

# ---------- 3b. WEB_USER --------------------------------------------------

c_info ""
c_info "[2/8] WEB_USER — operator web UI username"
if [[ -n "${WEB_USER:-}" ]]; then
    web_user="$WEB_USER"
    c_info "  Using WEB_USER from environment: $web_user"
else
    web_user=$(prompt_text "Username" "admin" "${existing[WEB_USER]:-}")
fi

# ---------- 3c. WEB_PASS_HASH ---------------------------------------------

c_info ""
c_info "[3/8] WEB_PASS_HASH — operator web UI password (bcrypt-hashed)"
if [[ -n "${WEB_PASS:-}" ]]; then
    c_info "  Using WEB_PASS from environment (hashing now)"
    [[ ${#WEB_PASS} -ge 8 ]] || fatal "WEB_PASS must be ≥ 8 chars"
    web_pass_hash=$(printf '%s\n' "$WEB_PASS" \
        | docker run --rm -i "$IMAGE_TAG" python -m bluewave.hashpw --stdin)
    unset WEB_PASS
elif [[ -n "${existing[WEB_PASS_HASH]:-}" ]]; then
    c_info "  Current hash: ${existing[WEB_PASS_HASH]:0:7}…${existing[WEB_PASS_HASH]: -10} (60 chars)"
    if prompt_keep_or_replace "password"; then
        web_pass_hash="${existing[WEB_PASS_HASH]}"
    else
        web_pass_hash=$(prompt_password_to_hash)
        c_ok "  password hashed"
    fi
else
    web_pass_hash=$(prompt_password_to_hash)
    c_ok "  password hashed"
fi

# ---------- 3d. WEB_ALLOW_HTTP (forced to "1") ----------------------------

c_info ""
c_info "[4/8] WEB_ALLOW_HTTP — explicit acknowledgment of HTTP-only posture"
c_info "  Forced to '1' (spec L13). Container refuses to start otherwise."
web_allow_http="1"

# ---------- 3e. TZ (must remain UTC) --------------------------------------

c_info ""
c_info "[5/8] TZ — container libc timezone (must remain UTC; spec L17)"
tz=$(prompt_text "Container TZ" "UTC" "${existing[TZ]:-}")
if [[ "$tz" != "UTC" ]]; then
    c_warn "  TZ must be 'UTC' — forcing back to UTC (operator timezone is set in the web UI)."
    tz="UTC"
fi

# ---------- 3f. LOG_LEVEL --------------------------------------------------

c_info ""
c_info "[6/8] LOG_LEVEL — one of DEBUG / INFO / WARNING / ERROR"
c_info "  (DEBUG also enables the /docs OpenAPI page behind Basic Auth.)"
log_level=$(prompt_text "Log level" "INFO" "${existing[LOG_LEVEL]:-}")
case "${log_level^^}" in
    DEBUG|INFO|WARNING|ERROR) log_level="${log_level^^}" ;;
    *) c_warn "  Unknown level '$log_level' — falling back to INFO"; log_level="INFO" ;;
esac

# ---------- 3g. CATCH_UP_CAP_DAYS -----------------------------------------

c_info ""
c_info "[7/8] CATCH_UP_CAP_DAYS — max days to auto-catch-up on boot (1–90)"
catch_up_cap=$(prompt_text "Catch-up cap (days)" "14" "${existing[CATCH_UP_CAP_DAYS]:-}")
if ! [[ "$catch_up_cap" =~ ^[0-9]+$ ]] || (( catch_up_cap < 1 || catch_up_cap > 90 )); then
    c_warn "  Invalid value — falling back to 14"
    catch_up_cap=14
fi

# ---------- 3h. BACKFILL_SAFETY_CAP_DAYS ----------------------------------

c_info ""
c_info "[8/8] BACKFILL_SAFETY_CAP_DAYS — max age for an operator backfill"
backfill_cap=$(prompt_text "Backfill safety cap (days)" "365" "${existing[BACKFILL_SAFETY_CAP_DAYS]:-}")
if ! [[ "$backfill_cap" =~ ^[0-9]+$ ]] || (( backfill_cap < 1 )); then
    c_warn "  Invalid value — falling back to 365"
    backfill_cap=365
fi

# ---------- step 4: write .env --------------------------------------------

c_step "Step 4/5 — Write $ENV_FILE"

if [[ -f "$ENV_FILE" ]]; then
    backup="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$ENV_FILE" "$backup"
    chmod 600 "$backup"
    c_info "  Existing $ENV_FILE backed up to $backup"
fi

umask 077
cat > "$ENV_FILE" <<EOF
# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Keep this file out of git. Re-run deploy.sh to update any value.

# --- required ---
CONFIG_ENC_KEYS=$config_enc_keys
WEB_USER=$web_user
WEB_PASS_HASH=$web_pass_hash
WEB_ALLOW_HTTP=$web_allow_http

# --- optional overrides ---
TZ=$tz
LOG_LEVEL=$log_level
CATCH_UP_CAP_DAYS=$catch_up_cap
BACKFILL_SAFETY_CAP_DAYS=$backfill_cap
EOF
chmod 600 "$ENV_FILE"
c_ok "$ENV_FILE written (mode 600)"

# ---------- step 5: compose up + healthz wait -----------------------------

c_step "Step 5/5 — Start container"
docker compose up -d >&2
c_ok "container started"

c_info "Waiting for $WORKER_URL/healthz (timeout ${HEALTHZ_TIMEOUT_S}s)..."
deadline=$(( $(date +%s) + HEALTHZ_TIMEOUT_S ))
code="000"
hz_file=$(mktemp -t bluewave_healthz.XXXXXX)
while [[ $(date +%s) -lt $deadline ]]; do
    code=$(curl -sS -o "$hz_file" -w '%{http_code}' "$WORKER_URL/healthz" 2>/dev/null || echo "000")
    if [[ "$code" == "200" || "$code" == "503" ]]; then
        break
    fi
    sleep 2
done

if [[ "$code" != "200" && "$code" != "503" ]]; then
    rm -f "$hz_file"
    c_err "container did not respond on /healthz within ${HEALTHZ_TIMEOUT_S}s (last code: $code)"
    c_err "tail of 'docker compose logs':"
    docker compose logs --tail=40 "$SERVICE_NAME" >&2 || true
    exit 1
fi

status=$(python3 -c "
import json, sys
try:
    print(json.load(open('$hz_file')).get('status', '?'))
except Exception:
    print('?')
" 2>/dev/null || echo "?")
rm -f "$hz_file"

c_ok "/healthz responded (status=$status, HTTP $code)"

# ---------- post-start sanity: WEB_PASS_HASH arrived intact ---------------
#
# bcrypt hashes contain `$` chars. Older compose files used
# `environment: { WEB_PASS_HASH: "${WEB_PASS_HASH:?...}" }` which made
# Compose interpolate the `$`s, corrupting the hash. We now use env_file:
# which loads values verbatim. This check guarantees the hash matches.

c_info "Verifying secrets arrived in the container intact..."
container_hash=$(docker compose exec -T "$SERVICE_NAME" printenv WEB_PASS_HASH 2>/dev/null | tr -d '\r\n' || echo "")
if [[ "$container_hash" != "$web_pass_hash" ]]; then
    c_err "WEB_PASS_HASH in container does not match .env — auth WILL fail"
    c_err "  expected length: ${#web_pass_hash} chars"
    c_err "  actual length:   ${#container_hash} chars"
    c_err "  Likely cause: docker-compose.yml is interpolating the hash."
    c_err "  Make sure compose uses 'env_file: [.env]' (not 'environment:')."
    exit 1
fi
c_ok "WEB_PASS_HASH verified intact (${#container_hash} chars)"

container_keys=$(docker compose exec -T "$SERVICE_NAME" printenv CONFIG_ENC_KEYS 2>/dev/null | tr -d '\r\n' || echo "")
if [[ "$container_keys" != "$config_enc_keys" ]]; then
    c_err "CONFIG_ENC_KEYS in container does not match .env"
    exit 1
fi
c_ok "CONFIG_ENC_KEYS verified intact"

# ---------- next steps ----------------------------------------------------

echo >&2
case "$status" in
    unconfigured)
        c_info "Container is up and waiting for runtime configuration."
        cat >&2 <<EOF

  Next steps (in a browser):

    1. Open  $WORKER_URL/config
    2. Log in as  $web_user  (with the password you just entered)
    3. Fill in:
         - Site label (e.g. "Easy Foods Inc.")
         - BlueWeb URL / username / password
         - Operator timezone (IANA, e.g. America/New_York)
         - MySQL host / port / database / user / password
         - Schedule (HH:MM in operator timezone)
    4. Click 'Test BlueWeb' and 'Test MySQL' to validate
    5. Save — the daily cron registers and boot-time catch-up runs

EOF
        ;;
    ok)
        c_ok "Container is configured and healthy. Dashboard: $WORKER_URL/"
        ;;
    degraded)
        c_warn "/healthz status=degraded — check 'docker compose logs $SERVICE_NAME'"
        ;;
    *)
        c_warn "unexpected /healthz status: $status"
        ;;
esac
