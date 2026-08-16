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
*is* a departure, and the only open question is which end it left.

That question is answered for the day at once rather than for each sailing separately, in
`departure_ledger`. The boat must arrive somewhere before it can leave again, so departures
strictly alternate between the two ends; the Saltery Bay board publishes its own departures
to the minute several times a day, and each one it publishes fixes the parity of the whole
sequence — including the departures it has not got round to publishing yet, and every Earls
Cove departure, which it never publishes at all. The compass heading is not read: it is a
proxy for geometry where the alternation is a consequence of the timetable being physically
possible.

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
from .schedule import load_schedule_cached, sailings_for_day
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


#: How late a sailing may leave and still be paired with its scheduled time. Comfortably
#: inside this route's tightest headway (110 minutes at Earls Cove), so a sailing cannot be
#: handed the departure that belonged to the next one.
LATE_TOLERANCE = timedelta(minutes=75)

#: How close a tracked transition may sit to a departure the board published before it is
#: taken to *be* that departure. Consecutive departures are at least a crossing apart —
#: about fifty minutes here — so fifteen minutes is generous against feed lag while nowhere
#: near the real separation.
SAME_DEPARTURE = timedelta(minutes=15)

#: The least that can separate two consecutive departures on a two-point shuttle: the vessel
#: has to cross and turn around. Anything closer is not the next departure but a reading the
#: alternation cannot account for — a stop mid-crossing, most likely — so the chain is cut
#: there rather than carried across a step that cannot be real. Well under the true
#: half-cycle (a fifty-minute crossing plus a turnaround) and well over the feed's jitter.
MIN_SEPARATION = timedelta(minutes=30)


