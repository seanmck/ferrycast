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

from .aggregate import axes_from_outcome
from .config import Config
from .holidays import is_long_weekend
from .lanes import PROMPT_VERSION as LANE_PROMPT_VERSION
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

# One person turned away is an observed failure and stands on its own. One person getting
# on is one person's luck, so the "everyone who got on" bound waits for a second report.
MIN_BOARDED_REPORTS = 2

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
    "report": (
        "Reported by people who were in the line: the only evidence that says outright "
        "whether someone got on."
    ),
    "mixed": "More than one kind of evidence — camera frames, deck space or first-hand reports.",
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
    # How full it got, for sailings read under prompt v2 onward. `peak_queue` is the same
    # question asked in a unit the cameras cannot actually support; prefer this when set.
    peak_fullness: str | None = None
    queue_started_local: str | None = None
    cleared_local: str | None = None
    #: The board reported this sailing loading to capacity. None means it never said —
    #: which is not the same as "no", and must not be shown as one.
    left_full: bool | None = None
    #: The two axes, per sailing, so a listed date carries the same facts the summary is
    #: built from rather than a second account of them.
    filled: bool | None = None
    left_behind: bool | None = None
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
    #: The share that ran out of room, whether or not anyone was left behind by it.
    filled_share: float = 0.0
    # The sailing axis: what the board can attest about every sailing, and the three
    # segments of the bar. They sum to `n`.
    had_space: int = 0
    filled: int = 0
    cancelled: int = 0
    # The person axis, which refines `filled` and nothing else. These three sum to `filled`.
    # `unresolved` is the honest third state — the vessel ran out of room and nobody has
    # said whether that cost anyone their crossing.
    left_behind: int = 0
    took_everyone: int = 0
    unresolved: int = 0
    # How far each source reached into the pool, so the page can say what it rests on
    # instead of implying every sailing was watched the same way.
    board_reported: int = 0
    sharpened: int = 0

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
    # A long-weekend Monday behaves nothing like an ordinary one, and the difference is not
    # captured by day type alone: a stat Monday already buckets as sunday_holiday, but the
    # Friday and Sunday *around* it look like ordinary Fridays and Sundays to the schedule.
    # This is the last dimension to be relaxed, because it is the strongest one.
    same_long_weekend: bool = True


