"""Time helpers. Everything is stored in UTC; everything is *reasoned about* in local time."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

UTC = UTC


def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime cannot be converted to UTC without a zone")
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    """Canonical storage format: UTC, second resolution, trailing Z."""
    return to_utc(dt).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def local(dt: datetime, zone: ZoneInfo) -> datetime:
    return to_utc(dt).astimezone(zone)


def local_date(dt: datetime, zone: ZoneInfo) -> date:
    return local(dt, zone).date()


def parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def combine_local(day: date, hhmm: time, zone: ZoneInfo) -> datetime:
    """Local wall-clock time on a given day, as an aware datetime.

    Note: DST transitions can make a wall-clock time ambiguous or nonexistent.
    Python resolves those deterministically (fold=0), which is the right call here —
    the ferry timetable is published in wall-clock terms.
    """
    return datetime.combine(day, hhmm, tzinfo=zone)


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
