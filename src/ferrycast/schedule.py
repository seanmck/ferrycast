"""Sailing schedule, day-type and season bucketing.

The timetable is seasonal, so it is expressed as dated blocks: each block gives the
departures for one terminal, on a set of weekdays, over a date range.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError
from .holidays import holiday_name, is_holiday
from .timeutil import combine_local, parse_hhmm

WEEKDAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

DAY_TYPES = ("weekday", "friday", "saturday", "sunday_holiday")

# Coarse buckets; deliberately few, because the PRD flags thin buckets as a risk.
SEASONS = ("peak_summer", "shoulder", "winter")


@dataclass(frozen=True)
class ScheduleBlock:
    terminal: str
    effective_from: date
    effective_to: date
    days: frozenset[int]
    departures: tuple[time, ...]

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to and day.weekday() in self.days


@dataclass(frozen=True)
class Sailing:
    route: str
    origin: str
    destination: str
    service_date: date
    scheduled_departure: datetime  # local, timezone-aware
    depart_hhmm: str
    day_type: str
    season: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.origin, self.scheduled_departure.isoformat())


def _parse_days(value) -> frozenset[int]:
    if value in (None, "all", "daily"):
        return frozenset(range(7))
    if isinstance(value, str):
        value = [value]
    days: set[int] = set()
    for item in value:
        token = item.strip().lower()[:3]
        if token not in WEEKDAY_NAMES:
            raise ConfigError(f"unrecognised day {item!r} in schedule")
        days.add(WEEKDAY_NAMES[token])
    if not days:
        raise ConfigError("schedule block has an empty day list")
    return frozenset(days)


def _parse_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def load_schedule(path: str | Path) -> tuple[ScheduleBlock, ...]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"no schedule at {path}. Copy config/schedule.example.toml and verify the "
            "departure times against the published BC Ferries timetable."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    blocks_raw = raw.get("block") or raw.get("schedule") or []
    if not blocks_raw:
        raise ConfigError(f"{path} defines no [[block]] entries")

    blocks = []
    for entry in blocks_raw:
        try:
            departures = tuple(sorted(parse_hhmm(t) for t in entry["departures"]))
        except KeyError as exc:
            raise ConfigError(f"schedule block missing {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise ConfigError(f"bad departure time in schedule: {exc}") from exc
        if not departures:
            raise ConfigError("schedule block has no departures")
        blocks.append(
            ScheduleBlock(
                terminal=entry["terminal"],
                effective_from=_parse_date(entry["effective_from"]),
                effective_to=_parse_date(entry["effective_to"]),
                days=_parse_days(entry.get("days")),
                departures=departures,
            )
        )
    return tuple(blocks)


@lru_cache(maxsize=8)
def _cached_schedule(path: str, mtime: float) -> tuple[ScheduleBlock, ...]:
    return load_schedule(path)


def load_schedule_cached(path: str | Path) -> tuple[ScheduleBlock, ...]:
    path = Path(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    return _cached_schedule(str(path), mtime)


def day_type(day: date) -> str:
    """Weekday / Friday / Saturday / Sunday-or-holiday, per the PRD's comparability rule."""
    if is_holiday(day):
        return "sunday_holiday"
    weekday = day.weekday()
    if weekday == 6:
        return "sunday_holiday"
    if weekday == 5:
        return "saturday"
    if weekday == 4:
        return "friday"
    return "weekday"


def season(day: date) -> str:
    """Peak summer is the stretch that actually overloads; the rest splits at the shoulders."""
    md = (day.month, day.day)
    if (6, 25) <= md <= (9, 8):
        return "peak_summer"
    if (4, 1) <= md < (6, 25) or (9, 8) < md <= (10, 31):
        return "shoulder"
    return "winter"


def day_tags(day: date) -> list[str]:
    """Automatic demand-driver tags. Manual tags live in the database alongside these."""
    tags: list[str] = []
    name = holiday_name(day)
    if name:
        tags.append(f"holiday:{name}")
    from .holidays import is_long_weekend

    if is_long_weekend(day):
        tags.append("long_weekend")
    return tags


def sailings_for_day(
    blocks: tuple[ScheduleBlock, ...],
    day: date,
    route_id: str,
    origin_to_destination: dict[str, str],
    zone: ZoneInfo,
    origin: str | None = None,
) -> list[Sailing]:
    """Every scheduled sailing on `day`, ordered by departure time."""
    dtype = day_type(day)
    bucket = season(day)
    seen: set[tuple[str, str]] = set()
    result: list[Sailing] = []
    for block in blocks:
        if origin and block.terminal != origin:
            continue
        if not block.covers(day):
            continue
        destination = origin_to_destination.get(block.terminal)
        if destination is None:
            continue
        for departure in block.departures:
            hhmm = departure.strftime("%H:%M")
            marker = (block.terminal, hhmm)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(
                Sailing(
                    route=route_id,
                    origin=block.terminal,
                    destination=destination,
                    service_date=day,
                    scheduled_departure=combine_local(day, departure, zone),
                    depart_hhmm=hhmm,
                    day_type=dtype,
                    season=bucket,
                )
            )
    result.sort(key=lambda s: (s.scheduled_departure, s.origin))
    return result
