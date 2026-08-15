"""Vessel tracking — where the ship is, for the direction no board covers.

BC Ferries publishes a departures board for Saltery Bay and none at all for Earls Cove (see
`deckspace.publishes_departures`), so the homeward direction has no source of a departure
time. It does publish a live vessel tracker, and both directions' conditions pages embed the
same one: a small page carrying each vessel's status (`In Port` / `Under Way`), compass
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

Which terminal a vessel is docked at is **not** published: the status column says `In Port`
without naming the port. Direction therefore comes from the heading once it is moving, which
is why `Terminal.outbound_bearing` is configuration rather than something inferred here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import Config
from .db import JobRun
from .fetching import fetch
from .timeutil import combine_local, iso, local, now_utc, parse_iso

#: Status words the feed uses. Anything else is stored verbatim and treated as unknown.
IN_PORT = "In Port"
UNDER_WAY = "Under Way"

_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E"}

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
        return self.status == UNDER_WAY

    @property
    def in_port(self) -> bool:
        return self.status == IN_PORT


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


def _sense(heading: str, bearing: str) -> bool | None:
    """Is this heading outbound (True), inbound (False), or silent on the question (None)?

    Judged only along the axis the terminal's bearing names. For a crossing that runs
    east-west, `NW` is outbound from the eastern end and `N` says nothing at all — and
    "nothing at all" has to stay distinct from "inbound", because a vessel manoeuvring off
    the berth can point anywhere for a reading or two. Treating that as an answer would file
    the departure against the wrong terminal.
    """
    heading = (heading or "").strip().upper()
    bearing = (bearing or "").strip().upper()
    if not heading or not bearing:
        return None
    opposite = _OPPOSITE.get(bearing)
    if opposite is None:
        return None
    if bearing in heading and opposite not in heading:
        return True
    if opposite in heading and bearing not in heading:
        return False
    return None


#: How late a sailing may leave and still be recognised. Comfortably inside this route's
#: tightest headway (110 minutes at Earls Cove), so one sailing's window cannot reach into
#: the next one's departure.
LATE_TOLERANCE = timedelta(minutes=75)


def departure_from_tracking(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    departure: datetime,
    grace: timedelta = timedelta(minutes=20),
) -> datetime | None:
    """When the vessel left `origin`, as the tracker saw it — or None if it cannot say.

    Returns the first moving reading after the vessel was last seen stopped, which is an
    upper bound: it had certainly gone by then, and reading a residual queue slightly late
    is the safe direction to be wrong in.

    Two things must both hold, and either one missing means no answer rather than a guess:

    * the vessel was seen **stopped and then moving** inside the window, so we are looking at
      a departure rather than the middle of a crossing;
    * the first reading that commits to a direction commits to *this terminal's* outbound
      one. The feed never names the port a vessel is docked at, so the heading is the only
      thing that distinguishes leaving Earls Cove from leaving Saltery Bay.
    """
    bearing = config.route.terminal(origin).outbound_bearing
    if not bearing:
        return None

    rows = conn.execute(
        """SELECT status, heading, reported_at FROM vessel_positions
            WHERE route = ? AND fetch_status = 'ok' AND reported_at IS NOT NULL
              AND reported_at >= ? AND reported_at <= ?
            ORDER BY reported_at""",
        (config.route.id, iso(departure - grace), iso(departure + LATE_TOLERANCE)),
    ).fetchall()
    if not rows:
        return None

    stopped = [i for i, row in enumerate(rows) if row["status"] == IN_PORT]
    if not stopped:
        return None
    after = [row for row in rows[stopped[-1] + 1 :] if row["status"] == UNDER_WAY]
    if not after:
        return None

    committed = [s for s in (_sense(row["heading"], bearing) for row in after) if s is not None]
    if not committed or not committed[0]:
        return None
    return parse_iso(after[0]["reported_at"])