def _published_departures(
    conn: sqlite3.Connection, config: Config, *, route_id: str, span: tuple
) -> list[tuple[datetime, str]]:
    """Every departure the board published inside `span`, with the terminal that published it.

    These are the ledger's anchors, and they are hard evidence rather than inference: the
    operator states that a sailing left Saltery Bay at 5:51, so a transition matching it is
    Saltery Bay's. Every terminal that publishes contributes; on this route that is one of
    the two, which is enough, because fixing one departure in an alternating sequence fixes
    all of them.
    """
    start, end = span
    days = sorted({(start.date() - timedelta(days=1)), start.date(), end.date()})
    placeholders = ",".join("?" for _ in days)
    rows = conn.execute(
        f"""SELECT DISTINCT terminal, service_date, sailing_hhmm, departed_hhmm FROM deck_space
             WHERE route = ? AND fetch_status = 'ok'
               AND departed_hhmm IS NOT NULL
               AND service_date IN ({placeholders})""",
        (route_id, *(d.isoformat() for d in days)),
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
            found.append((left, row["terminal"]))
    return sorted(found)


@dataclass(frozen=True)
class Departure:
    """One tracked departure, and the end it left from once the ledger can say.

    `terminal` is None where the ledger declines to name one. That is not the same as the
    departure not having happened: the vessel certainly went somewhere, we just cannot say
    from which end, and a caller must not read it as either terminal's.
    """

    at: datetime
    terminal: str | None = None


@dataclass(frozen=True)
class Ledger:
    """The day's departures in order, each labelled with the end it left from.

    Empty means the tracker cannot speak to this day at all, which is different from a day
    with no departures in it — `observed_at` tells them apart, and is what freshness is
    judged on.
    """

    departures: tuple[Departure, ...] = ()
    observed_at: datetime | None = None

    def left_from(self, terminal: str) -> list[datetime]:
        return [d.at for d in self.departures if d.terminal == terminal]

    @property
    def named_any(self) -> bool:
        """Whether the ledger managed to name a terminal for anything at all."""
        return any(d.terminal for d in self.departures)


def departure_ledger(
    conn: sqlite3.Connection,
    config: Config,
    *,
    target_date: date,
    until: datetime | None = None,
    route_id: str | None = None,
) -> Ledger:
    """Every departure the tracker saw on `target_date`, each attributed to an end.

    This is a two-point shuttle with a single vessel, and that premise does the work that
    was previously attempted per sailing from a slice of time. The boat has to *arrive*
    somewhere before it can leave again, so departures strictly alternate between the two
    ends — which makes the end a departure left from a property of the whole day's sequence,
    not something to be inferred afresh from each one.

    So the parity of any single departure fixes the parity of every other, and the board
    supplies parity for free: Saltery Bay publishes its departures to the minute, several
    times a day. A transition matching one of those is Saltery Bay's, and the alternation
    carries that outward in both directions — forward over the departures the board has not
    posted yet, which is the case this exists for. Saltery Bay routinely takes half an hour
    to publish, and Earls Cove never publishes at all.

    What this replaced was a per-sailing search over a window from twenty minutes before the
    scheduled time to seventy-five after, which asked "which of these transitions is not
    accounted for at the other end?". That question has no answer at Saltery Bay, because
    the other end is Earls Cove and it publishes nothing to subtract — so the search fell
    back to taking the earliest transition in the window, unguarded. On a day running late
    the previous Earls Cove sailing lands inside that window and *before* the real one, so
    the earliest transition is the wrong end's, and Saltery Bay would have been told its
    boat had gone while it was still crossing towards the berth. Alternation needs nothing
    subtracted and so has no such blind side.

    Being able to check itself is the other half. Two anchors an even number of steps apart
    have to name the same end, and an odd number apart opposite ends; where they do not, a
    transition was missed — a feed gap swallowing a whole turnaround, or a vessel stopping
    mid-crossing and registering as a departure. The chain is cut there and the stretch
    between those anchors goes unnamed, rather than the error propagating silently through
    every departure after it. A window search cannot do this: it has nothing to be
    inconsistent with.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    codes = route.codes
    # Alternation is only meaningful between exactly two ends. A route with more is not a
    # shuttle and every claim below would be unfounded on it.
    if len(codes) != 2:
        return Ledger()

    start = combine_local(target_date, time(0, 0), config.tz)
    end = until or start + timedelta(days=1)
    rows = conn.execute(
        """SELECT vessel, status, speed_knots, reported_at FROM vessel_positions
            WHERE route = ? AND fetch_status = 'ok' AND reported_at IS NOT NULL
              AND reported_at >= ? AND reported_at <= ?
            ORDER BY reported_at""",
        (route.id, iso(start), iso(end)),
    ).fetchall()
    if not rows:
        return Ledger()
    observed_at = parse_iso(rows[-1]["reported_at"])

    # One vessel is a premise, not a detail: two boats interleave their transitions into one
    # sequence and the alternation is no longer about a single hull going back and forth.
    # Asserted rather than assumed, because the feed would report a second vessel without
    # any other sign that the reasoning below had stopped being true.
    if len({row["vessel"] for row in rows if row["vessel"]}) > 1:
        return Ledger(observed_at=observed_at)

    moments = _transitions(rows)
    if not moments:
        return Ledger(observed_at=observed_at)

    anchored = _anchors(
        moments, _published_departures(conn, config, route_id=route.id, span=(start, end))
    )
    labels = _label_by_alternation(moments, anchored, codes)
    return Ledger(
        tuple(Departure(at, label) for at, label in zip(moments, labels, strict=True)),
        observed_at,
    )


def _anchors(
    moments: list[datetime], published: list[tuple[datetime, str]]
) -> dict[int, str]:
    """Which departures the board has already named an end for, by position in the sequence."""
    found = {}
    for index, moment in enumerate(moments):
        for at, terminal in published:
            if abs((moment - at).total_seconds()) <= SAME_DEPARTURE.total_seconds():
                found[index] = terminal
                break
    return found


def _label_by_alternation(
    moments: list[datetime], anchored: dict[int, str], codes: tuple[str, ...]
) -> list[str | None]:
    """Spread each anchor's end outward by alternation, cutting the chain where it cannot hold.

    A stretch with no anchor in it goes unnamed. That is the conservative answer and it is
    the right one: the alternation is only ever as good as the fact it is anchored to, and
    with nothing to anchor to there is no fact, only a pattern.
    """
    breaks = {
        index
        for index in range(1, len(moments))
        if moments[index] - moments[index - 1] < MIN_SEPARATION
    }

    # Anchors that disagree about parity mean a transition between them was missed. Neither
    # anchor is in doubt — the board published both — so both keep their own end and go on
    # anchoring their own side; it is only the stretch between them that cannot be walked.
    ordered = sorted(anchored)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        agrees = (anchored[earlier] == anchored[later]) == ((later - earlier) % 2 == 0)
        if not agrees:
            breaks.update({earlier + 1, later})

    labels: list[str | None] = [None] * len(moments)
    opened = 0
    for boundary in sorted(breaks) + [len(moments)]:
        segment = range(opened, boundary)
        anchor = next((index for index in segment if index in anchored), None)
        if anchor is not None:
            base = codes.index(anchored[anchor])
            for index in segment:
                labels[index] = codes[(base + index - anchor) % 2]
        opened = boundary
    return labels


def _transitions(rows) -> list[datetime]:
    """Every stopped-then-moving transition in `rows`, earliest first.

    Split out so the day board can ask about ten sailings from a single read of the table
    without the rule living in two places — the board and the record disagreeing about
    whether a sailing has gone would be worse than either being wrong alone.

    Says nothing about *which* end each was left from: that is `departure_ledger`'s to
    settle, from the sequence as a whole. A vessel stopping mid-crossing registers here as a
    departure, which is the one case this shape cannot tell apart on its own — the ledger
    catches it downstream, as a step too short to be a real crossing or as two anchors that
    no longer agree on parity.
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


def _sailing_times(config: Config, route, origin: str, target_date: date) -> list[str]:
    blocks = load_schedule_cached(config.schedule_path)
    return [
        s.depart_hhmm
        for s in sailings_for_day(
            blocks, target_date, route.id, route.destinations, config.tz, origin=origin
        )
    ]


def _pair_with_sailings(
    departures: list[datetime], scheduled: dict[str, datetime], grace: timedelta
) -> dict[str, datetime]:
    """Give each scheduled sailing the tracked departure that was its own.

    Both sequences are in order and a departure can only belong to one sailing, so this is
    an ordered, exclusive hand-out rather than a search: each sailing takes the earliest
    departure still going that could plausibly be its own, and that departure is then spent.
    The plausibility bound is what makes it refuse rather than reach — a sailing whose own
    departure was never tracked is left with nothing instead of being handed the next one's.
    """
    remaining = sorted(departures)
    paired: dict[str, datetime] = {}
    for hhmm, when in sorted(scheduled.items(), key=lambda item: item[1]):
        match = next(
            (at for at in remaining if when - grace <= at <= when + LATE_TOLERANCE), None
        )
        if match is not None:
            paired[hhmm] = match
            remaining.remove(match)
    return paired


def departures_by_sailing(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    until: datetime | None = None,
    times: list[str] | None = None,
    grace: timedelta = timedelta(minutes=20),
    route_id: str | None = None,
) -> dict[str, datetime]:
    """`HH:MM` -> when the tracker saw that sailing leave `origin`, for one service date.

    The single place a tracked departure is turned into a claim about a scheduled sailing,
    so the record and the live board cannot come to different conclusions from the same
    readings.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    hhmms = times if times is not None else _sailing_times(config, route, origin, target_date)
    if not hhmms:
        return {}
    ledger = departure_ledger(
        conn, config, target_date=target_date, until=until, route_id=route.id
    )
    scheduled = {
        hhmm: combine_local(target_date, parse_hhmm(hhmm), config.tz) for hhmm in hhmms
    }
    return _pair_with_sailings(ledger.left_from(origin), scheduled, grace)


def departure_from_tracking(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    departure: datetime,
    grace: timedelta = timedelta(minutes=20),
    route_id: str | None = None,
) -> datetime | None:
    """When the vessel left `origin` on this sailing, as the tracker saw it, or None.

    The first moving reading after it was last seen stopped, which is an upper bound: it had
    certainly gone by then, and reading a residual queue slightly late is the safe direction
    to be wrong in.

    Which end that transition was from comes from `departure_ledger` — the day's departures
    alternate between the two terminals, so the board naming one names them all — and never
    from the compass heading, which tried to derive the same fact from a three-character
    string and got it wrong in production. Earls Cove sits in a narrow cove, so the vessel
    swings north-east clear of it before turning north-west across the strait, and `NE`
    contains an `E`: an outbound sailing read as inbound, and the 15:40 that left at 16:26
    still showed "not away yet" ten minutes later.
    """
    here = local(departure, config.tz)
    return departures_by_sailing(
        conn,
        config,
        origin=origin,
        target_date=here.date(),
        grace=grace,
        route_id=route_id,
    ).get(here.strftime("%H:%M"))


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

    ledger = departure_ledger(
        conn, config, target_date=target_date, until=now, route_id=route.id
    )
    # A ledger holding departures it could name no end for knows nothing about this
    # terminal, and its silence must not be read as "nothing has left here" — that is the
    # reading that tells somebody a boat they have missed is still catchable. No departures
    # at all is a different and stronger statement: the vessel was watched and never went
    # anywhere, which *is* evidence it is still tied up.
    if ledger.departures and not ledger.named_any:
        return TrackerWatch(observed_at=ledger.observed_at)

    scheduled = {
        hhmm: combine_local(target_date, parse_hhmm(hhmm), config.tz) for hhmm in times
    }
    paired = _pair_with_sailings(ledger.left_from(origin), scheduled, grace)

    departed: set[str] = set()
    not_away: set[str] = set()
    for hhmm, when in scheduled.items():
        if when > now:
            continue
        left = paired.get(hhmm)
        if left is not None and left <= now:
            departed.add(hhmm)
        elif now <= when + LATE_TOLERANCE:
            # Still inside the span a departure could turn up in, and none has. That is a
            # positive finding: the boat is late, not missed.
            not_away.add(hhmm)
    return TrackerWatch(frozenset(departed), frozenset(not_away), ledger.observed_at)
