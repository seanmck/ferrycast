"""Time helpers. Everything is stored in UTC; everything is *reasoned about* in local time."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class TimezoneRule:
    """What this machine's tz database believes about a zone.

    Two environments running the same code can disagree about what "11:45 local" means, and
    nothing in the app will notice: the timetable is stored as local clock times and turned
    into UTC instants, so a stale tz database shifts every sailing window by an hour without
    erroring. It happened here — a container on BC's permanent-DST rule against a laptop
    still expecting clocks to change, disagreeing about every date after 2026-11-01.

    So this is a fingerprint to compare across machines, not a pass/fail check. Nothing in a
    tz database is wrong in a way code can detect from the inside; it can only be different
    from somewhere else.
    """

    zone: str
    offset_now: str
    abbrev_now: str
    next_transition: str | None
    offset_in_a_year: str
    abbrev_in_a_year: str

    def summary(self) -> str:
        when = self.next_transition or "none within 2 years"
        return (
            f"{self.zone}: now {self.offset_now} ({self.abbrev_now}), "
            f"next change {when}, in 12 months {self.offset_in_a_year} "
            f"({self.abbrev_in_a_year})"
        )


def _offset_text(moment: datetime) -> str:
    delta = moment.utcoffset() or timedelta(0)
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def timezone_rule(tz: ZoneInfo, *, at: datetime | None = None) -> TimezoneRule:
    """Fingerprint the installed rule for `tz`: offset now, next change, offset a year out."""
    now = (at or now_utc()).astimezone(tz)
    probe = now
    previous = probe.utcoffset()
    change = None
    horizon = now + timedelta(days=730)
    while probe < horizon:
        probe += timedelta(days=1)
        current = probe.utcoffset()
        if current != previous:
            # Found the day; the exact instant is not what this is for.
            change = probe.date().isoformat()
            break
        previous = current
    later = now + timedelta(days=365)
    return TimezoneRule(
        zone=str(tz),
        offset_now=_offset_text(now),
        abbrev_now=now.tzname() or "?",
        next_transition=change,
        offset_in_a_year=_offset_text(later),
        abbrev_in_a_year=later.tzname() or "?",
    )
