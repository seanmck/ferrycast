"""Label-free diagnostics for the vision layer.

The question this answers is "is the camera reading tracking reality?", and it answers it
without anyone hand-labelling a single frame. Three properties of the archive stand in for
ground truth, because the sailing cycle constrains what a correct reading can look like:

* **Trend** — a compound fills before a departure. Readings across the lookback window
  should rise. A model whose readings wander up and down through that window is producing
  noise, and that is visible without knowing the true level at any instant.
* **Departure drop** — the vessel leaves and takes the queue with it. The departures board
  publishes the *actual* departure time, which is independent of the camera and free, so
  every sailing hands us a labelled transition: high before, near-empty after.
* **Step noise** — frames are 15 minutes apart. A jump of twenty vehicles between adjacent
  frames is not a queue, it is the model changing its mind.

None of these can catch an error every model makes in the same direction — for that there
is no substitute for a human looking at frames, or for a first-hand report from the line.
What they do catch is instability, which is the property that decides whether a day-vs-day
comparison means anything.

Run it over one prompt version to see how the current reader behaves, or over two to see
what changed when you bumped the version.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Config
from .timeutil import iso, parse_iso

# A frame-to-frame change larger than this is treated as implausible: at a 15-minute
# cadence on a route this size, a real queue does not grow or shrink by more in one step.
# It is a diagnostic threshold, not a correction — nothing is discarded on account of it.
IMPLAUSIBLE_STEP = 20

# How long after the board's departure to look for the drop. The vessel has to finish
# loading and clear the berth before the compound reads empty.
DROP_SETTLE_MINUTES = 12

# What counts as the queue having cleared. Not "any decrease": a compound that goes from 26
# to 24 across a departure has not cleared, it has drifted, and treating that as a pass
# would let a reader that never notices the vessel leave score full marks.
#
# A *partial* drop still passes, because an overloaded sailing genuinely leaves a residual
# behind — that carryover is the outcome the whole archive exists to record. Halving is the
# line: a vessel that takes anything like its capacity removes far more than half of any
# queue this route produces.
DROP_RATIO = 0.5
# Below this the counts are noise and the ratio stops meaning anything.
DROP_FLOOR = 2


@dataclass
class SailingTrend:
    service_date: str
    depart_hhmm: str
    n: int
    rho: float | None            # rank correlation of reading against time
    first: int | None
    peak: int | None


@dataclass
class SailingDrop:
    service_date: str
    depart_hhmm: str
    departed_hhmm: str
    before: int | None
    after: int | None

    @property
    def ratio(self) -> float | None:
        """What share of the queue was still there once the vessel had gone."""
        if self.before is None or self.after is None or self.before == 0:
            return None
        return self.after / self.before

    @property
    def dropped(self) -> bool | None:
        if self.before is None or self.after is None:
            return None
        return self.after <= max(DROP_FLOOR, self.before * DROP_RATIO)


@dataclass
class Coverage:
    frames: int = 0
    observed: int = 0
    usable: int = 0
    sailings: int = 0
    with_outcome: int = 0
    unusable_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class CalibrationReport:
    prompt_version: str
    origin: str | None
    coverage: Coverage = field(default_factory=Coverage)
    trends: list[SailingTrend] = field(default_factory=list)
    drops: list[SailingDrop] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---- headline scores, each in 0..1 where higher is better -------------------

    @property
    def trend_score(self) -> float | None:
        """Share of sailings whose readings rise through the pre-departure window."""
        scored = [t.rho for t in self.trends if t.rho is not None]
        if not scored:
            return None
        return sum(1 for r in scored if r > 0.3) / len(scored)

    @property
    def median_rho(self) -> float | None:
        scored = [t.rho for t in self.trends if t.rho is not None]
        return statistics.median(scored) if scored else None

    @property
    def drop_score(self) -> float | None:
        """Share of departures the camera actually saw the queue clear after."""
        judged = [d.dropped for d in self.drops if d.dropped is not None]
        if not judged:
            return None
        return sum(1 for d in judged if d) / len(judged)

    @property
    def noise_score(self) -> float | None:
        """Share of adjacent-frame steps that are physically plausible."""
        if not self.steps:
            return None
        return sum(1 for s in self.steps if s <= IMPLAUSIBLE_STEP) / len(self.steps)

    @property
    def median_step(self) -> float | None:
        return statistics.median(self.steps) if self.steps else None

    @property
    def usable_share(self) -> float | None:
        if not self.coverage.observed:
            return None
        return self.coverage.usable / self.coverage.observed


def _rank(values: list[float]) -> list[float]:
    """Average ranks, so ties do not fabricate a trend that isn't there."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, hand-rolled so this module adds no dependency.

    Returns None when either series is flat — an all-empty compound is not evidence of a
    bad reader, and calling it a failed trend would punish quiet sailings.
    """
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy) ** 0.5


def _readings(
    conn: sqlite3.Connection,
    prompt_version: str,
    origin: str,
    start: datetime,
    end: datetime,
    *,
    usable_only: bool = True,
) -> list[tuple[datetime, int]]:
    sql = """
        SELECT f.captured_at, o.vehicle_count
          FROM observations o
          JOIN frames f ON f.id = o.frame_id
         WHERE o.prompt_version = ?
           AND f.terminal = ?
           AND f.captured_at >= ?
           AND f.captured_at <= ?
           AND o.vehicle_count IS NOT NULL
    """
    if usable_only:
        sql += " AND o.usable = 1"
    sql += " ORDER BY f.captured_at"
    rows = conn.execute(sql, (prompt_version, origin, iso(start), iso(end))).fetchall()
    return [(parse_iso(r["captured_at"]), int(r["vehicle_count"])) for r in rows]


def _sailings(
    conn: sqlite3.Connection, config: Config, origin: str | None
) -> list[sqlite3.Row]:
    sql = """
        SELECT id, route, origin, service_date, depart_hhmm, scheduled_departure
          FROM sailings
         WHERE route = ?
    """
    params: list = [config.route.id]
    if origin:
        sql += " AND origin = ?"
        params.append(origin)
    sql += " ORDER BY scheduled_departure"
    return list(conn.execute(sql, tuple(params)).fetchall())


def _coverage(conn: sqlite3.Connection, config: Config, prompt_version: str, origin: str | None) -> Coverage:
    cov = Coverage()
    where_terminal = " AND f.terminal = ?" if origin else ""
    p: list = [config.route.id]
    if origin:
        p.append(origin)

    cov.frames = int(
        conn.execute(
            f"SELECT COUNT(*) FROM frames f WHERE f.route = ? AND f.status = 'ok'{where_terminal}",
            tuple(p),
        ).fetchone()[0]
    )
    row = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(o.usable), 0) AS u
              FROM observations o JOIN frames f ON f.id = o.frame_id
             WHERE o.prompt_version = ? AND f.route = ?{where_terminal}""",
        (prompt_version, *p),
    ).fetchone()
    cov.observed, cov.usable = int(row["n"]), int(row["u"])

    # Why frames were dropped. `visibility` is the interesting one: an `obscured` frame is
    # usually still readable, so a large count here is evidence the gate is too strict.
    for r in conn.execute(
        f"""SELECT COALESCE(o.visibility, 'unknown') AS v, COUNT(*) AS n
              FROM observations o JOIN frames f ON f.id = o.frame_id
             WHERE o.prompt_version = ? AND f.route = ?{where_terminal} AND o.usable = 0
             GROUP BY v ORDER BY n DESC""",
        (prompt_version, *p),
    ):
        cov.unusable_reasons[r["v"]] = int(r["n"])

    sail_where = " AND origin = ?" if origin else ""
    sp: list = [config.route.id] + ([origin] if origin else [])
    cov.sailings = int(
        conn.execute(
            f"SELECT COUNT(*) FROM sailings WHERE route = ?{sail_where}", tuple(sp)
        ).fetchone()[0]
    )
    cov.with_outcome = int(
        conn.execute(
            f"""SELECT COUNT(*) FROM sailings s JOIN sailing_records r ON r.sailing_id = s.id
                 WHERE s.route = ?{sail_where} AND r.outcome != 'unknown'""",
            tuple(sp),
        ).fetchone()[0]
    )
    return cov


