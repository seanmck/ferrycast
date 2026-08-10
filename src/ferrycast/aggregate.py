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
from .schedule import Sailing, load_schedule_cached, sailings_for_day
from .timeutil import iso, local, now_utc, parse_iso

OUTCOMES = ("boarded", "waited_1", "waited_2plus", "cancelled", "unknown")


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
               ON CONFLICT (origin, scheduled_departure) DO UPDATE SET
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
    conn: sqlite3.Connection, terminal: str, service_date: str, hhmm: str
) -> int | None:
    row = conn.execute(
        """SELECT MIN(percent_available) FROM deck_space
            WHERE terminal = ? AND service_date = ? AND sailing_hhmm = ?
              AND percent_available IS NOT NULL""",
        (terminal, service_date, hhmm),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def classify(
    *,
    residual: int | None,
    queue_at_departure: int | None,
    departure_seen: bool,
    capacity: int,
    residual_threshold: int,
) -> tuple[str, bool, bool, int | None]:
    """Return (outcome, overload, cancelled, carryover)."""
    if residual is None or queue_at_departure is None:
        return "unknown", False, False, None

    if residual < residual_threshold:
        # Queue cleared. Even with no vessel positively identified, an emptied compound
        # means the sailing ran.
        return "boarded", False, False, 0

    if not departure_seen:
        # A queue that persists with no vessel ever at the dock is a cancellation, not an
        # overload — the distinction matters to anyone reading the history.
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
    settle_from = departure + timedelta(minutes=cfg.settle_minutes)
    after = [
        o for o in observations if o.at >= settle_from and o.vehicle_count is not None
    ]

    peak = max((o.vehicle_count for o in counted_before), default=None)

    grace_from = departure - timedelta(minutes=cfg.departure_grace_minutes)
    at_departure_candidates = [o for o in counted_before if o.at >= grace_from]
    queue_at_departure = at_departure_candidates[-1].vehicle_count if at_departure_candidates else None

    residual = after[0].vehicle_count if after else None

    # A vessel counts as having departed if it was at the dock near the scheduled time and
    # is gone afterwards, or if the deck-space feed published a figure for this sailing.
    dock_before = any(o.ferry_at_dock for o in before if o.at >= grace_from)
    dock_after = any(o.ferry_at_dock for o in observations if o.at >= settle_from)
    deck_min = _deck_space_min(
        conn, sailing_row["origin"], sailing_row["service_date"], sailing_row["depart_hhmm"]
    )
    departure_seen = (dock_before and not dock_after) or deck_min is not None

    outcome, overload, cancelled, carryover = classify(
        residual=residual,
        queue_at_departure=queue_at_departure,
        departure_seen=departure_seen,
        capacity=cfg.vessel_capacity,
        residual_threshold=cfg.residual_threshold,
    )

    used = counted_before + after
    confidence = round(sum(o.confidence for o in used) / len(used), 3) if used else None

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
        method=f"frames:{config.vision.prompt_version}",
    )


def store_record(conn: sqlite3.Connection, record: SailingRecord) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sailing_records
               (sailing_id, peak_queue, queue_at_departure, residual_queue, carryover,
                overload, cancelled, outcome, n_frames, confidence, queue_truncated,
                deck_space_min, method, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            record.method,
            iso(now_utc()),
        ),
    )
    conn.commit()


def aggregate_day(conn: sqlite3.Connection, config: Config, day: date) -> dict[str, int]:
    """Materialise the schedule for `day` and compute a record for every sailing on it."""
    blocks = load_schedule_cached(config.schedule_path)
    destinations = {t.code: t.destination for t in config.route.terminals}
    sailings = sailings_for_day(blocks, day, config.route.id, destinations, config.tz)
    upsert_sailings(conn, sailings)

    rows = list(
        conn.execute(
            """SELECT * FROM sailings WHERE service_date = ?
                ORDER BY origin, scheduled_departure""",
            (day.isoformat(),),
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
    row = conn.execute(
        "SELECT MIN(captured_at), MAX(captured_at) FROM frames WHERE status = 'ok'"
    ).fetchone()
    if not row or not row[0]:
        return None
    return (
        local(parse_iso(row[0]), config.tz).date(),
        local(parse_iso(row[1]), config.tz).date(),
    )
