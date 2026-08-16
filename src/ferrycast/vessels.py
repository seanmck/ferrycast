"""Vessel tracking — where the ship is, for the direction no board covers.

BC Ferries publishes a departures board for Saltery Bay and none at all for Earls Cove (see
`deckspace.publishes_departures`), so the homeward direction has no source of a departure
time. It does publish a live vessel tracker, and both directions' conditions pages embed the
same one: a small page carrying each vessel's status (`Stopped` / `Under Way`), compass
heading, speed and the feed's own timestamp, refreshed every 30 seconds.

What that supports and what it does not is worth stating plainly, because the gap is the
whole reason this module is small:

* It can say **the sailing ran, and roughly when it left**. That is the prerequisite for
  reading a residual queue: measuring at scheduled+12 photographs a compound still loading
  whenever a sailing leaves late, which on this route is routine.
* It can say nothing whatever about the **deck** or the **compound**. No capacity, no queue,
  no one left behind. So it never sets `filled` or `left_behind`, and in particular it must
  not be read as `boarded`: at Saltery Bay "departed, and the board never posted the
  capacity note" is informative *because a board exists that would have carried the note*.
  At Earls Cove there is no board, so the same silence proves nothing at all.

The feed keeps no history — thirty-second refresh, no archive — so a reading not polled is
gone for good, exactly like a frame.

Which terminal a vessel is stopped at is **not** published: the status says it is stopped and
never where. But the route is a two-point shuttle with a single vessel, so stopped-then-moving
*is* a departure, and the only open question is which end it left. That is settled against the
Saltery Bay board — which publishes its own departures to the minute — rather than inferred
from the compass heading: a transition matching a published one is Saltery Bay's, and one
matching none belongs to Earls Cove, which publishes nothing.

The status wording is per-route — `Stopped` here, `In Port` on the Tsawwassen runs — so it is
matched against a set, with the speed reading as a fallback beneath it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Config
from .db import JobRun
from .fetching import fetch
from .timeutil import combine_local, iso, local, now_utc, parse_hhmm, parse_iso

#: Status words meaning the vessel is not moving. Route 1 says `In Port`; route 29 — this
#: route — says `Stopped`, which is what made the first live version derive no departure at
#: all: matching only `In Port` meant Earls Cove never saw the transition it exists to catch.
#: Both are kept because the wording is per-route and neither is more official than the other.
STOPPED_STATUSES = frozenset({"in port", "stopped", "docked"})

#: ...and the ones meaning it is.
MOVING_STATUSES = frozenset({"under way", "underway", "en route"})

# "7:04 AM" — the feed's own timestamp, local to the terminal, with no date on it.
_UPDATED_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([AP])\.?M\.?$", re.IGNORECASE)

# One vessel's row: name, status, heading, last update. The trailing explanatory rows use
# colspan, so a row of exactly four plain cells is a vessel and nothing else is.
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td(?![^>]*colspan)[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)

# Speed lives only in the hover script, one block per vessel:
#     '<td><b>Malaspina Sky</b></td>' '<td>Heading: <b>W</b></td>' '<td>Speed: <b>14.9 knots</b></td>'
_SPEED_RE = re.compile(r"Speed:\s*<b>\s*([\d.]+)\s*knots?", re.IGNORECASE)
_BOLD_RE = re.compile(r"<b>([^<]+)</b>", re.IGNORECASE)


@dataclass(frozen=True)
class VesselReading:
    vessel: str
    status: str
    heading: str
    updated_hhmm: str | None
    speed_knots: float | None = None

    @property
    def under_way(self) -> bool:
        return is_moving(self.status, self.speed_knots) is True

    @property
    def in_port(self) -> bool:
        return is_moving(self.status, self.speed_knots) is False


def is_moving(status: str | None, speed_knots: float | None) -> bool | None:
    """Is the vessel moving (True), stopped (False), or is this reading silent (None)?

    Speed decides where the status word is one we have not seen. The wording turned out to
    be per-route — `In Port` on one, `Stopped` on another — and a word we do not recognise
    should not read as "not stopped", which is what silently cost Earls Cove every departure
    in the first live version. A number in knots is far less likely to be reworded than a
    label, so it is the safety net rather than the primary signal.
    """
    word = (status or "").strip().lower()
    if word in MOVING_STATUSES:
        return True
    if word in STOPPED_STATUSES:
        return False
    if speed_knots is None:
        return None
    return speed_knots > 0


def _strip(cell: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", cell).replace("&nbsp;", " ").split())


def _to_24h(raw: str) -> str | None:
    match = _UPDATED_RE.match(raw.strip())
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    if meridiem == "P" and hour != 12:
        hour += 12
    elif meridiem == "A" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _speeds(html: str) -> dict[str, float]:
    """Vessel name -> knots, read out of the hover script.

    Walked backwards from each speed rather than matched forwards from a name: the bold
    runs nearest a speed are that vessel's heading and then its name, while the first bold
    in the document is a column header several hundred bytes away. A forward match spans
    that gap happily and files every speed under "Vessel".
    """
    found: dict[str, float] = {}
    for match in _SPEED_RE.finditer(html):
        before = html[: match.start()]
        for bold in reversed(list(_BOLD_RE.finditer(before))):
            # Anything introduced by a label is a field, not the name: routes vary in which
            # fields they carry, and one with a `Destination:` line filed every speed under
            # the destination port until this stopped looking only for `Heading:`.
            if before[max(0, bold.start() - 16) : bold.start()].rstrip().endswith(":"):
                continue
            name = _strip(bold.group(1))
            if name:
                found[name] = float(match.group(1))
            break
    return found


def parse_tracking(html: str) -> list[VesselReading]:
    """Pull each vessel's line off the tracker.

    Degrades to an empty list rather than raising: this is a hand-rolled page from 2012 and
    the point of collecting it is that nothing else covers the direction, so a bad parse
    must not take the scrape down with it.
    """
    speeds = _speeds(html)
    readings: list[VesselReading] = []
    for row in _ROW_RE.findall(html):
        cells = [_strip(c) for c in _CELL_RE.findall(row)]
        if len(cells) != 4:
            continue
        vessel, status, heading, updated = cells
        if not vessel or vessel.lower() == "vessel":  # the header row
            continue
        readings.append(
            VesselReading(
                vessel=vessel,
                status=status,
                heading=heading,
                updated_hhmm=_to_24h(updated),
                speed_knots=speeds.get(vessel),
            )
        )
    return readings


def _reported_at(reading: VesselReading, observed_at: datetime, config: Config) -> datetime | None:
    """The feed's own timestamp as an instant.

    It publishes a bare local clock time, so the date comes from when we fetched it. The
    feed also runs a few minutes behind, which puts a reading taken just after midnight on
    the previous day — hence the rollback rather than a plain same-day combine.
    """
    if not reading.updated_hhmm:
        return None
    here = local(observed_at, config.tz)
    hour, minute = (int(part) for part in reading.updated_hhmm.split(":"))
    stamp = combine_local(here.date(), time(hour, minute), config.tz)
    if stamp - here > timedelta(hours=12):
        stamp -= timedelta(days=1)
    return stamp


def store_readings(
    conn: sqlite3.Connection,
    config: Config,
    observed_at: datetime,
    readings: list[VesselReading],
) -> int:
    stored = 0
    for reading in readings:
        reported = _reported_at(reading, observed_at, config)
        cur = conn.execute(
            """INSERT OR IGNORE INTO vessel_positions
                   (route, vessel, status, heading, speed_knots, reported_at, observed_at,
                    fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'ok')""",
            (
                config.route.id,
                reading.vessel,
                reading.status,
                reading.heading,
                reading.speed_knots,
                iso(reported) if reported else None,
                iso(observed_at),
            ),
        )
        stored += cur.rowcount or 0
    conn.commit()
    return stored


def refresh(conn: sqlite3.Connection, config: Config) -> dict:
    """Poll the tracker once. Deduplicated on the feed's timestamp, not ours.

    The page refreshes every thirty seconds but the underlying position updates about every
    five minutes, so polling on the capture cadence re-reads the same instant repeatedly.
    Keying on `reported_at` keeps one row per actual reading instead of one per poll.
    """
    url = config.route.vessel_tracking_url
    if not url:
        return {"ok": False, "skipped": True, "rows": 0}

    observed_at = now_utc()
    with JobRun(conn, "vessels") as run:
        run.attempted += 1
        result = fetch(
            url,
            user_agent=config.capture.user_agent,
            timeout=config.capture.timeout_seconds,
            max_retries=config.capture.max_retries,
        )
        if not result.ok or not result.text:
            error = result.error or "empty response"
            conn.execute(
                """INSERT INTO vessel_positions
                       (route, observed_at, fetch_status, error) VALUES (?, ?, 'error', ?)""",
                (config.route.id, iso(observed_at), error),
            )
            conn.commit()
            return {"ok": False, "rows": 0, "error": error}

        readings = parse_tracking(result.text)
        if not readings:
            conn.execute(
                """INSERT INTO vessel_positions
                       (route, observed_at, fetch_status, error) VALUES (?, ?, 'unparsed', ?)""",
                (config.route.id, iso(observed_at), "no vessel row recognised"),
            )
            conn.commit()
            return {"ok": False, "rows": 0, "error": "tracker format not recognised"}

        stored = store_readings(conn, config, observed_at, readings)
        run.succeeded += 1
        return {"ok": True, "rows": stored, "vessels": len(readings)}


#: How late a sailing may leave and still be recognised. Comfortably inside this route's
#: tightest headway (110 minutes at Earls Cove), so one sailing's window cannot reach into
#: the next one's departure.
LATE_TOLERANCE = timedelta(minutes=75)

#: How close a tracked transition may sit to a departure the board published at the *other*
#: terminal before it is taken to be that same departure rather than this terminal's. The two
#: cannot really be confused: the same vessel has to cross between them, so consecutive
#: departures are at least a crossing apart — about fifty minutes on this route. Fifteen
#: minutes is therefore generous against feed lag while nowhere near the real separation.
SAME_DEPARTURE = timedelta(minutes=15)


def _published_elsewhere(
    conn: sqlite3.Connection, config: Config, *, route_id: str, origin: str, window: tuple
) -> list[datetime]:
    """Departures the board published for a terminal other than this one, inside `window`.

    This is the whole disambiguation, and it rests on hard evidence rather than inference:
    the operator states when a sailing left Saltery Bay, so a transition matching one is
    Saltery Bay's and any other transition belongs to the end that publishes nothing.
    """
    start, end = window
    days = sorted({(start.date() - timedelta(days=1)), start.date(), end.date()})
    placeholders = ",".join("?" for _ in days)
    rows = conn.execute(
        f"""SELECT DISTINCT service_date, sailing_hhmm, departed_hhmm FROM deck_space
             WHERE route = ? AND terminal != ? AND fetch_status = 'ok'
               AND departed_hhmm IS NOT NULL
               AND service_date IN ({placeholders})""",
        (route_id, origin, *(d.isoformat() for d in days)),
    ).fetchall()

    found = []
    for row in rows:
        service_date = date.fromisoformat(row["service_date"])
        left = combine_local(service_date, parse_hhmm(row["departed_hhmm"]), config.tz)
        # A sailing scheduled at 23:50 and away at 00:05 belongs to the previous service
        # date; without this its departure lands twenty-four hours early.
        scheduled = combine_local(service_date, parse_hhmm(row["sailing_hhmm"]), config.tz)
        if left < scheduled - timedelta(hours=12):
            left += timedelta(days=1)
        if start - SAME_DEPARTURE <= left <= end + SAME_DEPARTURE:
            found.append(left)
    return found


def departure_from_tracking(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    departure: datetime,
    grace: timedelta = timedelta(minutes=20),
    route_id: str | None = None,
) -> datetime | None:
    """When the vessel left `origin`, as the tracker saw it — or None if it cannot say.

    Returns the first moving reading of the earliest stopped-then-moving transition in the
    window that the board does not already account for elsewhere. That is an upper bound on
    the departure: the vessel had certainly gone by then, and reading a residual queue
    slightly late is the safe direction to be wrong in.

    The route is a two-point shuttle with one vessel, so stopped-then-moving *is* a
    departure and there is nothing to infer about that. The only real question is which end
    it left from, and that is settled against the board rather than guessed from the feed:
    Saltery Bay publishes its departures to the minute, so a transition matching one is
    Saltery Bay's, and a transition matching none belongs to the end that publishes nothing.

    Every transition is offered to that test, earliest first, rather than only the last one
    in the window. The window has to reach far enough past the scheduled time to catch a
    late sailing, and on a fifty-minute crossing that is long enough to also contain the
    vessel arriving at the *other* end and tying up there. Taking the last transition
    therefore found the far terminal's departure — or, with the vessel still berthed over
    there when the window closed, no transition at all. Either way the answer was None for
    every sailing punctual enough that the round trip fitted inside the window, which is to
    say for nearly all of them: only a sailing over half an hour late reported anything.

    This replaced reading the compass heading, which tried to derive the same fact from a
    three-character string and got it wrong in production. Earls Cove sits in a narrow cove,
    so the vessel swings north-east clear of it before turning north-west across the strait
    — and `NE` contains an `E`, so an outbound sailing read as inbound and the 15:40 that
    left at 16:26 still showed "not away yet" ten minutes later. Heading is a proxy for
    geometry; the board is the fact.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    window = (departure - grace, departure + LATE_TOLERANCE)
    rows = conn.execute(
        """SELECT status, speed_knots, reported_at FROM vessel_positions
            WHERE route = ? AND fetch_status = 'ok' AND reported_at IS NOT NULL
              AND reported_at >= ? AND reported_at <= ?
            ORDER BY reported_at""",
        (route.id, iso(window[0]), iso(window[1])),
    ).fetchall()
    found = _transitions(rows)
    if not found:
        return None
    published = _published_elsewhere(
        conn, config, route_id=route.id, origin=origin, window=window
    )
    return next((left for left in found if not _is_claimed(left, published)), None)


