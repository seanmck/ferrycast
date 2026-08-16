"""R4 — sailing-level aggregation.

Per-frame observations roll up into one record per scheduled sailing. The core inference,
straight from the PRD: given frames before and after a scheduled departure, if the queue
does not drop near zero afterwards, the sailing was overloaded and the remaining vehicles
are the carryover.

A record carries two orthogonal claims rather than one overloaded word:

    filled       — did the vessel run out of room? Only the board can attest this.
    left_behind  — was anyone provably left on the tarmac? Only a camera residual or
                   somebody who was in the line can attest this.

Both are three-valued: None means nobody has said, which is not "no". Keeping them apart is
what lets the page distinguish a sailing that filled and still took everyone — the tightest
kind of success, and the one nothing else can show — from one that turned people away.

`outcome` is derived from the pair and kept for everything that reads a single word (the day
board, the export, the CLI, an imported CSV of outcomes):

    boarded      — had room, or the queue cleared; a traveller in it made this sailing
    filled       — ran out of room, with no count of who waited
    waited_1     — left behind, and the carryover fits the next sailing
    waited_2plus — carryover exceeds one vessel's capacity
    cancelled    — a queue persisted and no vessel ever appeared
    unknown      — neither axis has an answer (night, fog, capture gap)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .db import JobRun
from .deckspace import notice_says_full, status_says_departed
from .extent import PROMPT_VERSION as EXTENT_PROMPT_VERSION
from .lanes import PROMPT_VERSION as LANE_PROMPT_VERSION
from .lanes import load_for as load_lane_calibration
from .reports import fetch_reports, outcome_from_reports, report_confidence
from .schedule import Sailing, load_schedule_cached, sailings_for_day
from .timeutil import iso, local, now_utc, parse_iso
from .vessels import departure_from_tracking
from .vision import FULLNESS_LEVELS

OUTCOMES = ("boarded", "waited_1", "waited_2plus", "filled", "cancelled", "unknown")

# Outcomes that mean "you would have waited if you turned up late", whatever the evidence.
MISSED_OUTCOMES = ("waited_1", "waited_2plus", "filled")


@dataclass
class SailingRecord:
    sailing_id: int
    peak_queue: int | None
    queue_at_departure: int | None
    residual_queue: int | None
    carryover: int | None
    overload: bool
    cancelled: bool
    outcome: str
    n_frames: int
    confidence: float | None
    queue_truncated: bool
    deck_space_min: int | None
    method: str
    filled_at: str | None = None
    # Band fields, populated from prompt v2 onward. See `_classify_from_bands`.
    peak_fullness: str | None = None
    fullness_at_departure: str | None = None
    residual_fullness: str | None = None
    queue_started_at: str | None = None
    cleared_at: str | None = None
    #: The board's capacity notice, verbatim from the feed. `filled` supersedes it as the
    #: page-facing field; this stays because it says *the board* is the one who said so.
    left_full: bool | None = None
    #: The two axes. None on either means no source was entitled to speak to it.
    filled: bool | None = None
    left_behind: bool | None = None
    #: When the vessel actually left, and who saw it go. The board is to the minute; the
    #: vessel tracker is good to about five, so the source travels with the timestamp.
    departed_at: str | None = None
    departed_source: str | None = None


@dataclass
class _Obs:
    at: datetime  # UTC
    vehicle_count: int | None
    ferry_at_dock: bool
    beyond_frame: bool
    confidence: float
    fullness: str | None = None
    #: Which extractor produced this reading, so the record can say what it rests on.
    source: str = ""

    @property
    def occupied(self) -> bool | None:
        """Whether anything was queued. None when this frame cannot say."""
        if self.fullness is not None:
            return self.fullness != "empty"
        if self.vehicle_count is not None:
            return self.vehicle_count > 0
        return None


def upsert_sailings(conn: sqlite3.Connection, sailings: list[Sailing]) -> int:
    written = 0
    for s in sailings:
        cur = conn.execute(
            """INSERT INTO sailings
                   (route, origin, destination, service_date, scheduled_departure,
                    depart_hhmm, day_type, season)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (route, origin, scheduled_departure) DO UPDATE SET
                   day_type = excluded.day_type,
                   season   = excluded.season""",
            (
                s.route,
                s.origin,
                s.destination,
                s.service_date.isoformat(),
                s.scheduled_departure.isoformat(),
                s.depart_hhmm,
                s.day_type,
                s.season,
            ),
        )
        written += cur.rowcount or 0
    conn.commit()
    return written


def _load_observations(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    start: datetime,
    end: datetime,
    *,
    usable_only: bool = True,
) -> list[_Obs]:
    """Readings for this terminal's frames, geometry preferred over the model.

    All the extractors write here, keyed by prompt version, and a frame can carry more than
    one reading. Geometry wins where it exists: it is measured against known positions
    rather than judged, so it cannot report a lane it is unable to see — which is exactly
    how the model path went wrong, calling a compound clear while a queue stood in the
    lanes whose numbers are not painted in view. The two geometric generations never share
    a frame — the lane grid reads the terminal camera and `extent` reads the highway one —
    so no order between them is ever exercised.

    Filtering on `config.vision.prompt_version` alone, as this did, meant aggregation never
    saw a geometric reading at all. Every frame was read and every record stayed `unknown`.

    Frames are matched by terminal, not by camera, and that is what routes the highway
    camera in: its frames are stored under the terminal it watches the approach to, so its
    bands land in the same window as everything else Earls Cove has to say.
    """
    sql = """
        SELECT o.frame_id, o.prompt_version, f.captured_at, o.vehicle_count, o.ferry_at_dock,
               o.queue_beyond_frame, o.fullness, o.confidence, o.usable
          FROM observations o
          JOIN frames f ON f.id = o.frame_id
         WHERE o.prompt_version IN (?, ?, ?)
           AND f.terminal = ?
           AND f.captured_at >= ?
           AND f.captured_at <= ?
    """
    params = [
        LANE_PROMPT_VERSION,
        EXTENT_PROMPT_VERSION,
        config.vision.prompt_version,
        terminal,
        iso(start),
        iso(end),
    ]
    if usable_only:
        sql += " AND o.usable = 1"
    sql += " ORDER BY f.captured_at"

    best: dict[int, sqlite3.Row] = {}
    for row in conn.execute(sql, tuple(params)).fetchall():
        current = best.get(row["frame_id"])
        if current is None or row["prompt_version"] != config.vision.prompt_version:
            best[row["frame_id"]] = row

    return [
        _Obs(
            at=parse_iso(row["captured_at"]),
            vehicle_count=row["vehicle_count"],
            ferry_at_dock=bool(row["ferry_at_dock"]),
            beyond_frame=bool(row["queue_beyond_frame"]),
            confidence=float(row["confidence"] or 0.0),
            fullness=row["fullness"],
            source=row["prompt_version"],
        )
        for row in sorted(best.values(), key=lambda r: r["captured_at"])
    ]


def _deck_space_min(
    conn: sqlite3.Connection, route: str, terminal: str, service_date: str, hhmm: str
) -> int | None:
    row = conn.execute(
        """SELECT MIN(percent_available) FROM deck_space
            WHERE route = ? AND terminal = ? AND service_date = ? AND sailing_hhmm = ?
              AND percent_available IS NOT NULL""",
        (route, terminal, service_date, hhmm),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _board_departure(
    conn: sqlite3.Connection,
    route: str,
    terminal: str,
    service_date: str,
    hhmm: str,
    config: Config,
) -> datetime | None:
    """When the board said this sailing actually left.

    The published departure works where the camera cannot: at night, in fog, and — on this
    route, at both terminals — where the camera faces the approach road rather than the
    berth. Reported to the minute rather than to the nearest 15-minute frame.

    The latest reading wins: the board fills the time in once the vessel has gone, so the
    most recent scrape is the one that has it.
    """
    row = conn.execute(
        """SELECT departed_hhmm FROM deck_space
            WHERE route = ? AND terminal = ? AND service_date = ? AND sailing_hhmm = ?
              AND departed_hhmm IS NOT NULL AND fetch_status = 'ok'
            ORDER BY observed_at DESC
            LIMIT 1""",
        (route, terminal, service_date, hhmm),
    ).fetchone()
    if row is None:
        return None
    from .timeutil import combine_local, parse_hhmm

    departed = combine_local(date.fromisoformat(service_date), parse_hhmm(row[0]), config.tz)
    # A sailing scheduled at 23:50 that leaves at 00:05 belongs to the previous service
    # date. Without this the "actual" departure lands 24 hours early.
    scheduled = combine_local(date.fromisoformat(service_date), parse_hhmm(hhmm), config.tz)
    if departed < scheduled - timedelta(hours=12):
        departed += timedelta(days=1)
    return departed


def _deck_space_series(
    conn: sqlite3.Connection, route: str, terminal: str, service_date: str, hhmm: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT observed_at, percent_available, status_text, departed_hhmm
                 FROM deck_space
                WHERE route = ? AND terminal = ? AND service_date = ? AND sailing_hhmm = ?
                  AND fetch_status = 'ok'
                ORDER BY observed_at""",
            (route, terminal, service_date, hhmm),
        ).fetchall()
    )


