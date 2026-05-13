"""M5 — canonical_extra_data + dedup_hash determinism (spec §5.2 / §5.3)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bluewave.dedup import (
    DEDUP_SOURCE_BLUEWAVE,
    canonical_extra_data,
    canonical_timestamp_utc,
    dedup_hash,
)


# ---------------------------------------------------------------------------
# canonical_extra_data
# ---------------------------------------------------------------------------


def test_both_present_alphabetical_order() -> None:
    out = canonical_extra_data(card_number="5057", facility_code="190")
    # Keys MUST be alphabetical → card_number before facility_code.
    assert out == '{"card_number":"5057","facility_code":"190"}'


def test_only_card_number() -> None:
    assert canonical_extra_data("5057", None) == '{"card_number":"5057"}'


def test_only_facility_code() -> None:
    assert canonical_extra_data(None, "190") == '{"facility_code":"190"}'


def test_both_empty_returns_none() -> None:
    assert canonical_extra_data(None, None) is None
    assert canonical_extra_data("", "") is None
    assert canonical_extra_data("   ", "\t") is None


def test_whitespace_only_is_treated_as_empty() -> None:
    assert canonical_extra_data(" ", " ") is None


def test_internal_whitespace_is_stripped() -> None:
    assert canonical_extra_data("  5057  ", " 190 ") == \
        '{"card_number":"5057","facility_code":"190"}'


def test_canonical_extra_data_is_deterministic() -> None:
    """Same inputs → same output, byte-for-byte. Critical for hash stability."""
    a = canonical_extra_data("5057", "190")
    b = canonical_extra_data("5057", "190")
    assert a == b


def test_no_whitespace_between_tokens() -> None:
    out = canonical_extra_data("5057", "190")
    assert " " not in out and "\n" not in out


# ---------------------------------------------------------------------------
# canonical_timestamp_utc
# ---------------------------------------------------------------------------


def test_canonical_timestamp_format() -> None:
    ts = datetime(2026, 5, 12, 23, 59, 46, tzinfo=timezone.utc)
    assert canonical_timestamp_utc(ts) == "2026-05-12T23:59:46.000Z"


def test_canonical_timestamp_requires_tz() -> None:
    with pytest.raises(ValueError):
        canonical_timestamp_utc(datetime(2026, 5, 12, 12, 0, 0))  # naive


def test_canonical_timestamp_converts_to_utc() -> None:
    from zoneinfo import ZoneInfo

    ts = datetime(2026, 5, 12, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    # NY in May is UTC-4 → 12:00 NY = 16:00 UTC
    assert canonical_timestamp_utc(ts) == "2026-05-12T16:00:00.000Z"


# ---------------------------------------------------------------------------
# dedup_hash
# ---------------------------------------------------------------------------


def _sample_kwargs():
    return {
        "source": DEDUP_SOURCE_BLUEWAVE,
        "timestamp_utc": datetime(2026, 5, 12, 22, 0, 0, tzinfo=timezone.utc),
        "operation": "Admit W1",
        "instance": "Parts",
        "user_id": "6162",
        "user_name": "Borregales, Pedro",
        "extra_data_str": '{"card_number":"4522","facility_code":"190"}',
    }


def test_dedup_hash_is_64_hex_chars() -> None:
    h = dedup_hash(**_sample_kwargs())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_dedup_hash_is_deterministic() -> None:
    assert dedup_hash(**_sample_kwargs()) == dedup_hash(**_sample_kwargs())


def test_dedup_hash_distinct_on_any_field_change() -> None:
    base = _sample_kwargs()
    base_hash = dedup_hash(**base)

    mutations = [
        ("source", "Other"),
        ("operation", "Reject W1"),
        ("instance", "Receiving"),
        ("user_id", "9999"),
        ("user_name", "Other, Person"),
        ("extra_data_str", None),
        ("extra_data_str", '{"card_number":"9999"}'),
    ]
    for field, new in mutations:
        cfg = dict(base)
        cfg[field] = new
        assert dedup_hash(**cfg) != base_hash, f"hash should change for {field}={new!r}"


def test_dedup_hash_distinct_on_timestamp_change() -> None:
    base = _sample_kwargs()
    base_hash = dedup_hash(**base)
    bumped = dict(base)
    bumped["timestamp_utc"] = datetime(2026, 5, 12, 22, 0, 1, tzinfo=timezone.utc)
    assert dedup_hash(**bumped) != base_hash


def test_dedup_hash_treats_none_user_id_same_as_empty_string() -> None:
    """The hash formula's ``user_id or ""`` collapses None and ''. Document it."""
    a = dict(_sample_kwargs(), user_id=None)
    b = dict(_sample_kwargs(), user_id="")
    assert dedup_hash(**a) == dedup_hash(**b)


def test_dedup_hash_distinguishes_present_id_from_missing() -> None:
    a = dict(_sample_kwargs(), user_id="6162")
    b = dict(_sample_kwargs(), user_id=None)
    assert dedup_hash(**a) != dedup_hash(**b)