def _levels(config: Config, target_day_type: str) -> list[_Level]:
    tight = config.query.time_tolerance_minutes
    wide = config.query.relaxed_time_tolerance_minutes
    group = DAY_TYPE_GROUPS.get(target_day_type, (target_day_type,))
    return [
        _Level("exact", tight, (target_day_type,), True, "same sailing time, day type and season"),
        _Level("any_season", tight, (target_day_type,), False, "widened to all seasons"),
        _Level("wider_time", wide, (target_day_type,), False, f"widened to +/-{wide} min"),
        # Long-weekend context is relaxed before day type, not after: for a long-weekend
        # Friday, ordinary Fridays are a closer comparison than long-weekend Saturdays.
        # Day type is the more fundamental dimension; this one is a strong modifier on it.
        _Level(
            "any_long_weekend",
            wide,
            (target_day_type,),
            False,
            "widened to include days outside a long weekend, which behave differently",
            same_long_weekend=False,
        ),
        _Level(
            "grouped_days",
            wide,
            group,
            False,
            f"widened to similar day types ({', '.join(group)})",
            same_long_weekend=False,
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
               r.outcome, r.filled, r.left_behind,
               r.peak_queue, r.queue_at_departure, r.carryover,
               r.confidence, r.queue_truncated, r.filled_at, r.method,
               r.peak_fullness, r.queue_started_at, r.cleared_at, r.left_full
          FROM sailings s
          JOIN sailing_records r ON r.sailing_id = s.id
         WHERE s.route = ?
           AND s.origin = ?
           AND s.day_type IN ({placeholders})
           AND r.outcome != 'unknown'
    """
    # One pool, one sufficiency rule. `outcome` is now derived from the two axes, so
    # "not unknown" is exactly "at least one axis has an answer" — which is how the board's
    # readings widen this denominator instead of being tallied separately beside it.
    params: list = [route, origin, *day_types]
    if season_bucket:
        sql += " AND s.season = ?"
        params.append(season_bucket)
    if exclude_date:
        sql += " AND s.service_date != ?"
        params.append(exclude_date)
    sql += " ORDER BY s.service_date DESC"
    return list(conn.execute(sql, tuple(params)).fetchall())


def _axes(row: sqlite3.Row) -> tuple[bool | None, bool | None]:
    """(filled, left_behind) for one comparable sailing.

    Falls back to the outcome word where the columns were never written — an imported CSV,
    or a database aggregated before the axes existed. Such a record still answers, at the
    coarser resolution the single overloaded field could carry.
    """
    if row["filled"] is None and row["left_behind"] is None:
        return axes_from_outcome(row["outcome"])
    return (
        None if row["filled"] is None else bool(row["filled"]),
        None if row["left_behind"] is None else bool(row["left_behind"]),
    )


def _axis_counts(matches: list[sqlite3.Row]) -> dict[str, int]:
    """Tally both axes over one pool.

    This replaces a separate capacity tally computed over its own set of sailings with its
    own sufficiency rule. Two pools meant two denominators for one question — "0 of 3 left
    at capacity" printed above "1 comparable sailing, 0% ran out of room" — and no reader
    could reconcile them because they were never counting the same sailings.
    """
    tally = dict.fromkeys(
        (
            "had_space",
            "filled",
            "cancelled",
            "left_behind",
            "took_everyone",
            "unresolved",
            "board_reported",
            "sharpened",
        ),
        0,
    )
    for row in matches:
        if row["left_full"] is not None:
            tally["board_reported"] += 1
            if _evidence_of({row["method"]}) in ("frames", "report"):
                tally["sharpened"] += 1

        if row["outcome"] == "cancelled":
            tally["cancelled"] += 1
            continue

        filled, left_behind = _axes(row)
        if filled:
            tally["filled"] += 1
            if left_behind is True:
                tally["left_behind"] += 1
            elif left_behind is False:
                tally["took_everyone"] += 1
            else:
                tally["unresolved"] += 1
        else:
            # Everything that did not run out of room, including a compound the camera
            # watched empty without the board ever saying how close the deck came. The
            # segment is labelled for the traveller — there was space when it mattered.
            tally["had_space"] += 1
    return tally


def _candidate_reader(conn: sqlite3.Connection, route: str, origin: str, exclude_date: str | None):
    """A `_fetch_candidates` that answers each distinct bucket once.

    The ladder asks for the same (day types, season) pair at several of its rungs — three
    of the five differ only in time tolerance — and the day board then walks that ladder
    once per sailing. Memoising turns what would be five queries per sailing into three
    for a whole day's timetable. The query is deterministic, so a cached answer is the
    same answer.
    """
    cache: dict[tuple[tuple[str, ...], str | None], list[sqlite3.Row]] = {}

    def read(day_types: tuple[str, ...], season_bucket: str | None) -> list[sqlite3.Row]:
        key = (day_types, season_bucket)
        if key not in cache:
            cache[key] = _fetch_candidates(
                conn, route, origin, day_types, season_bucket, exclude_date
            )
        return cache[key]

    return read


def _walk_levels(
    read,
    levels: list[_Level],
    *,
    target_season: str,
    target_minutes: int,
    target_long_weekend: bool,
    min_sample: int,
    cap: int,
) -> tuple[list[sqlite3.Row], _Level, list[str]]:
    """Widen the search a rung at a time until the bucket is deep enough.

    Shared by the single-sailing answer and by every row of the day board, so a row can
    never disagree with the panel it opens — the board saying 46% beside a headline of 54%
    would be read as two different facts about the same sailing rather than one bug.
    """
    matches: list[sqlite3.Row] = []
    used = levels[0]
    relaxations: list[str] = []

    for level in levels:
        rows = read(level.day_types, target_season if level.same_season else None)
        matches = [
            row
            for row in rows
            if abs(_hhmm_to_minutes(row["depart_hhmm"]) - target_minutes) <= level.tolerance
            and (
                not level.same_long_weekend
                or is_long_weekend(date.fromisoformat(row["service_date"])) == target_long_weekend
            )
        ]
        # Newest first from the query, so truncating here keeps the most recent sailings.
        # The cap applies to the counts as well as the listed dates: a distribution that
        # says "5 sailings" must be computed from those five and no others.
        matches = matches[:cap]
        used = level
        # Record the widening *before* deciding to stop, so the level that finally
        # succeeded is reported rather than silently omitted.
        if level.name != "exact":
            relaxations.append(level.description)
        if len(matches) >= min_sample:
            break

    return matches, used, relaxations


def _typical_fill(
    config: Config, matches: list[sqlite3.Row], target_date: date, depart_hhmm: str
) -> tuple[int | None, str | None]:
    """Median minutes before departure at which these sailings ran out of room, as a clock."""
    gaps = [gap for gap in (_fill_gap_minutes(row) for row in matches) if gap is not None]
    typical = _median_int(gaps)
    if typical is None:
        return None, None
    moment = combine_local(target_date, parse_hhmm(depart_hhmm), config.tz) - timedelta(
        minutes=typical
    )
    return typical, moment.strftime("%H:%M")


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
    max_samples: int | None = None,
    route_id: str | None = None,
) -> Distribution:
    route = config.route_by_id(route_id) if route_id else config.route
    terminal = route.terminal(origin)
    target_type = day_type(target_date)
    target_season = season(target_date)
    target_minutes = _hhmm_to_minutes(depart_hhmm)
    target_long_weekend = is_long_weekend(target_date)
    cap = max_samples if max_samples is not None else config.query.max_samples

    matches, used, relaxations = _walk_levels(
        _candidate_reader(conn, route.id, origin, target_date.isoformat()),
        _levels(config, target_type),
        target_season=target_season,
        target_minutes=target_minutes,
        target_long_weekend=target_long_weekend,
        min_sample=config.query.min_sample,
        cap=cap,
    )

    counts = _counts_of(matches)
    n = sum(counts.values())
    shares = {k: (round(v / n, 4) if n else 0.0) for k, v in counts.items()}

    samples = [_sample(conn, config, row) for row in matches]

    typical_gap, typical_local = _typical_fill(config, matches, target_date, depart_hhmm)

    evidence = _evidence_of({row["method"] for row in matches})
    axes = _axis_counts(matches)

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
        filled_share=round(axes["filled"] / n, 4) if n else 0.0,
        **axes,
    )


def _counts_of(matches: list[sqlite3.Row]) -> dict[str, int]:
    counts = dict.fromkeys(REPORTED_OUTCOMES, 0)
    for row in matches:
        if row["outcome"] in counts:
            counts[row["outcome"]] += 1
    return counts


def _evidence_of(methods: set[str | None]) -> str:
    kinds = set()
    for method in methods:
        if not method:
            continue
        if method.startswith("frames"):
            kinds.add("frames")
        elif method.startswith("report"):
            kinds.add("report")
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


def _local_hhmm(stamp: str | None, config: Config) -> str | None:
    """A stored UTC transition as a local clock time, which is how a traveller reads it."""
    if not stamp:
        return None
    return local(parse_iso(stamp), config.tz).strftime("%H:%M")


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
    filled, left_behind = _axes(row)
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
        peak_fullness=row["peak_fullness"],
        queue_started_local=_local_hhmm(row["queue_started_at"], config),
        cleared_local=_local_hhmm(row["cleared_at"], config),
        left_full=None if row["left_full"] is None else bool(row["left_full"]),
        filled=filled,
        left_behind=left_behind,
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


@dataclass
class BoardSailing:
    """One row of the day board: a whole timetable answered at a glance.

    Deliberately thinner than `Distribution`. The board carries ten of these, and the two
    expensive parts of the full answer — the per-sample tag lookup and the unknown count —
    are worth a query each for the sailing somebody actually opened and worth none for the
    nine they did not.
    """

    depart_hhmm: str
    n: int
    counts: dict[str, int]
    shares: dict[str, float]
    # The board's own two figures: the share that got on, and the share that did not.
    boarded_share: float
    filled_share: float
    sufficient: bool
    match_level: str
    relaxed: bool
    arrive_by: str | None
    fill_minutes_before: int | None


def day_board(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    route_id: str | None = None,
) -> list[BoardSailing]:
    """Every sailing on one date, each answered from its own comparable history.

    The desktop board's premise is that a whole day fits on one screen, so the question
    stops being "what about the 15:25" and becomes "which boat should I take" — which the
    single-sailing page could only answer by being loaded ten times.

    Ten answers cost three queries, not thirty: the ladder's buckets are shared across
    sailings and read once each, and every rung after the fetch is filtering in memory.
    Rows walk the same ladder as `query_distribution`, so opening one cannot show a
    different number from the row that was clicked.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    target_type = day_type(target_date)
    target_season = season(target_date)
    target_long_weekend = is_long_weekend(target_date)
    levels = _levels(config, target_type)
    cap = config.query.max_samples
    read = _candidate_reader(conn, route.id, origin, target_date.isoformat())

    board = []
    for depart_hhmm in sailing_times(config, origin, target_date):
        # The descriptions of each widening are the panel's to print, not the board's — a
        # row has one column of width for its sample size and no room to justify it.
        matches, used, _ = _walk_levels(
            read,
            levels,
            target_season=target_season,
            target_minutes=_hhmm_to_minutes(depart_hhmm),
            target_long_weekend=target_long_weekend,
            min_sample=config.query.min_sample,
            cap=cap,
        )
        counts = _counts_of(matches)
        n = sum(counts.values())
        # Through the same tally as the panel, not a second sum over the outcome words:
        # a row that says 46% beside a headline of 54% reads as two findings.
        axes = _axis_counts(matches)
        gap, arrive_by = _typical_fill(config, matches, target_date, depart_hhmm)
        board.append(
            BoardSailing(
                depart_hhmm=depart_hhmm,
                n=n,
                counts=counts,
                shares={k: (round(v / n, 4) if n else 0.0) for k, v in counts.items()},
                boarded_share=round(counts["boarded"] / n, 4) if n else 0.0,
                filled_share=round(axes["filled"] / n, 4) if n else 0.0,
                sufficient=n >= config.query.min_sample,
                match_level=used.name,
                # Only a count can be qualified. A sailing with no history anywhere walks
                # the whole ladder and ends on its last rung, but nothing was widened to
                # reach that — there was nothing to reach — so marking its em dash with a
                # "we had to search wider" asterisk points at an absence.
                relaxed=bool(n) and used.name != "exact",
                arrive_by=arrive_by,
                fill_minutes_before=gap,
            )
        )
    return board


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
    lane_buckets: dict[int, list[int]] = {}
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

        # Lanes in use, read from camera geometry. This is the curve's best source and the
        # only one that works on this route: counts were dropped as a unit (an RV is not a
        # hatchback, and the readings were unstable besides), and the deck-space percentage
        # this function was written to fall back on has never once been published here — 2368
        # rows, zero percentages. Lanes are ordinal, comparable across days, and free.
        for obs in conn.execute(
            """SELECT f.captured_at, o.lanes_occupied
                 FROM observations o
                 JOIN frames f ON f.id = o.frame_id
                WHERE o.prompt_version = ? AND o.usable = 1
                  AND o.lanes_occupied IS NOT NULL
                  AND f.terminal = ?
                  AND f.captured_at >= ? AND f.captured_at <= ?""",
            (LANE_PROMPT_VERSION, origin, iso(window_start), iso(departure)),
        ).fetchall():
            minutes_before = int(
                (departure - parse_iso(obs["captured_at"])).total_seconds() // 60
            )
            if minutes_before < 0:
                continue
            bucket = (minutes_before // bucket_minutes) * bucket_minutes
            lane_buckets.setdefault(bucket, []).append(obs["lanes_occupied"])

        # Frames read under a prompt that counted vehicles. Kept so the curve does not go
        # blank for history collected before the geometry existed.
        for obs in conn.execute(
            """SELECT f.captured_at, o.vehicle_count
                 FROM observations o
                 JOIN frames f ON f.id = o.frame_id
                WHERE o.usable = 1 AND o.vehicle_count IS NOT NULL
                  AND f.terminal = ?
                  AND f.captured_at >= ? AND f.captured_at <= ?""",
            (origin, iso(window_start), iso(departure)),
        ).fetchall():
            minutes_before = int(
                (departure - parse_iso(obs["captured_at"])).total_seconds() // 60
            )
            if minutes_before < 0:
                continue
            bucket = (minutes_before // bucket_minutes) * bucket_minutes
            buckets.setdefault(bucket, []).append(obs["vehicle_count"])

    # Lanes first, then counts, then deck space — best-supported measurement to weakest.
    if lane_buckets:
        source, unit, series, descending = "lanes", "lanes in use", lane_buckets, False
    elif buckets:
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


def comparable_report_bounds(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    cutline_minutes_before: int | None = None,
    route_id: str | None = None,
) -> dict | None:
    """What people in the line proved about the cutoff, pooled over comparable sailings.

    `describe_reports` states these same two bounds for one sailing, which means they are
    blank on every future date — every date anybody actually plans against. Pooling them
    the way the distribution pools outcomes puts them back on the page.

    The two bounds are not mirror images, and only one of them is advice:

    - Somebody who joined at 11:20 and did *not* get on proves that arriving that late has
      failed on a day like this. That claim transfers, and it can only move the advice
      earlier — the direction this app is allowed to be wrong in.
    - Somebody who joined at 11:05 and *did* get on proves nothing transferable: whether it
      worked depended on how many vehicles were ahead of them that day. It stays
      descriptive, and is never allowed to say "you can arrive at 11:05".

    Pooled in minutes-before-departure rather than by wall clock, because the tolerance
    admits sailings at neighbouring times: 11:20 is 70 minutes early for a 12:30 and 25 for
    an 11:45, and comparing those as clock strings would quietly mix them.

    Day type must match exactly — no group fallback. The distribution widens because a thin
    bucket of outcomes is still worth summarising; a bound is a claim about one traveller
    on one day, and widening it only makes that day less like yours.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    tolerance = config.query.time_tolerance_minutes
    target_minutes = _hhmm_to_minutes(depart_hhmm)
    target_type = day_type(target_date)
    target_long_weekend = is_long_weekend(target_date)
    departure = combine_local(target_date, parse_hhmm(depart_hhmm), config.tz)

    # Straight from the reports table rather than through `sailings`: a report is keyed by
    # its *scheduled* sailing, so this still sees days the aggregator has not materialised.
    rows = conn.execute(
        """SELECT service_date, depart_hhmm, joined_queue_at, boarded
             FROM sailing_reports
            WHERE route = ? AND origin = ? AND joined_queue_at IS NOT NULL
              AND service_date != ?""",
        (route.id, origin, target_date.isoformat()),
    ).fetchall()

    turned_away: list[tuple[int, str, str]] = []
    boarded: list[int] = []
    sailings: set[tuple[str, str]] = set()
    for row in rows:
        service_date = date.fromisoformat(row["service_date"])
        if day_type(service_date) != target_type:
            continue
        # Same rule the distribution above this panel uses. Without it "on days like this"
        # would pool a long-weekend Friday with ordinary ones — and this is the panel where
        # that matters most, because a single "I was turned away at 11:20" is stated as
        # fact rather than averaged away.
        if is_long_weekend(service_date) != target_long_weekend:
            continue
        if abs(_hhmm_to_minutes(row["depart_hhmm"]) - target_minutes) > tolerance:
            continue
        scheduled = combine_local(service_date, parse_hhmm(row["depart_hhmm"]), config.tz)
        lead = int((scheduled - parse_iso(row["joined_queue_at"])).total_seconds() // 60)
        # Joining at or after the scheduled departure happens when the vessel ran late. It
        # is a real record, but it cannot answer "how early do I need to be".
        if lead <= 0:
            continue
        sailings.add((row["service_date"], row["depart_hhmm"]))
        if row["boarded"]:
            boarded.append(lead)
        else:
            turned_away.append((lead, row["service_date"], row["depart_hhmm"]))

    # The earliest anybody was turned away is the largest lead, and it is deliberately an
    # extreme rather than a median: one person turned away is an observed failure, not a
    # sample from a distribution. Its date travels with it so an outlying day can be seen
    # for what it is instead of quietly setting the advice for every day like it.
    strongest = max(turned_away, default=None)
    turned_away_lead = strongest[0] if strongest else None

    # Only worth printing when it is earlier than what deck space already said. A bound
    # that merely agrees with the headline is a line of type carrying no information.
    tightens = turned_away_lead is not None and (
        cutline_minutes_before is None or turned_away_lead > cutline_minutes_before
    )

    # The latest anybody got on is the smallest lead. Two reports minimum: with one, a
    # single lucky arrival would be published as "everyone who got on".
    boarded_lead = min(boarded) if len(boarded) >= MIN_BOARDED_REPORTS else None

    if not tightens and boarded_lead is None:
        return None

    def clock(lead: int | None) -> str | None:
        return (departure - timedelta(minutes=lead)).strftime("%H:%M") if lead is not None else None

    return {
        "turned_away_at": clock(turned_away_lead) if tightens else None,
        "turned_away_minutes_before": turned_away_lead if tightens else None,
        "turned_away_date": strongest[1] if tightens else None,
        "turned_away_date_label": (
            date.fromisoformat(strongest[1]).strftime("%a %-d %b") if tightens else None
        ),
        "turned_away_sailings": len({(day, hhmm) for _, day, hhmm in turned_away}),
        "boarded_by": clock(boarded_lead),
        "boarded_by_minutes_before": boarded_lead,
        "n_sailings": len(sailings),
        "n_reports": len(turned_away) + len(boarded),
    }


def collection_status(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    limit: int = 6,
    route_id: str | None = None,
) -> dict:
    """What has been collected *for this slot* — equivalent sailings only, newest first.

    The query page is answered from past comparable sailings, so on a new install it is
    correctly blank for weeks — which looks exactly like a broken scraper. This is the
    evidence that separates the two.

    Scoped the same way the distribution is: same terminal, same day type, same departure
    time within the tolerance. Anything wider is not evidence about the sailing on screen —
    a list of last week's afternoon crossings is the same list on every page, which reads
    as data about whichever sailing happens to be selected and is data about none of them.
    Season is deliberately *not* required to match: this panel exists precisely when the
    strict bucket is empty, and a same-slot sailing from the shoulder season is still the
    honest answer to "has anything been recorded here at all".

    Deliberately not a distribution. A handful of sailings is proof of life, not an answer,
    and presenting them as one would undercut the whole point of the comparability rules.
    """
    route = config.route_by_id(route_id) if route_id else config.route
    tolerance = config.query.time_tolerance_minutes
    target_minutes = _hhmm_to_minutes(depart_hhmm)

    rows = conn.execute(
        """SELECT s.service_date, s.depart_hhmm, s.scheduled_departure,
                  r.outcome, r.filled_at, r.method, r.peak_queue
             FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.route = ? AND s.origin = ? AND s.day_type = ?
            ORDER BY s.scheduled_departure DESC""",
        (route.id, origin, day_type(target_date)),
    ).fetchall()
    equivalent = [
        row
        for row in rows
        if abs(_hhmm_to_minutes(row["depart_hhmm"]) - target_minutes) <= tolerance
    ]

    # `unknown` is excluded from the list but counted: a sailing the collector was not
    # running for is a real gap, and hiding it entirely would overstate coverage.
    unknown = sum(1 for row in equivalent if row["outcome"] == "unknown")
    listed = [row for row in equivalent if row["outcome"] != "unknown"][:limit]
    departures = _reported_departures(conn, route.id, origin, listed)

    recent = []
    for row in listed:
        actual = departures.get((row["service_date"], row["depart_hhmm"]))
        scheduled = datetime.fromisoformat(row["scheduled_departure"])
        recent.append(
            {
                "service_date": row["service_date"],
                "depart_hhmm": row["depart_hhmm"],
                "outcome": row["outcome"],
                "label": OUTCOME_LABELS.get(row["outcome"], row["outcome"]),
                # When the vessel actually left, where somebody in the line said so. The
                # feed only ever reports the scheduled sailing, so this is the one place
                # the real departure is known — and it is often not known at all.
                "departed_local": (
                    local(actual, config.tz).strftime("%H:%M") if actual else None
                ),
                "minutes_late": (
                    int((actual - scheduled).total_seconds() // 60) if actual else None
                ),
                "filled_at_local": (
                    local(parse_iso(row["filled_at"]), config.tz).strftime("%H:%M")
                    if row["filled_at"]
                    else None
                ),
                "evidence": _evidence_of({row["method"]}),
                "peak_queue": row["peak_queue"],
            }
        )

    return {
        "recent": recent,
        "recorded": len(equivalent) - unknown,
        "unknown": unknown,
        "since": min((row["service_date"] for row in equivalent), default=None),
        "depart_hhmm": depart_hhmm,
        "tolerance_minutes": tolerance,
    }


def _reported_departures(
    conn: sqlite3.Connection, route_id: str, origin: str, rows: list[sqlite3.Row]
) -> dict[tuple[str, str], datetime]:
    """When each of these sailings actually left, from the people who watched it go.

    Median rather than earliest: two travellers rarely agree to the minute, and one of them
    glancing at the clock as the ramp came up should not become the record.
    """
    if not rows:
        return {}
    placeholders = ",".join("?" for _ in rows)
    reported = conn.execute(
        f"""SELECT service_date, depart_hhmm, departed_at
              FROM sailing_reports
             WHERE route = ? AND origin = ? AND departed_at IS NOT NULL
               AND service_date IN ({placeholders})""",
        (route_id, origin, *(row["service_date"] for row in rows)),
    ).fetchall()

    wanted = {(row["service_date"], row["depart_hhmm"]) for row in rows}
    grouped: dict[tuple[str, str], list[datetime]] = {}
    for row in reported:
        key = (row["service_date"], row["depart_hhmm"])
        if key in wanted:
            grouped.setdefault(key, []).append(parse_iso(row["departed_at"]))
    return {key: sorted(times)[len(times) // 2] for key, times in grouped.items()}