def classify_from_deck_space(
    series: list[sqlite3.Row], departure: datetime
) -> tuple[str, str | None, float | None]:
    """Derive an outcome from the published deck-space feed alone.

    Returns (outcome, filled_at, confidence).

    This is the free signal, and it is weaker than counting the queue: deck space describes
    space aboard the vessel, not vehicles still waiting on the approach road. So it can say
    a sailing *filled* — and when — but never how many were left behind. That is why filling
    gets its own outcome rather than being folded into `waited_1`: claiming someone waited
    exactly one sailing would be asserting something this evidence cannot support.
    """
    readings = [row for row in series if row["percent_available"] is not None]
    if any(
        (row["status_text"] or "").lower() == "cancelled"
        for row in series
        if row["status_text"]
    ):
        return "cancelled", None, 0.6
    if not readings:
        return _classify_from_departures_board(series)

    before = [row for row in readings if parse_iso(row["observed_at"]) <= departure]
    if not before:
        return "unknown", None, None

    full = [row for row in before if row["percent_available"] <= 0]
    if full:
        return "filled", full[0]["observed_at"], 0.7

    # Never observed full. How confident that is depends on how close the last reading
    # sits to departure — a feed that stopped an hour early proves much less.
    last_gap = (departure - parse_iso(before[-1]["observed_at"])).total_seconds() / 60
    if last_gap > 45:
        return "unknown", None, None
    confidence = 0.65 if last_gap <= 20 else 0.5
    return "boarded", None, confidence