def calibrate(
    conn: sqlite3.Connection,
    config: Config,
    *,
    prompt_version: str | None = None,
    origin: str | None = None,
    usable_only: bool = True,
) -> CalibrationReport:
    """Score the archive on trend, departure drop and step noise. No labels required."""
    from .aggregate import _board_departure

    prompt_version = prompt_version or config.vision.prompt_version
    report = CalibrationReport(prompt_version=prompt_version, origin=origin)
    report.coverage = _coverage(conn, config, prompt_version, origin)

    if not report.coverage.observed:
        report.notes.append(
            f"no observations at prompt version {prompt_version!r} — "
            "run `ferrycast extract` first, or pass --prompt-version"
        )
        return report

    lookback = config.aggregate.lookback_minutes

    for s in _sailings(conn, config, origin):
        departure = parse_iso(s["scheduled_departure"])
        window = _readings(
            conn, prompt_version, s["origin"], departure - timedelta(minutes=lookback),
            departure, usable_only=usable_only,
        )

        # ---- trend through the fill window ----
        if len(window) >= 3:
            times = [(t - departure).total_seconds() for t, _ in window]
            counts = [float(c) for _, c in window]
            report.trends.append(
                SailingTrend(
                    service_date=s["service_date"],
                    depart_hhmm=s["depart_hhmm"],
                    n=len(window),
                    rho=spearman(times, counts),
                    first=window[0][1],
                    peak=max(c for _, c in window),
                )
            )

        # ---- the drop after the board says it left ----
        board = _board_departure(
            conn, s["route"], s["origin"], s["service_date"], s["depart_hhmm"], config
        )
        if board is not None:
            after = _readings(
                conn, prompt_version, s["origin"],
                board + timedelta(minutes=DROP_SETTLE_MINUTES),
                board + timedelta(minutes=config.aggregate.post_window_minutes),
                usable_only=usable_only,
            )
            report.drops.append(
                SailingDrop(
                    service_date=s["service_date"],
                    depart_hhmm=s["depart_hhmm"],
                    departed_hhmm=board.strftime("%H:%M"),
                    before=max((c for _, c in window), default=None),
                    after=after[0][1] if after else None,
                )
            )

    # ---- step noise across the whole archive ----
    terminals = [origin] if origin else [t.code for t in config.route.terminals]
    for code in terminals:
        series = _readings(
            conn, prompt_version, code,
            datetime.min.replace(tzinfo=parse_iso("1970-01-01T00:00:00Z").tzinfo),
            parse_iso("2999-01-01T00:00:00Z"),
            usable_only=usable_only,
        )
        for (t0, c0), (t1, c1) in zip(series, series[1:], strict=False):
            gap = (t1 - t0).total_seconds() / 60
            # Only consecutive frames tell you anything about step size; a gap over a
            # night of dark frames is not a step, it is a different day.
            if 0 < gap <= 25:
                report.steps.append(abs(c1 - c0))

    return report


