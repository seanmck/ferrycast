"""R5 — "day like today" retrieval.

Comparable sailings are the same sailing time x day-type x season bucket. The PRD's open
question is that those buckets may be too thin in the first season, so instead of returning
a distribution over three sailings and calling it an answer, the search widens in defined
steps and reports which step it had to use. The sample size and the underlying dates travel
with the answer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from .config import Config
from .schedule import Sailing, day_tags, day_type, load_schedule_cached, sailings_for_day, season
from .timeutil import combine_local, iso, local, now_utc, parse_hhmm, parse_iso

# Day types that stand in for one another when a bucket is too thin.
DAY_TYPE_GROUPS = {
    "weekday": ("weekday", "friday"),
    "friday": ("friday", "weekday"),
    "saturday": ("saturday", "sunday_holiday"),
    "sunday_holiday": ("sunday_holiday", "saturday"),
}

REPORTED_OUTCOMES = ("boarded", "filled", "waited_1", "waited_2plus", "cancelled")

OUTCOME_LABELS = {
    "boarded": "Space the whole time",
    "filled": "Filled up before departure",
    "waited_1": "Waited 1 sailing",
    "waited_2plus": "Waited 2+ sailings",
    "cancelled": "Cancelled",
    "unknown": "No usable record",
}

# Same vocabulary at a glance, for the counts under the distribution bar where the full
# labels would wrap. Read as a ramp: made it, only just, waited, waited more.
OUTCOME_LABELS_SHORT = {
    "boarded": "made it",
    "filled": "filled up",
    "waited_1": "waited 1",
    "waited_2plus": "waited 2+",
    "cancelled": "cancelled",
    "unknown": "no record",
}

# What the evidence behind an outcome can and cannot support, shown alongside the answer.
EVIDENCE_NOTES = {
    "deck_space": (
        "From published deck space: it shows when the vessel ran out of room, not how many "
        "vehicles were still queued outside the terminal."
    ),
    "frames": "From terminal camera frames, which count vehicles waiting outside the terminal.",
    "mixed": "Part camera frames, part published deck space.",
    "import": "Manually imported records.",
}


@dataclass
class ComparableSailing:
    service_date: str
    depart_hhmm: str
    day_type: str
    season: str
    outcome: str
    peak_queue: int | None
    queue_at_departure: int | None
    carryover: int | None
    confidence: float | None
    queue_truncated: bool
    filled_at_local: str | None = None
    fill_minutes_before: int | None = None
    evidence: str = "unknown"
    tags: list[str] = field(default_factory=list)


@dataclass
class Distribution:
    origin: str
    destination: str
    service_date: str
    depart_hhmm: str
    day_type: str
    season: str
    n: int
    counts: dict[str, int]
    shares: dict[str, float]
    unknown_excluded: int
    match_level: str
    relaxations: list[str]
    samples: list[ComparableSailing]
    tags: list[str]
    sufficient: bool
    evidence: str = "unknown"
    evidence_note: str = ""
    # Median minutes before departure at which comparable sailings ran out of room —
    # the "how late could I turn up" answer, free from the deck-space feed.
    typical_fill_minutes_before: int | None = None
    typical_fill_local: str | None = None
    filled_share: float = 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["labels"] = OUTCOME_LABELS
        return data


def _hhmm_to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class _Level:
    name: str
    tolerance: int
    day_types: tuple[str, ...]
    same_season: bool
    description: str


def _levels(config: Config, target_day_type: str) -> list[_Level]:
    tight = config.query.time_tolerance_minutes
    wide = config.query.relaxed_time_tolerance_minutes
    group = DAY_TYPE_GROUPS.get(target_day_type, (target_day_type,))
    return [
        _Level("exact", tight, (target_day_type,), True, "same sailing time, day type and season"),
        _Level("any_season", tight, (target_day_type,), False, "widened to all seasons"),
        _Level("wider_time", wide, (target_day_type,), False, f"widened to +/-{wide} min"),
        _Level(
            "grouped_days",
            wide,
            group,
            False,
            f"widened to similar day types ({', '.join(group)})",
        ),
    ]


def _fetch_candidates(
    conn: sqlite3.Connection,
    route: str,
    origin: str,
    day_types: tuple[str, ...],
    season_bucket: str | None,
    exclude_date: str | None,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in day_types)
    sql = f"""
        SELECT s.service_date, s.depart_hhmm, s.day_type, s.season, s.scheduled_departure,
               r.outcome, r.peak_queue, r.queue_at_departure, r.carryover,
               r.confidence, r.queue_truncated, r.filled_at, r.method
          FROM sailings s
          JOIN sailing_records r ON r.sailing_id = s.id
         WHERE s.route = ?
           AND s.origin = ?
           AND s.day_type IN ({placeholders})
           AND r.outcome != 'unknown'
    """
    params: list = [route, origin, *day_types]
    if season_bucket:
        sql += " AND s.season = ?"
        params.append(season_bucket)
    if exclude_date:
        sql += " AND s.service_date != ?"
        params.append(exclude_date)
    sql += " ORDER BY s.service_date DESC"
    return list(conn.execute(sql, tuple(params)).fetchall())


def _count_unknown(
    conn: sqlite3.Connection, route: str, origin: str, day_types: tuple[str, ...]
) -> int:
    placeholders = ",".join("?" for _ in day_types)
    row = conn.execute(
        f"""SELECT COUNT(*) FROM sailings s
              JOIN sailing_records r ON r.sailing_id = s.id
             WHERE s.route = ? AND s.origin = ? AND s.day_type IN ({placeholders})
               AND r.outcome = 'unknown'""",
        (route, origin, *day_types),
    ).fetchone()
    return int(row[0] or 0)


def _tags_for(conn: sqlite3.Connection, service_date: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM event_tags WHERE service_date = ? ORDER BY tag", (service_date,)
    ).fetchall()
    auto = day_tags(date.fromisoformat(service_date))
    return sorted({*auto, *(r["tag"] for r in rows)})


def query_distribution(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    max_samples: int = 40,
    route_id: str | None = None,
) -> Distribution:
    route = config.route_by_id(route_id) if route_id else config.route
    terminal = route.terminal(origin)
    target_type = day_type(target_date)
    target_season = season(target_date)
    target_minutes = _hhmm_to_minutes(depart_hhmm)

    matches: list[sqlite3.Row] = []
    used = _levels(config, target_type)[0]
    relaxations: list[str] = []

    for level in _levels(config, target_type):
        rows = _fetch_candidates(
            conn,
            route.id,
            origin,
            level.day_types,
            target_season if level.same_season else None,
            target_date.isoformat(),
        )
        matches = [
            row
            for row in rows
            if abs(_hhmm_to_minutes(row["depart_hhmm"]) - target_minutes) <= level.tolerance
        ]
        used = level
        # Record the widening *before* deciding to stop, so the level that finally
        # succeeded is reported rather than silently omitted.
        if level.name != "exact":
            relaxations.append(level.description)
        if len(matches) >= config.query.min_sample:
            break

    counts = dict.fromkeys(REPORTED_OUTCOMES, 0)
    for row in matches:
        if row["outcome"] in counts:
            counts[row["outcome"]] += 1
    n = sum(counts.values())
    shares = {k: (round(v / n, 4) if n else 0.0) for k, v in counts.items()}

    samples = [_sample(conn, config, row) for row in matches[:max_samples]]

    fill_gaps = [
        gap
        for gap in (_fill_gap_minutes(row) for row in matches)
        if gap is not None
    ]
    typical_gap = _median_int(fill_gaps)
    typical_local = None
    if typical_gap is not None:
        moment = combine_local(target_date, parse_hhmm(depart_hhmm), config.tz) - timedelta(
            minutes=typical_gap
        )
        typical_local = moment.strftime("%H:%M")

    evidence = _evidence_of({row["method"] for row in matches})

    return Distribution(
        origin=origin,
        destination=terminal.destination,
        service_date=target_date.isoformat(),
        depart_hhmm=depart_hhmm,
        day_type=target_type,
        season=target_season,
        n=n,
        counts=counts,
        shares=shares,
        unknown_excluded=_count_unknown(conn, route.id, origin, used.day_types),
        match_level=used.name,
        relaxations=relaxations if used.name != "exact" else [],
        samples=samples,
        tags=_tags_for(conn, target_date.isoformat()),
        sufficient=n >= config.query.min_sample,
        evidence=evidence,
        evidence_note=EVIDENCE_NOTES.get(evidence, ""),
        typical_fill_minutes_before=typical_gap,
        typical_fill_local=typical_local,
        filled_share=round(
            sum(counts[o] for o in ("filled", "waited_1", "waited_2plus")) / n, 4
        )
        if n
        else 0.0,
    )


def _evidence_of(methods: set[str | None]) -> str:
    kinds = set()
    for method in methods:
        if not method:
            continue
        if method.startswith("frames"):
            kinds.add("frames")
        elif method.startswith("import"):
            kinds.add("import")
        elif method.startswith("deck_space"):
            kinds.add("deck_space")
    if not kinds:
        return "unknown"
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed"


def _fill_gap_minutes(row: sqlite3.Row) -> int | None:
    """Minutes between a sailing filling up and its scheduled departure."""
    if not row["filled_at"]:
        return None
    departure = datetime.fromisoformat(row["scheduled_departure"])
    gap = (departure - parse_iso(row["filled_at"])).total_seconds() / 60
    return int(gap) if gap >= 0 else 0


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _sample(conn: sqlite3.Connection, config: Config, row: sqlite3.Row) -> ComparableSailing:
    gap = _fill_gap_minutes(row)
    filled_local = None
    if row["filled_at"]:
        filled_local = local(parse_iso(row["filled_at"]), config.tz).strftime("%H:%M")
    return ComparableSailing(
        service_date=row["service_date"],
        depart_hhmm=row["depart_hhmm"],
        day_type=row["day_type"],
        season=row["season"],
        outcome=row["outcome"],
        peak_queue=row["peak_queue"],
        queue_at_departure=row["queue_at_departure"],
        carryover=row["carryover"],
        confidence=row["confidence"],
        queue_truncated=bool(row["queue_truncated"]),
        filled_at_local=filled_local,
        fill_minutes_before=gap,
        evidence=_evidence_of({row["method"]}),
        tags=_tags_for(conn, row["service_date"]),
    )


def upcoming_sailings(
    config: Config,
    *,
    origin: str | None = None,
    at: datetime | None = None,
    limit: int = 12,
) -> list[Sailing]:
    """Scheduled sailings from now forward — used to default the UI to the next departure."""
    blocks = load_schedule_cached(config.schedule_path)
    route = config.route
    reference = local(at or now_utc(), config.tz)
    found: list[Sailing] = []
    day = reference.date()
    for _ in range(8):  # look ahead a week for a schedule block that covers a day
        for sailing in sailings_for_day(
            blocks, day, route.id, route.destinations, config.tz, origin=origin
        ):
            if sailing.scheduled_departure >= reference:
                found.append(sailing)
        if len(found) >= limit:
            break
        day += timedelta(days=1)
    return found[:limit]


def sailing_times(config: Config, origin: str, target_date: date) -> list[str]:
    blocks = load_schedule_cached(config.schedule_path)
    route = config.route
    return [
        s.depart_hhmm
        for s in sailings_for_day(
            blocks, target_date, route.id, route.destinations, config.tz, origin=origin
        )
    ]


def default_sailing_time(
    config: Config,
    times: list[str],
    target_date: date,
    *,
    at: datetime | None = None,
) -> str | None:
    """Which sailing to answer about when the caller did not name a usable one.

    The next one that has not departed. Taking the first of the day instead answers about
    a sailing that left hours ago — switch terminals at half past nine and the form would
    quietly offer you the 06:30, with no countdown and history for a boat you cannot
    catch. Only today has a "now" to measure against: on any other date the first sailing
    is the neutral place to start, and once today's are all gone the last one is the
    nearest there is.

    `times` must be in departure order, as `sailing_times` returns them.
    """
    if not times:
        return None
    reference = local(at or now_utc(), config.tz)
    if target_date != reference.date():
        return times[0]
    return next((t for t in times if t >= reference.strftime("%H:%M")), times[-1])


def arrival_curve(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    bucket_minutes: int = 15,
) -> dict:
    """P1 — queue length versus minutes before departure, over comparable sailings.

    Answers "when do I need to arrive" by showing how the queue built on days like this one.
    """
    target_type = day_type(target_date)
    target_season = season(target_date)
    target_minutes = _hhmm_to_minutes(depart_hhmm)
    tolerance = config.query.time_tolerance_minutes
    group = DAY_TYPE_GROUPS.get(target_type, (target_type,))
    placeholders = ",".join("?" for _ in group)

    rows = conn.execute(
        f"""SELECT s.id, s.scheduled_departure, s.depart_hhmm, s.season, s.service_date
              FROM sailings s
              JOIN sailing_records r ON r.sailing_id = s.id
             WHERE s.route = ? AND s.origin = ? AND s.day_type IN ({placeholders})
               AND r.outcome != 'unknown'""",
        (config.route.id, origin, *group),
    ).fetchall()

    relevant = [
        row
        for row in rows
        if abs(_hhmm_to_minutes(row["depart_hhmm"]) - target_minutes) <= tolerance
    ]
    # Prefer same-season sailings, but fall back rather than return an empty chart.
    same_season = [row for row in relevant if row["season"] == target_season]
    if len(same_season) >= config.query.min_sample:
        relevant = same_season

    buckets: dict[int, list[int]] = {}
    deck_buckets: dict[int, list[int]] = {}
    for row in relevant:
        departure = datetime.fromisoformat(row["scheduled_departure"])
        window_start = departure - timedelta(minutes=config.aggregate.lookback_minutes)

        # Deck space costs nothing and is collected continuously, so it carries the curve
        # whenever frames were not extracted for these sailings.
        for deck in conn.execute(
            """SELECT observed_at, percent_available FROM deck_space
                WHERE route = ? AND terminal = ? AND service_date = ? AND sailing_hhmm = ?
                  AND percent_available IS NOT NULL AND fetch_status = 'ok'""",
            (config.route.id, origin, row["service_date"], row["depart_hhmm"]),
        ).fetchall():
            observed = parse_iso(deck["observed_at"])
            minutes_before = int((departure - observed).total_seconds() // 60)
            if 0 <= minutes_before <= config.aggregate.lookback_minutes:
                bucket = (minutes_before // bucket_minutes) * bucket_minutes
                deck_buckets.setdefault(bucket, []).append(deck["percent_available"])

        observations = conn.execute(
            """SELECT f.captured_at, o.vehicle_count
                 FROM observations o
                 JOIN frames f ON f.id = o.frame_id
                WHERE o.prompt_version = ? AND o.usable = 1
                  AND o.vehicle_count IS NOT NULL
                  AND f.terminal = ?
                  AND f.captured_at >= ? AND f.captured_at <= ?""",
            (
                config.vision.prompt_version,
                origin,
                iso(window_start),
                iso(departure),
            ),
        ).fetchall()
        for obs in observations:
            minutes_before = int(
                (departure - parse_iso(obs["captured_at"])).total_seconds() // 60
            )
            if minutes_before < 0:
                continue
            bucket = (minutes_before // bucket_minutes) * bucket_minutes
            buckets.setdefault(bucket, []).append(obs["vehicle_count"])

    # Vehicle counts are the better signal when they exist; deck space is the free fallback.
    if buckets:
        source, unit, series, descending = "frames", "vehicles waiting", buckets, False
    else:
        source, unit, series, descending = (
            "deck_space",
            "% deck space left",
            deck_buckets,
            True,
        )

    points = []
    for bucket in sorted(series, reverse=True):
        values = sorted(series[bucket])
        points.append(
            {
                "minutes_before": bucket,
                "n": len(values),
                "median": _percentile(values, 0.5),
                # p25/p75 are named for the optimistic/pessimistic edge of the band, not the
                # raw percentile: deck space drains downward, so its quartiles are mirrored.
                "p25": _percentile(values, 0.75 if descending else 0.25),
                "p75": _percentile(values, 0.25 if descending else 0.75),
                "max": values[-1],
            }
        )

    return {
        "origin": origin,
        "depart_hhmm": depart_hhmm,
        "day_type": target_type,
        "season": target_season,
        "sailings": len(relevant),
        "source": source,
        "unit": unit,
        # Deck space drains toward zero, so the pessimistic case is the low quartile.
        "pessimistic_is_low": descending,
        "points": points,
    }


def _percentile(sorted_values: list[int], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1))))
    return float(sorted_values[index])