#: How long the board must stay noteless after a departure first shows before its silence
#: counts. The capacity note rides the same row as the departed status, so one scrape that
#: caught "Departed" a moment before the note landed must not become "had space".
DEPARTED_NOTE_WAIT = timedelta(minutes=15)


def _row_departed(row: sqlite3.Row) -> bool:
    """Whether this scrape saw the sailing marked departed.

    The column where the parser has filled it in; the status text for rows stored before
    the column existed — the phrase was always on the board, it just was not parsed out.
    """
    return bool(row["departed_hhmm"]) or status_says_departed(row["status_text"])


def _classify_from_departures_board(
    series: list[sqlite3.Row],
) -> tuple[str, str | None, float | None]:
    """On a board that never publishes a percentage, the departures column still speaks.

    Route 7's conditions page has never once carried a deck-space figure — 2368 rows, zero
    percentages — so the numeric path above classifies nothing there. What the board does
    publish is "Departed h:mm" for every sailing that ran, and, beside the ones that loaded
    to capacity, the "loading maximum number of vehicles" note. A sailing the operator
    watched leave whose row never acquired the note is one that had room: `boarded`.

    The note case returns nothing from here: it is `left_full`'s to assert (see
    `compute_record`), and it carries no fill *time*, which is the other thing this
    function exists to report. The silence case is guarded by `DEPARTED_NOTE_WAIT` and
    priced below the percentage path's confidence — "they did not say full" is the
    operator's habit read in reverse, not a measurement.
    """
    departed = [row for row in series if _row_departed(row)]
    if not departed:
        return "unknown", None, None
    if any(notice_says_full(row["status_text"]) for row in series):
        return "unknown", None, None
    first_seen = min(parse_iso(row["observed_at"]) for row in departed)
    watched_until = max(parse_iso(row["observed_at"]) for row in series)
    if watched_until - first_seen < DEPARTED_NOTE_WAIT:
        return "unknown", None, None
    return "boarded", None, 0.55


def _departure_from_frames(observations, grace_from) -> tuple[datetime | None, bool]:
    """When the vessel actually left, and whether it was still there at the end.

    Returns (left_at, still_at_dock). `left_at` is the first frame showing an empty berth
    after one that showed a vessel — the best the camera can say about a departure. It is
    None both when no vessel was ever seen and when one never left.
    """
    window = [o for o in observations if o.at >= grace_from]
    dock_times = [o.at for o in window if o.ferry_at_dock]
    if not dock_times:
        return None, False
    last_dock = max(dock_times)
    gone = [o for o in window if o.at > last_dock]
    if not gone:
        # A vessel was berthed in the last frame we have. It has not left yet — which is
        # not the same as never leaving, and must not be read as a cancellation.
        return None, True
    return gone[0].at, False


def classify(
    *,
    residual: int | None,
    queue_at_departure: int | None,
    departure_seen: bool,
    capacity: int,
    residual_threshold: int,
    still_at_dock: bool = False,
    berth_visible: bool = False,
) -> tuple[str, bool, bool, int | None]:
    """Return (outcome, overload, cancelled, carryover)."""
    if residual is None or queue_at_departure is None:
        return "unknown", False, False, None

    # A vessel berthed in the last frame means the sailing had not gone by the time the
    # evidence runs out. Anything else is a guess: the queue in shot may be boarding.
    if still_at_dock:
        return "unknown", False, False, None

    if residual < residual_threshold:
        # Queue cleared. Even with no vessel positively identified, an emptied compound
        # means the sailing ran.
        return "boarded", False, False, 0

    if not departure_seen:
        # A queue that persists with no vessel ever seen is a cancellation ONLY where the
        # camera can see the berth. Earls Cove's points up the approach road, so "no ferry
        # in any frame" is the normal state there and says nothing about whether the
        # sailing ran — reading it as a cancellation marked every busy sailing cancelled.
        if not berth_visible:
            return "unknown", False, False, None
        return "cancelled", False, True, residual

    if residual > capacity:
        return "waited_2plus", True, False, residual
    return "waited_1", True, False, residual


