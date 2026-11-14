"""Compare the schedule file with the operator's published GTFS feed.

The schedule file is the timetable, and a person verifies it: a departure time that is
merely plausible mis-windows every observation around it, silently. This module does not
change that. It reads the feed the operator publishes and reports where the file and the
feed disagree, and how long the file has left — so that a timetable which changed, or ran
out, is noticed by something other than a traveller standing in the line.

It may say "these differ" and "this runs out on". It may not write a departure time
anywhere: the feed has its own mistakes, and which side is right is a person's call.

Nothing here runs in the app or on its scheduler. It is a command, run by hand or by CI.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .schedule import ScheduleBlock

WEEKDAY_COLUMNS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)  # fmt: skip


class FeedError(Exception):
    """The feed could not be read as a timetable for this route.

    Raised, not swallowed: a check that cannot run must not look like a check that passed.
    """


@dataclass(frozen=True)
class FeedTimetable:
    """What the feed publishes for one route: departure times per terminal, per date."""

    # The last date the feed says anything about. Past it, an empty day is the feed's
    # silence and not a day without sailings, so nothing is compared beyond it.
    last_date: date
    _services: tuple[tuple[str, str, str, bool], ...]  # service_id, terminal, HH:MM, +1 day
    _calendar: dict[str, tuple[date, date, frozenset[int]]]
    _exceptions: dict[tuple[str, date], bool]

    def _runs(self, service_id: str, day: date) -> bool:
        exception = self._exceptions.get((service_id, day))
        if exception is not None:
            return exception
        entry = self._calendar.get(service_id)
        if entry is None:
            return False
        start, end, weekdays = entry
        return start <= day <= end and day.weekday() in weekdays

    def departures(self, day: date) -> dict[str, frozenset[str]]:
        """terminal code -> the HH:MM departures the feed publishes for `day`."""
        found: dict[str, set[str]] = {}
        for service_id, terminal, hhmm, next_day in self._services:
            # GTFS counts past midnight as 24:10 on the service day; the schedule file
            # holds local wall-clock dates, so such a trip belongs to the day after.
            service_day = day - timedelta(days=1) if next_day else day
            if self._runs(service_id, service_day):
                found.setdefault(terminal, set()).add(hhmm)
        return {terminal: frozenset(times) for terminal, times in found.items()}


def _rows(archive: zipfile.ZipFile, name: str, *, required: bool = True) -> list[dict]:
    try:
        raw = archive.read(name)
    except KeyError:
        if required:
            raise FeedError(f"the feed has no {name}") from None
        return []
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


def _feed_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def read_feed(content: bytes, route_id: str, terminal_by_stop: dict[str, str]) -> FeedTimetable:
    """Parse a GTFS zip into this route's departures.

    A trip's terminal is where its first stop is. A trip on this route that leaves from a
    stop nobody mapped fails the read: the alternative is dropping it, and a renumbered
    stop would then look exactly like a direction with no sailings.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise FeedError(f"the feed is not a zip archive: {exc}") from exc

    with archive:
        trips = {
            row["trip_id"]: row["service_id"]
            for row in _rows(archive, "trips.txt")
            if row["route_id"] == route_id
        }
        if not trips:
            raise FeedError(f"the feed has no trips for route_id {route_id!r}")

        first_stop: dict[str, tuple[int, str, str]] = {}
        for row in _rows(archive, "stop_times.txt"):
            trip_id = row["trip_id"]
            if trip_id not in trips:
                continue
            sequence = int(row["stop_sequence"])
            if trip_id not in first_stop or sequence < first_stop[trip_id][0]:
                first_stop[trip_id] = (sequence, row["stop_id"], row["departure_time"])

        services: set[tuple[str, str, str, bool]] = set()
        for trip_id, (_, stop_id, departure) in first_stop.items():
            terminal = terminal_by_stop.get(stop_id)
            if terminal is None:
                raise FeedError(
                    f"a trip on route {route_id!r} departs from stop_id {stop_id!r}, which "
                    "is not mapped to a terminal in [route.timetable_feed] stops"
                )
            hours, minutes = (int(part) for part in departure.strip().split(":")[:2])
            services.add(
                (trips[trip_id], terminal, f"{hours % 24:02d}:{minutes:02d}", hours >= 24)
            )

        wanted = {service_id for service_id, *_ in services}
        calendar: dict[str, tuple[date, date, frozenset[int]]] = {}
        for row in _rows(archive, "calendar.txt", required=False):
            if row["service_id"] in wanted:
                weekdays = frozenset(
                    index for index, column in enumerate(WEEKDAY_COLUMNS) if row[column] == "1"
                )
                calendar[row["service_id"]] = (
                    _feed_date(row["start_date"]),
                    _feed_date(row["end_date"]),
                    weekdays,
                )
        exceptions: dict[tuple[str, date], bool] = {}
        for row in _rows(archive, "calendar_dates.txt", required=False):
            if row["service_id"] in wanted:
                exceptions[(row["service_id"], _feed_date(row["date"]))] = (
                    row["exception_type"].strip() == "1"
                )

        # The feed's own stated end when it gives one; otherwise the last day any of this
        # route's services is defined for.
        last = None
        for row in _rows(archive, "feed_info.txt", required=False):
            if (row.get("feed_end_date") or "").strip():
                last = _feed_date(row["feed_end_date"])
        if last is None:
            ends = [end for _, end, _ in calendar.values()]
            ends += [day for (_, day), added in exceptions.items() if added]
            if not ends:
                raise FeedError(f"the feed gives no service dates for route_id {route_id!r}")
            last = max(ends)

    return FeedTimetable(
        last_date=last,
        _services=tuple(sorted(services)),
        _calendar=calendar,
        _exceptions=exceptions,
    )


