"""CSV → typed Row sequence (spec §5).

Pure: takes a CSV file path + an operator timezone, returns
``list[Row]`` ready for insert. Never touches MySQL, never touches a
network, never logs anything that contains card numbers.

Failure modes:

* :class:`MalformedCsvError` — the file's structure or a row's values
  are not parseable (e.g. unparseable timestamp, DST-non-existent local
  time). The whole call raises; partial output is never returned.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .dedup import DEDUP_SOURCE_BLUEWAVE, canonical_extra_data, dedup_hash
from .exceptions import ParseFailed


log = logging.getLogger(__name__)


EXPECTED_HEADER = (
    "Date/Time",
    "Door Name",
    "Description",
    "Person",
    "Employee ID",
    "Card Number",
    "Facility Code",
    "Pin",
)

CSV_TIMESTAMP_FMT = "%m/%d/%Y %H:%M:%S"


class MalformedCsvError(ParseFailed):
    """The CSV deviates from BlueWeb v20's expected shape."""


@dataclass(frozen=True)
class Row:
    """One ingestable event, ready for sink insert.

    Fields map 1:1 onto ``z_audit_logs_efk`` columns (plus the precomputed
    ``extra_data_str`` so callers don't re-serialize).
    """

    timestamp: datetime              # UTC-aware
    source: str
    operation: str
    instance: str
    user_name: Optional[str]
    user_id: Optional[str]
    extra_data_str: Optional[str]    # canonical JSON or None
    dedup_hash: str


def _strip_or_none(value: str) -> Optional[str]:
    """Strip; empty-after-strip → None (spec §5.1)."""
    s = (value or "").strip()
    return s or None


def _parse_naive_local(value: str) -> datetime:
    try:
        return datetime.strptime(value, CSV_TIMESTAMP_FMT)
    except ValueError as e:
        raise MalformedCsvError(
            f"unparseable Date/Time: {value!r} (expected {CSV_TIMESTAMP_FMT!r})"
        ) from e


def _localize_to_utc(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach ``tz`` to a naive local datetime and convert to UTC.

    DST rules per spec L17 / §5.1.1:

    * **Ambiguous** (fall-back duplicated hour) → use ``fold=0`` (the earlier,
      pre-transition UTC instance). Deterministic.
    * **Non-existent** (spring-forward skipped hour) → raise
      :class:`MalformedCsvError`. The whole run fails loud rather than
      silently shifting an event by an hour.
    """
    # fold=0 is the default for replace(); set it explicitly for clarity.
    localized = naive.replace(tzinfo=tz, fold=0)

    # zoneinfo: dst() returning None means the local time is non-existent
    # (a "gap"). Per spec L17, this is a hard error.
    dst_offset = localized.dst()
    if dst_offset is None:  # pragma: no cover - zoneinfo policy varies
        raise MalformedCsvError(
            f"non-existent local time {naive.isoformat()} in {tz.key!r} "
            f"(spring-forward gap)"
        )

    # Detect spring-forward gap by checking round-trip equivalence: if
    # converting through UTC and back produces a different naive local
    # time, the original was in the gap.
    utc = localized.astimezone(timezone.utc)
    roundtrip = utc.astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        raise MalformedCsvError(
            f"non-existent local time {naive.isoformat()} in {tz.key!r} "
            f"(spring-forward gap; round-trip → {roundtrip.isoformat()})"
        )

    return utc


def _row_from_csv(
    raw: dict[str, str],
    *,
    tz: ZoneInfo,
    source: str,
) -> Row:
    naive = _parse_naive_local(raw["Date/Time"])
    ts_utc = _localize_to_utc(naive, tz)

    operation = (raw["Description"] or "").strip()
    instance = (raw["Door Name"] or "").strip()
    if not operation or not instance:
        raise MalformedCsvError(
            f"row missing required field(s): "
            f"operation={operation!r}, instance={instance!r}"
        )

    user_name = _strip_or_none(raw["Person"])
    user_id = _strip_or_none(raw["Employee ID"])
    card_number = _strip_or_none(raw["Card Number"])
    facility_code = _strip_or_none(raw["Facility Code"])
    extra = canonical_extra_data(card_number, facility_code)

    h = dedup_hash(
        source=source,
        timestamp_utc=ts_utc,
        operation=operation,
        instance=instance,
        user_id=user_id,
        user_name=user_name,
        extra_data_str=extra,
    )

    return Row(
        timestamp=ts_utc,
        source=source,
        operation=operation,
        instance=instance,
        user_name=user_name,
        user_id=user_id,
        extra_data_str=extra,
        dedup_hash=h,
    )


def transform(
    csv_path: str,
    *,
    timezone: str,
    source: str = DEDUP_SOURCE_BLUEWAVE,
) -> list[Row]:
    """Parse the BlueWeb CSV at ``csv_path`` and return a list of typed rows.

    ``timezone`` is the IANA name (e.g. ``"America/New_York"``) the operator
    configured. CSV timestamps are interpreted as local-naive in that zone,
    then converted to UTC for storage.

    Pure: no DB writes, no network, no logging of card-numbers.
    """
    try:
        tz = ZoneInfo(timezone)
    except Exception as e:
        raise MalformedCsvError(f"unknown timezone {timezone!r}: {e}") from e

    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = tuple(next(reader))
        except StopIteration:
            raise MalformedCsvError(f"empty CSV: {csv_path}")

        if header != EXPECTED_HEADER:
            raise MalformedCsvError(
                f"unexpected header: {header!r} (want {EXPECTED_HEADER!r})"
            )

        rows: list[Row] = []
        for i, fields in enumerate(reader, start=2):  # line 1 was header
            if len(fields) != len(EXPECTED_HEADER):
                raise MalformedCsvError(
                    f"line {i}: expected {len(EXPECTED_HEADER)} fields, "
                    f"got {len(fields)}"
                )
            raw = dict(zip(EXPECTED_HEADER, fields))
            try:
                rows.append(_row_from_csv(raw, tz=tz, source=source))
            except MalformedCsvError:
                # Re-raise with line context preserved.
                raise
            except Exception as e:  # pragma: no cover — defensive
                raise MalformedCsvError(
                    f"line {i}: unhandled parse error: {e}"
                ) from e

        return rows