def _peak_band(observations: list[_Obs]) -> str | None:
    """The fullest band reached. Ordered comparison, not string comparison."""
    ranked = [
        FULLNESS_LEVELS.index(o.fullness)
        for o in observations
        if o.fullness in FULLNESS_LEVELS
    ]
    return FULLNESS_LEVELS[max(ranked)] if ranked else None


def _first_occupied_at(observations: list[_Obs]) -> str | None:
    """When a queue first appeared.

    Approximate by construction, and worth stating as such: the first two or three vehicles
    are the hardest thing in the frame to see, so this lands within a frame or so rather than
    on the minute. It is the weakest of the stored transitions — `cleared_at` is the sharp one.
    """
    for o in observations:
        if o.occupied:
            return iso(o.at)
    return None


def _cleared_at(observations: list[_Obs], settle_from: datetime) -> str | None:
    """When the compound actually emptied.

    Previously this searched from `settle_from` onward, which is at least twelve minutes
    *after* departure — so it could only ever return a time later than the sailing left, and
    a page showing both read as nonsense. It was not measuring the clear at all; it was
    measuring the first frame that later confirmed one.

    The compound empties while the vessel loads, before it goes: on 2026-08-12 the tarmac was
    bare at 12:00 and the 11:45 departed at 12:03. So the clear is the transition — the first
    empty frame after the last occupied one — and it usually lands before departure.

    None means it never emptied, which is what an overload looks like and is exactly the case
    the residual is there to catch.
    """
    last_occupied = None
    for o in observations:
        if o.at > settle_from:
            break
        if o.occupied:
            last_occupied = o.at
    if last_occupied is None:
        # Nothing was ever queued, so there was no clear to observe.
        return None
    for o in observations:
        if o.at > last_occupied and o.occupied is False:
            return iso(o.at)
    return None


def classify_from_bands(
    *,
    residual_fullness: str | None,
    fullness_at_departure: str | None,
    departure_seen: bool,
    still_at_dock: bool = False,
    berth_visible: bool = False,
    empty_when_it_left: bool = False,
    clear_requires_departure: bool = False,
) -> tuple[str, bool, bool, int | None]:
    """Return (outcome, overload, cancelled, carryover) from fullness bands alone.

    Same shape as `classify`, one deliberate difference: this never returns `waited_1` or
    `waited_2plus`.

    Those two outcomes claim to know *how many* sailings someone waited, which a count could
    support and a band cannot — "heavy" left on the tarmac is one vessel's worth or three
    depending on what those vehicles are. So an overload becomes `filled`, exactly as it does
    for deck space, which has the same limitation and for the same reason. Saying "you would
    not have got on" is honest; saying "you would have waited exactly one sailing" is not.

    What the band does support is the clear, and it supports it well: across four sailings a
    packed compound went to bare asphalt in a single frame every time. That transition is the
    whole outcome, and it is the one thing measured here that was never ambiguous.

    The clear also decides *whose* queue a residual is, which is what `empty_when_it_left` is
    for. A band after departure only means somebody was left behind if the compound never went
    bare in between; if it did, the vessel took everyone and the vehicles in shot belong to the
    next sailing. The count path gets this free from `residual_threshold` — a handful of early
    arrivals sits under the floor — while a band has no floor to hide behind, because "light"
    is a positive reading rather than a noisy one.
    """
    if residual_fullness is None or fullness_at_departure is None:
        return "unknown", False, False, None

    if still_at_dock:
        return "unknown", False, False, None

    if residual_fullness == "empty":
        # Where "empty" is bare tarmac, a cleared compound alone proves the sailing ran —
        # nothing else carries a queue away. Where it is a licensed inference ("the queue
        # never reached the fitted lanes", Earls Cove), it proves no such thing: five cars
        # can stand all evening below the fitted lanes waiting for a boat that never came,
        # and reading their invisible patience as `boarded` is the false all-clear again,
        # one level up. So a licensed clear also needs something to have seen the sailing
        # go — in practice the vessel tracker, which covers exactly the terminal that
        # needs this.
        if clear_requires_departure and not departure_seen:
            return "unknown", False, False, None
        return "boarded", False, False, 0

    if not departure_seen:
        # Same asymmetry as the count path: only a camera that can see the berth is entitled
        # to read "queue remains, no vessel ever seen" as a cancellation.
        if not berth_visible:
            return "unknown", False, False, None
        return "cancelled", False, True, None

    if empty_when_it_left:
        # A compound bare when the vessel left took everyone standing in it, so whatever is in
        # shot afterwards is the next sailing's queue forming and not a residual. Without this
        # the band path claimed both axes off a single reading: on 2026-08-15 the first sailing
        # of the day emptied seven minutes before departure, two lanes filled again thirteen
        # minutes after it, and the record read "ran out of room, left vehicles behind" — of
        # the emptiest crossing on the timetable, with no board notice anywhere near it.
        #
        # This sits below the `departure_seen` check on purpose. "The compound emptied" is only
        # evidence that a sailing carried it away if something saw a sailing; where nothing
        # did, an empty compound is equally consistent with a cancellation nobody queued for,
        # and that stays the branch above's call.
        return "boarded", False, False, 0

    return "filled", True, False, None


