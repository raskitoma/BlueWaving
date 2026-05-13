"""Canonical extra_data JSON + deterministic dedup hash (spec §5.2 / §5.3).

Pure functions. No I/O. Reproducible byte-for-byte across runs and across
hosts — critical for ``z_audit_logs_efk.dedup_hash`` to function as the
idempotency key.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional


# Canonical timestamp format used inside the dedup hash (spec §5.3).
# Always millisecond-zero suffix. UTC, with trailing Z.
DEDUP_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S.000Z"

# Field separator inside the dedup hash payload (spec §5.3).
# Pipe is chosen because no observed source value contains it; if a real
# collision is ever reported, the response is a hash version bump (§11/D2).
DEDUP_SEP = "|"

DEDUP_SOURCE_BLUEWAVE = "Bluewave"


def canonical_extra_data(
    card_number: Optional[str],
    facility_code: Optional[str],
) -> Optional[str]:
    """Build the canonical ``extra_data`` JSON string per spec §5.2.

    Rules (reproduced from spec, in evaluation order):

    1. Keys are ``snake_case`` exactly as listed (``card_number``, ``facility_code``).
    2. Keys appear in alphabetical order.
    3. No whitespace between tokens.
    4. UTF-8 preserved (``ensure_ascii=False``).
    5. A key whose value is empty/whitespace is **omitted entirely**.
    6. If both keys are omitted, return ``None``.

    The output string is what gets stored in MySQL's JSON column AND what
    feeds the dedup hash. Both must be byte-stable.
    """
    payload: dict[str, str] = {}

    if card_number is not None and card_number.strip():
        payload["card_number"] = card_number.strip()
    if facility_code is not None and facility_code.strip():
        payload["facility_code"] = facility_code.strip()

    if not payload:
        return None

    # Alphabetical key order + no whitespace. ensure_ascii=False preserves UTF-8.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_timestamp_utc(ts: datetime) -> str:
    """Format ``ts`` (must be UTC-aware) per :data:`DEDUP_TIMESTAMP_FMT`."""
    if ts.tzinfo is None:
        raise ValueError("dedup hash requires a UTC-aware timestamp")
    if ts.utcoffset() != timezone.utc.utcoffset(None):
        ts = ts.astimezone(timezone.utc)
    return ts.strftime(DEDUP_TIMESTAMP_FMT)


def dedup_hash(
    *,
    source: str,
    timestamp_utc: datetime,
    operation: str,
    instance: str,
    user_id: Optional[str],
    user_name: Optional[str],
    extra_data_str: Optional[str],
) -> str:
    """Return ``sha256(source|ts|op|instance|user_id|user|extra)`` as hex.

    Exactly matches spec §5.3. Any change to this function requires a hash
    version bump and a re-ingest of historical data — never change the
    canonicalization rules without considering downstream consumers.
    """
    parts = [
        source,
        canonical_timestamp_utc(timestamp_utc),
        operation,
        instance,
        user_id or "",
        user_name or "",
        extra_data_str or "",
    ]
    payload = DEDUP_SEP.join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