@dataclass(frozen=True)
class Difference:
    """A run of dates on which the feed and the schedule file disagree the same way."""

    terminal: str
    first: date
    last: date
    days: int
    published: tuple[str, ...]  # what the feed says
    scheduled: tuple[str, ...]  # what the schedule file says

    def describe(self) -> str:
        when = (
            self.first.isoformat()
            if self.days == 1
            else f"{self.days} days from {self.first.isoformat()} to {self.last.isoformat()}"
        )
        missing = sorted(set(self.published) - set(self.scheduled))
        extra = sorted(set(self.scheduled) - set(self.published))
        if not self.scheduled:
            detail = f"published {', '.join(self.published)}; the schedule has nothing"
        elif not self.published:
            detail = f"the schedule has {', '.join(self.scheduled)}; nothing is published"
        else:
            parts = []
            if missing:
                parts.append(f"published but not scheduled: {', '.join(missing)}")
            if extra:
                parts.append(f"scheduled but not published: {', '.join(extra)}")
            detail = "; ".join(parts)
        return f"{self.terminal} {when}: {detail}"


def compare(
    blocks: tuple[ScheduleBlock, ...],
    feed: FeedTimetable,
    route_id: str,
    terminals: tuple[str, ...],
    start: date,
    end: date,
) -> list[Difference]:
    """Every disagreement from `start` to `end`, never looking past the feed's last date.

    Dates that differ in the same way are folded into one entry even when they are not
    consecutive — "every Sunday is missing the 05:35" is one finding, not twenty.
    """
    end = min(end, feed.last_date)
    grouped: dict[tuple[str, tuple[str, ...], tuple[str, ...]], list[date]] = {}
    day = start
    while day <= end:
        published = feed.departures(day)
        for terminal in terminals:
            scheduled = {
                departure.strftime("%H:%M")
                for block in blocks
                if block.applies_to(route_id) and block.terminal == terminal and block.covers(day)
                for departure in block.departures
            }
            theirs = published.get(terminal, frozenset())
            if scheduled != theirs:
                key = (terminal, tuple(sorted(theirs)), tuple(sorted(scheduled)))
                grouped.setdefault(key, []).append(day)
        day += timedelta(days=1)
    differences = [
        Difference(terminal, days[0], days[-1], len(days), published, scheduled)
        for (terminal, published, scheduled), days in grouped.items()
    ]
    differences.sort(key=lambda d: (d.first, d.terminal))
    return differences