def outcome_from_axes(
    *,
    filled: bool | None,
    left_behind: bool | None,
    cancelled: bool,
    waited: str | None = None,
) -> str:
    """The one `outcome` word the two axes add up to.

    Derived rather than asserted, so the column and the axes can never disagree. Note that a
    sailing which filled and still took everyone is `filled`: it did run out of room, and the
    headline that counts those is answering "will there be room", not "did anyone miss it".
    Which of the two it was is the `left_behind` axis' business, and the page states it there.
    """
    if cancelled:
        return "cancelled"
    if waited in ("waited_1", "waited_2plus"):
        return waited
    if filled:
        return "filled"
    if filled is False or left_behind is False:
        return "boarded"
    return "unknown"


def axes_from_outcome(outcome: str) -> tuple[bool | None, bool | None]:
    """(filled, left_behind) implied by an `outcome` word alone — the lossy direction.

    Only for records whose axis columns were never written: history imported from a CSV of
    outcomes, and databases aggregated before the split. `boarded` collapses two different
    situations — the board seeing space, and the camera seeing the compound empty — into
    "had room and took everyone", which is what the single-word vocabulary always implied.
    """
    if outcome in ("waited_1", "waited_2plus"):
        return True, True
    if outcome == "filled":
        return True, None
    if outcome == "boarded":
        return False, False
    return None, None


