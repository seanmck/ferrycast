"""R4 — sailing-level aggregation.

Per-frame observations roll up into one record per scheduled sailing. The core inference,
straight from the PRD: given frames before and after a scheduled departure, if the queue
does not drop near zero afterwards, the sailing was overloaded and the remaining vehicles
are the carryover.

The outcome vocabulary matches what the query UI reports:

    boarded      — the queue cleared; a traveller in it made this sailing
    waited_1     — overloaded, but the carryover fits the next sailing
    waited_2plus — carryover exceeds one vessel's capacity
    cancelled    — a queue persisted and no vessel ever appeared
    unknown      — not enough usable frames to say (night, fog, capture gap)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Config
from .db import JobRun
from .reports import fetch_reports, outcome_from_reports, report_confidence
from .schedule import Sailing, load_schedule_cached, sailings_for_day
from .timeutil import iso, local, now_utc, parse_iso

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


@dataclass
class _Obs:
    at: datetime  # UTC
    vehicle_count: int | None
    ferry_at_dock: bool
    beyond_frame: bool
    confidence: float


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
    sql = """
        SELECT f.captured_at, o.vehicle_count, o.ferry_at_dock, o.queue_beyond_frame,
               o.confidence, o.usable
          FROM observations o
          JOIN frames f ON f.id = o.frame_id
         WHERE o.prompt_version = ?
           AND f.terminal = ?
           AND f.captured_at >= ?
           AND f.captured_at <= ?
    """
    params = [config.vision.prompt_version, terminal, iso(start), iso(end)]
    if usable_only:
        sql += " AND o.usable = 1"
    sql += " ORDER BY f.captured_at"
    return [
        _Obs(
            at=parse_iso(row["captured_at"]),
            vehicle_count=row["vehicle_count"],
            ferry_at_dock=bool(row["ferry_at_dock"]),
            beyond_frame=bool(row["queue_beyond_frame"]),
            confidence=float(row["confidence"] or 0.0),
        )
        for row in conn.execute(sql, tuple(params)).fetchall()
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
            """SELECT observed_at, percent_available, status_text FROM deck_space
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
        return "unknown", None, None

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

    peak = max((o.vehicle_count for o in counted_before), default=None)

    at_departure_candidates = [o for o in counted_before if o.at >= grace_from]
    queue_at_departure = at_departure_candidates[-1].vehicle_count if at_departure_candidates else None

    residual = after[0].vehicle_count if after else None

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
    if board_departure is not None:
        left_at = board_departure
        still_at_dock = False
        settle_from = max(departure + timedelta(minutes=cfg.settle_minutes), board_departure)
        after = [
            o for o in observations if o.at >= settle_from and o.vehicle_count is not None
        ]
        residual = after[0].vehicle_count if after else None

    departure_seen = left_at is not None or deck_min is not None

    outcome, overload, cancelled, carryover = classify(
        residual=residual,
        queue_at_departure=queue_at_departure,
        departure_seen=departure_seen,
        still_at_dock=still_at_dock,
        berth_visible=config.route.terminal(sailing_row["origin"]).camera_sees_berth,
        capacity=cfg.vessel_capacity,
        residual_threshold=cfg.residual_threshold,
    )

    used = counted_before + after
    confidence = round(sum(o.confidence for o in used) / len(used), 3) if used else None
    method = f"frames:{config.vision.prompt_version}"
    filled_at = None

    # Frames, when we have them, measure the thing that actually matters — vehicles waiting
    # outside the terminal. Falling back to deck space keeps the historical record alive
    # for every other sailing at no cost, at the price of a coarser answer.
    if outcome == "unknown":
        series = _deck_space_series(
            conn,
            sailing_row["route"],
            sailing_row["origin"],
            sailing_row["service_date"],
            sailing_row["depart_hhmm"],
        )
        derived, filled_at, derived_confidence = classify_from_deck_space(series, departure)
        if derived != "unknown":
            outcome = derived
            cancelled = derived == "cancelled"
            overload = derived == "filled"
            confidence = derived_confidence
            method = "deck_space"

    # Somebody who was in that queue outranks both machines: they observed the one thing
    # this whole pipeline is inferring. So a report settles the outcome whatever else spoke.
    #
    # What it must not settle is `filled_at`. A traveller who joined at 11:20 and did not
    # get on proves the cutoff was *earlier* than 11:20 — treating their arrival as the
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
        outcome = reported
        overload = reported == "filled"
        cancelled = False
        confidence = report_confidence(reports)
        method = "report"
        if outcome == "filled" and filled_at is None:
            _, filled_at, _ = classify_from_deck_space(
                _deck_space_series(
                    conn,
                    sailing_row["route"],
                    sailing_row["origin"],
                    sailing_row["service_date"],
                    sailing_row["depart_hhmm"],
                ),
                departure,
            )

    return SailingRecord(
        sailing_id=sailing_row["id"],
        peak_queue=peak,
        queue_at_departure=queue_at_departure,
        residual_queue=residual,
        carryover=carryover,
        overload=overload,
        cancelled=cancelled,
        outcome=outcome,
        n_frames=len(observations),
        confidence=confidence,
        queue_truncated=any(o.beyond_frame for o in counted_before),
        deck_space_min=deck_min,
        method=method,
        filled_at=filled_at,
    )


def store_record(conn: sqlite3.Connection, record: SailingRecord) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sailing_records
               (sailing_id, peak_queue, queue_at_departure, residual_queue, carryover,
                overload, cancelled, outcome, n_frames, confidence, queue_truncated,
                deck_space_min, filled_at, method, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