def _is_claimed(moment: datetime, published: list[datetime]) -> bool:
    """Whether the board has already accounted for this transition at the other terminal."""
    return any(abs((moment - other).total_seconds()) <= SAME_DEPARTURE.total_seconds()
               for other in published)


def _transitions(rows) -> list[datetime]:
    """Every stopped-then-moving transition in `rows`, earliest first.

    Split out so the day board can ask about ten sailings from a single read of the table
    without the rule living in two places — the board and the record disagreeing about
    whether a sailing has gone would be worse than either being wrong alone.

    Every one of them, and not just the last. A window wide enough to catch a late sailing
    is also wide enough to hold a whole round trip on a fifty-minute crossing, so its last
    transition is the far terminal's departure — and when the vessel is still tied up over
    there as the window closes, there is no last transition at all. That is what emptied the
    Earls Cove column: any sailing punctual enough to get across and berth reported nothing,
    and only one over half an hour late reported at all.

    Says nothing about *which* terminal each was left from; that is the caller's to settle.
    A vessel stopping mid-crossing would register here as a departure, which is the one case
    this shape cannot distinguish — rare enough, and the sailing window bounds the damage.
    """
    if not rows:
        return []

    # Classified here rather than matched in SQL, so an unrecognised status word falls
    # through to speed instead of quietly matching nothing.
    moving = [is_moving(row["status"], row["speed_knots"]) for row in rows]
    found = []
    berthed = False
    for row, state in zip(rows, moving, strict=True):
        if state is False:
            berthed = True
        elif state is True and berthed:
            found.append(parse_iso(row["reported_at"]))
            berthed = False
    # A silent reading is neither: it says nothing, and being silent is not the same as
    # being tied up, so it can neither open a berthing nor close one.
    return found