def compute_record(
    conn: sqlite3.Connection,
    config: Config,
    sailing_row: sqlite3.Row,
    *,
    previous_departure: datetime | None,
    next_departure: datetime | None,
) -> SailingRecord:
    cfg = config.aggregate
    departure = datetime.fromisoformat(sailing_row["scheduled_departure"])

    window_start = departure - timedelta(minutes=cfg.lookback_minutes)
    if previous_departure:
        window_start = max(window_start, previous_departure + timedelta(minutes=cfg.settle_minutes))
    window_end = departure + timedelta(minutes=cfg.post_window_minutes)
    if next_departure:
        window_end = min(window_end, next_departure - timedelta(minutes=5))

    observations = _load_observations(
        conn, config, sailing_row["origin"], window_start, window_end
    )

    before = [o for o in observations if o.at <= departure]
    counted_before = [o for o in before if o.vehicle_count is not None]
    banded_before = [o for o in before if o.fullness is not None]

    grace_from = departure - timedelta(minutes=cfg.departure_grace_minutes)
    left_at, still_at_dock = _departure_from_frames(observations, grace_from)

    # Never read the residual before the vessel has actually gone. A sailing that leaves 30
    # minutes late is routine here — the live board showed 9:25 departing at 9:56 — and
    # measuring at scheduled+12 would photograph a compound that is still loading, counting
    # every vehicle about to board as left behind.
    #
    # `left_at` is already the first frame showing an empty berth, so no further settle is
    # added on top of it; the normal settle still applies when the sailing ran to time.
    settle_from = max(departure + timedelta(minutes=cfg.settle_minutes), left_at or departure)
    after = [
        o for o in observations if o.at >= settle_from and o.vehicle_count is not None
    ]
    banded_after = [o for o in observations if o.at >= settle_from and o.fullness is not None]

    peak = max((o.vehicle_count for o in counted_before), default=None)
    peak_fullness = _peak_band(banded_before)

    at_departure_candidates = [o for o in counted_before if o.at >= grace_from]
    queue_at_departure = at_departure_candidates[-1].vehicle_count if at_departure_candidates else None
    banded_at_departure = [o for o in banded_before if o.at >= grace_from]
    fullness_at_departure = banded_at_departure[-1].fullness if banded_at_departure else None

    residual = after[0].vehicle_count if after else None
    residual_fullness = banded_after[0].fullness if banded_after else None

    # What the board said about the deck. It speaks to `filled` and never to `left_behind`:
    # "we loaded as many as would fit" and "somebody was left standing on the tarmac" are
    # different claims, and the feed cannot see the approach road to make the second. Held
    # against the camera's view of the compound the pair say something neither can — a
    # sailing that filled AND cleared is the one you would have made by a margin nobody
    # can otherwise see.
    board_series = _deck_space_series(
        conn,
        sailing_row["route"],
        sailing_row["origin"],
        sailing_row["service_date"],
        sailing_row["depart_hhmm"],
    )
    left_full = (
        any(notice_says_full(row["status_text"]) for row in board_series)
        if board_series
        else None
    )

    deck_min = _deck_space_min(
        conn,
        sailing_row["route"],
        sailing_row["origin"],
        sailing_row["service_date"],
        sailing_row["depart_hhmm"],
    )
    # The board's published departure outranks the camera's: it is to the minute rather
    # than to the nearest frame, and it works where the camera cannot see the berth.
    board_departure = _board_departure(
        conn,
        sailing_row["route"],
        sailing_row["origin"],
        sailing_row["service_date"],
        sailing_row["depart_hhmm"],
        config,
    )
    # The vessel tracker, for the direction the board does not cover. It ranks below the
    # board — to the minute versus to about five — and above the camera, which on this route
    # cannot see either berth. It witnesses the ship and nothing else, so it settles *when*
    # a sailing went and never whether anyone got on.
    departed_at = board_departure
    departed_source = "board" if board_departure else None
    if departed_at is None:
        tracked = departure_from_tracking(
            conn,
            config,
            origin=sailing_row["origin"],
            departure=departure,
            grace=timedelta(minutes=cfg.departure_grace_minutes),
        )
        if tracked is not None:
            departed_at, departed_source = tracked, "tracking"

    if departed_at is not None:
        left_at = departed_at
        still_at_dock = False
        settle_from = max(departure + timedelta(minutes=cfg.settle_minutes), departed_at)
        after = [
            o for o in observations if o.at >= settle_from and o.vehicle_count is not None
        ]
        residual = after[0].vehicle_count if after else None
        banded_after = [
            o for o in observations if o.at >= settle_from and o.fullness is not None
        ]
        residual_fullness = banded_after[0].fullness if banded_after else None

    departure_seen = left_at is not None or deck_min is not None
    berth_visible = config.route.terminal(sailing_row["origin"]).camera_sees_berth

    # Whether the compound had gone bare by the time the residual reading starts. Deliberately
    # not `fullness_at_departure`, which stops at the *scheduled* time: a sailing leaving 30
    # minutes late is routine here, and reading the inference off the timetable rather than off
    # the ship is the whole bug this guards. On 2026-08-15 the 05:35's line cleared at 05:28,
    # the tarmac stayed bare through the departure, and the two lanes that appeared at 05:48
    # were the 07:25's queue beginning to form — billed to the sailing that already took
    # everyone. `settle_from` follows the ship, so the same reading survives a late departure:
    # a compound bare at 05:35 that fills again by 06:05 and strands thirty vehicles ends on a
    # heavy band, not an empty one, and is still an overload.
    #
    # Bounded at `settle_from` rather than at the departure itself because the compound empties
    # *while* the vessel loads and the last car can board on the minute it goes: on 2026-08-14
    # the board stamped "Departed 5:34 am" and the tarmac read bare at 05:34:25, twenty-five
    # seconds later. A bound on the departure would have called that sailing an overload over
    # one frame's cadence. This window ends exactly where `banded_after` begins, so every band
    # belongs to the run-up or to the residual and none to both.
    banded_before_settle = [
        o for o in observations if o.fullness is not None and grace_from <= o.at <= settle_from
    ]
    empty_when_it_left = (
        bool(banded_before_settle) and banded_before_settle[-1].fullness == "empty"
    )

    # Whether this terminal's "empty" band is bare tarmac or a licensed inference. The
    # reader owns that distinction through its calibration (`lanes_before_capacity`), so
    # ask the same file it read — keeping the rule in one place, where drift cannot put
    # the classifier and the reader on different sides of it.
    lane_cal = (
        load_lane_calibration(Path(config.source_path).parent, sailing_row["origin"])
        if config.source_path
        else None
    )
    clear_is_inference = (
        lane_cal is not None
        and not lane_cal.covers_compound
        and lane_cal.lanes_before_capacity
    )

    # Bands are preferred where they exist. A count is the older contract and the weaker
    # measurement — it survives only so that frames read under prompt v1 keep their meaning.
    if banded_before or banded_after:
        camera, _, cancelled, carryover = classify_from_bands(
            residual_fullness=residual_fullness,
            fullness_at_departure=fullness_at_departure,
            departure_seen=departure_seen,
            still_at_dock=still_at_dock,
            berth_visible=berth_visible,
            empty_when_it_left=empty_when_it_left,
            clear_requires_departure=clear_is_inference,
        )
        used = banded_before + banded_after
    else:
        camera, _, cancelled, carryover = classify(
            residual=residual,
            queue_at_departure=queue_at_departure,
            departure_seen=departure_seen,
            still_at_dock=still_at_dock,
            berth_visible=berth_visible,
            capacity=cfg.vessel_capacity,
            residual_threshold=cfg.residual_threshold,
        )
        used = counted_before + after

    # ---- Merge by claim, not by source ---------------------------------------------------
    #
    # Every source below writes only the axis it can actually witness, which is what stops
    # them competing to own one word. The old shape let whichever spoke last win outright,
    # and the last speaker was the report — so somebody saying "I got on" erased a fill the
    # board had watched happen and cleared the overload with it.
    filled: bool | None = None
    left_behind: bool | None = None
    waited: str | None = None  # the waited_1/waited_2plus refinement of left_behind

    # The camera. A residual queue after the vessel has gone is the one direct sight of
    # somebody left behind, and it implies the fill. A cleared compound proves the opposite
    # about the people and nothing at all about the deck: a vessel can load to the last
    # metre and still take everyone, so `filled` stays unknown rather than becoming False.
    if camera in ("waited_1", "waited_2plus"):
        filled, left_behind, waited = True, True, camera
    elif camera == "filled":
        filled, left_behind = True, True
    elif camera == "boarded":
        left_behind = False

    confidence = round(sum(o.confidence for o in used) / len(used), 3) if used else None
    sources = {o.source for o in used if o.source}
    method = f"frames:{'+'.join(sorted(sources))}" if sources else "frames:none"

    # The board, always — not only where the camera came up empty, as this used to be. It is
    # the only source that can say *when* a sailing filled, and `filled_at` is what the
    # "arrive before" advice is made of, so skipping the read whenever frames happened to
    # classify the crossing threw away the more useful half of the reading.
    deck, filled_at, deck_confidence = classify_from_deck_space(board_series, departure)
    if deck == "filled":
        filled = True
    elif deck == "boarded" and filled is None:
        # Saying "had space" needs either a reading near departure or a departed sailing
        # the board stayed noteless about — `classify_from_deck_space` holds both bars —
        # and it can only ever fill a gap, never overturn a fill somebody else witnessed.
        filled = False
    elif deck == "cancelled":
        cancelled = True

    # A capacity notice counts whatever the readings did. It appears on the board's second
    # line once the vessel has gone, long after the last percentage, so a gap in the series
    # is no reason to discount it — affirmative evidence does not expire, and a percentage
    # taken before the rush is not evidence against it.
    if left_full:
        filled = True

    if camera == "unknown" and (deck != "unknown" or left_full):
        confidence = deck_confidence if deck_confidence is not None else 0.7
        method = "deck_space"

    # Somebody who was in that queue outranks both machines on the axis they can speak to:
    # they observed the one thing this whole pipeline is inferring.
    #
    # What a report must not settle is `filled_at`. A traveller who joined at 11:20 and did
    # not get on proves the cutoff was *earlier* than 11:20 — treating their arrival as the
    # cutoff would tell the next traveller they can turn up later than they really can,
    # which is the one direction this app is not allowed to be wrong in. Deck space keeps
    # that job; the bound the report does establish is shown on the page instead.
    reports = fetch_reports(
        conn,
        sailing_row["route"],
        sailing_row["origin"],
        sailing_row["service_date"],
        sailing_row["depart_hhmm"],
    )
    reported = outcome_from_reports(reports)
    if reported:
        # Whoever filed it was standing there, so the sailing ran.
        cancelled = False
        if reported == "boarded":
            # Boarding proves the reporter got on and nothing whatever about the people
            # behind them. So it may lift `unknown`, and it may resolve a fill into "took
            # everyone" — the two facts are true at once — but it never un-fills a sailing.
            if left_behind is None:
                left_behind = False
        else:
            filled, left_behind = True, True
            waited = reported if reported in ("waited_1", "waited_2plus") else None
        confidence = report_confidence(reports)
        method = "report"

    # A cancelled sailing has no deck to fill and no line to turn away. Leaving stale axes
    # on it would put it in both the cancelled column and the filled one.
    if cancelled:
        filled = left_behind = None
        waited = None

    outcome = outcome_from_axes(
        filled=filled, left_behind=left_behind, cancelled=cancelled, waited=waited
    )

    return SailingRecord(
        sailing_id=sailing_row["id"],
        peak_queue=peak,
        queue_at_departure=queue_at_departure,
        residual_queue=residual,
        carryover=carryover,
        # An overload is people left on the tarmac, which is the `left_behind` axis and only
        # ever that. A vessel loading to capacity is not one.
        overload=bool(left_behind),
        cancelled=cancelled,
        outcome=outcome,
        n_frames=len(observations),
        confidence=confidence,
        queue_truncated=any(o.beyond_frame for o in counted_before),
        deck_space_min=deck_min,
        method=method,
        filled_at=filled_at,
        peak_fullness=peak_fullness,
        fullness_at_departure=fullness_at_departure,
        residual_fullness=residual_fullness,
        left_full=left_full,
        filled=filled,
        left_behind=left_behind,
        queue_started_at=_first_occupied_at(before),
        cleared_at=_cleared_at(observations, settle_from),
        departed_at=iso(departed_at) if departed_at else None,
        departed_source=departed_source,
    )