def format_report(report: CalibrationReport) -> str:
    """Human-facing summary. Every score is 0..1, higher is better."""
    c = report.coverage
    out: list[str] = []
    out.append(f"prompt version {report.prompt_version}" + (f", {report.origin}" if report.origin else ""))
    out.append("")

    out.append("coverage")
    out.append(f"  frames stored          {c.frames}")
    out.append(f"  with an observation    {c.observed}")
    share = f" ({report.usable_share:.0%})" if report.usable_share is not None else ""
    out.append(f"  marked usable          {c.usable}{share}")
    if c.unusable_reasons:
        detail = ", ".join(f"{k}: {n}" for k, n in c.unusable_reasons.items())
        out.append(f"  dropped as unusable    {detail}")
    out.append(f"  sailings               {c.sailings}")
    out.append(f"  with a known outcome   {c.with_outcome}")
    if c.sailings and c.with_outcome < c.sailings:
        lost = c.sailings - c.with_outcome
        out.append(f"    -> {lost} sailing(s) cannot enter a comparison at all")
    out.append("")

    def line(name: str, score: float | None, detail: str) -> str:
        shown = f"{score:.0%}" if score is not None else "  n/a"
        return f"  {name:<22} {shown:>5}   {detail}"

    out.append("behaviour (no labels involved)")
    rho = report.median_rho
    out.append(
        line(
            "queue builds",
            report.trend_score,
            f"{len(report.trends)} sailing(s), median rho "
            + (f"{rho:+.2f}" if rho is not None else "n/a"),
        )
    )
    out.append(
        line(
            "clears after departure",
            report.drop_score,
            f"{len(report.drops)} departure(s) timed by the board",
        )
    )
    step = report.median_step
    out.append(
        line(
            "plausible steps",
            report.noise_score,
            f"{len(report.steps)} adjacent pair(s), median step "
            + (f"{step:g}" if step is not None else "n/a"),
        )
    )
    out.append("")
    out.append("  A low 'queue builds' or 'clears after departure' means the reader is not")
    out.append("  tracking the sailing cycle. A low 'plausible steps' means it is unstable")
    out.append("  frame to frame. Neither can detect an error every model shares.")

    for note in report.notes:
        out.append(f"\n  note: {note}")
    return "\n".join(out)