@dataclass(frozen=True)
class TrackerWatch:
    """What the vessel tracker can say about today's sailings from one terminal.

    Deliberately three-valued, like the board's own watch. `departed` and `not_away` are
    both positive findings; a sailing in neither is one the tracker declines to speak about,
    and the caller falls back to whatever it would have done without a tracker.

    That abstention is the important part. A sailing far enough past its time is beyond what
    a departure window can reason about, and a tracker that has stopped publishing knows
    nothing at all — in both cases claiming "not away yet" would tell somebody a boat they
    have missed is still catchable, which is the direction this project is not allowed to be
    wrong in.
    """

    departed: frozenset[str] = frozenset()
    not_away: frozenset[str] = frozenset()
    observed_at: datetime | None = None

    def is_fresh(self, now: datetime, within: timedelta) -> bool:
        return self.observed_at is not None and now - self.observed_at <= within


def tracker_watch(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    times: list[str],
    now: datetime,
    grace: timedelta = timedelta(minutes=20),
    route_id: str | None = None,
) -> TrackerWatch:
    """Which of today's sailings the tracker has watched leave, and which are still here.

    One read of the table answers the whole timetable. Only sailings whose time has already
    come are considered — a boat that is not due has obviously not gone, and the board says
    so with a countdown rather than with this.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    if not times:
        return TrackerWatch()

    scheduled = {
        hhmm: combine_local(target_date, parse_hhmm(hhmm), config.tz) for hhmm in times
    }
    first = min(scheduled.values()) - grace
    rows = conn.execute(
        """SELECT status, speed_knots, reported_at FROM vessel_positions
            WHERE route = ? AND fetch_status = 'ok' AND reported_at IS NOT NULL
              AND reported_at >= ? AND reported_at <= ?
            ORDER BY reported_at""",
        (route.id, iso(first), iso(now)),
    ).fetchall()
    if not rows:
        return TrackerWatch()

    stamps = [parse_iso(row["reported_at"]) for row in rows]
    # One read for the whole day, same as the positions: every sailing is checked against
    # the departures the other end published, so a transition the board already accounts
    # for is never also counted as this terminal's.
    published = _published_elsewhere(
        conn, config, route_id=route.id, origin=origin, window=(first, now)
    )
    departed: set[str] = set()
    not_away: set[str] = set()
    for hhmm, when in scheduled.items():
        if when > now:
            continue
        window = [
            row
            for row, at in zip(rows, stamps, strict=True)
            if when - grace <= at <= when + LATE_TOLERANCE
        ]
        left = next(
            (at for at in _transitions(window) if not _is_claimed(at, published)), None
        )
        if left is not None and left <= now:
            departed.add(hhmm)
        elif now <= when + LATE_TOLERANCE:
            # Still inside the span a departure could turn up in, and none has. That is a
            # positive finding: the boat is late, not missed.
            not_away.add(hhmm)
    return TrackerWatch(frozenset(departed), frozenset(not_away), stamps[-1])