def store_record(conn: sqlite3.Connection, record: SailingRecord) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sailing_records
               (sailing_id, peak_queue, queue_at_departure, residual_queue, carryover,
                overload, cancelled, outcome, n_frames, confidence, queue_truncated,
                deck_space_min, filled_at, method, peak_fullness, fullness_at_departure,
                residual_fullness, queue_started_at, cleared_at, left_full,
                filled, left_behind, departed_at, departed_source, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record.sailing_id,
            record.peak_queue,
            record.queue_at_departure,
            record.residual_queue,
            record.carryover,
            int(record.overload),
            int(record.cancelled),
            record.outcome,
            record.n_frames,
            record.confidence,
            int(record.queue_truncated),
            record.deck_space_min,
            record.filled_at,
            record.method,
            record.peak_fullness,
            record.fullness_at_departure,
            record.residual_fullness,
            record.queue_started_at,
            record.cleared_at,
            None if record.left_full is None else int(record.left_full),
            None if record.filled is None else int(record.filled),
            None if record.left_behind is None else int(record.left_behind),
            record.departed_at,
            record.departed_source,
            iso(now_utc()),
        ),
    )
    conn.commit()


def aggregate_day(conn: sqlite3.Connection, config: Config, day: date) -> dict[str, int]:
    """Materialise the schedule for `day` and compute a record for every sailing on it."""
    route = config.route
    blocks = load_schedule_cached(config.schedule_path)
    sailings = sailings_for_day(blocks, day, route.id, route.destinations, config.tz)
    upsert_sailings(conn, sailings)

    # Scoped to the active route, so a database that later holds several routes still
    # aggregates each one against only its own sailings.
    rows = list(
        conn.execute(
            """SELECT * FROM sailings WHERE route = ? AND service_date = ?
                ORDER BY origin, scheduled_departure""",
            (route.id, day.isoformat()),
        ).fetchall()
    )

    by_origin: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_origin.setdefault(row["origin"], []).append(row)

    counts = dict.fromkeys(OUTCOMES, 0)
    for origin_rows in by_origin.values():
        for index, row in enumerate(origin_rows):
            previous = (
                datetime.fromisoformat(origin_rows[index - 1]["scheduled_departure"])
                if index > 0
                else None
            )
            following = (
                datetime.fromisoformat(origin_rows[index + 1]["scheduled_departure"])
                if index + 1 < len(origin_rows)
                else None
            )
            record = compute_record(
                conn, config, row, previous_departure=previous, next_departure=following
            )
            store_record(conn, record)
            counts[record.outcome] += 1
    return counts


def aggregate_range(
    conn: sqlite3.Connection, config: Config, start: date, end: date
) -> dict[str, int]:
    totals = dict.fromkeys(OUTCOMES, 0)
    with JobRun(conn, "aggregate") as run:
        day = start
        while day <= end:
            run.attempted += 1
            counts = aggregate_day(conn, config, day)
            for key, value in counts.items():
                totals[key] += value
            run.succeeded += 1
            day += timedelta(days=1)
    return totals


def observed_date_range(conn: sqlite3.Connection, config: Config) -> tuple[date, date] | None:
    """The span of days with any evidence at all.

    Deck space counts as much as frames here: with extraction on demand, deck space may be
    the only thing collected, and `aggregate --all` still has real work to do.
    """
    bounds: list[tuple[date, date]] = []

    frames = conn.execute(
        "SELECT MIN(captured_at), MAX(captured_at) FROM frames WHERE status = 'ok'"
    ).fetchone()
    if frames and frames[0]:
        bounds.append(
            (
                local(parse_iso(frames[0]), config.tz).date(),
                local(parse_iso(frames[1]), config.tz).date(),
            )
        )

    deck = conn.execute(
        "SELECT MIN(service_date), MAX(service_date) FROM deck_space WHERE fetch_status = 'ok'"
    ).fetchone()
    if deck and deck[0]:
        bounds.append((date.fromisoformat(deck[0]), date.fromisoformat(deck[1])))

    if not bounds:
        return None
    return (min(b[0] for b in bounds), max(b[1] for b in bounds))
