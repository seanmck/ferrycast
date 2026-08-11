"""On-demand status: "I'm travelling today — how does it look right now?"

This is the cheap path. Rather than reading every captured frame on a schedule, it grabs a
fresh frame at the terminal you're leaving from, reads just that one (plus a couple of
recent ones for a trend), and reports it alongside the historical distribution for the
sailing you're aiming at.

Cost is a few tenths of a cent per check. The historical side is free: it is already-stored
sailing records, no model call involved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Config
from .selection import recent_frames
from .timeutil import local, now_utc, parse_iso
from .vision import ExtractionStats, extract_frames


@dataclass
class QueueReading:
    captured_at: str          # UTC ISO
    observed_at_local: str    # human-facing local time
    vehicle_count: int | None
    confidence: float | None
    visibility: str | None
    ferry_at_dock: bool
    queue_beyond_frame: bool
    usable: bool
    age_minutes: int


@dataclass
class LiveStatus:
    origin: str
    terminal_name: str
    checked_at: str
    latest: QueueReading | None = None
    trend: list[QueueReading] = field(default_factory=list)
    captured_now: bool = False
    frames_read: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def has_reading(self) -> bool:
        return self.latest is not None and self.latest.usable

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _reading(row: sqlite3.Row, config: Config, reference: datetime) -> QueueReading:
    captured = parse_iso(row["captured_at"])
    return QueueReading(
        captured_at=row["captured_at"],
        observed_at_local=local(captured, config.tz).strftime("%H:%M"),
        vehicle_count=row["vehicle_count"],
        confidence=row["confidence"],
        visibility=row["visibility"],
        ferry_at_dock=bool(row["ferry_at_dock"]),
        queue_beyond_frame=bool(row["queue_beyond_frame"]),
        usable=bool(row["usable"]),
        age_minutes=max(0, int((reference - captured).total_seconds() // 60)),
    )


def _observations_for(
    conn: sqlite3.Connection, config: Config, terminal: str, since: datetime
) -> list[sqlite3.Row]:
    from .timeutil import iso

    return list(
        conn.execute(
            """SELECT f.captured_at, o.vehicle_count, o.confidence, o.visibility,
                      o.ferry_at_dock, o.queue_beyond_frame, o.usable
                 FROM observations o
                 JOIN frames f ON f.id = o.frame_id
                WHERE o.prompt_version = ? AND f.terminal = ? AND f.captured_at >= ?
                ORDER BY f.captured_at DESC""",
            (config.vision.prompt_version, terminal, iso(since)),
        ).fetchall()
    )


def check_now(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    capture_fresh: bool = True,
    max_frames: int | None = None,
    client=None,
    at: datetime | None = None,
) -> LiveStatus:
    """Read the queue at `origin` right now, spending as little as possible.

    Captures a frame first (free) so this works even with no capture cron running at all,
    then extracts only the newest few frames that have not already been read.
    """
    reference = at or now_utc()
    terminal = config.route.terminal(origin)
    status = LiveStatus(
        origin=origin,
        terminal_name=terminal.name,
        checked_at=reference.isoformat().replace("+00:00", "Z"),
    )

    if capture_fresh:
        if not terminal.configured_for_capture:
            status.notes.append(f"no webcam_url configured for {origin}")
        else:
            from .capture import capture_once

            outcomes = capture_once(conn, config, terminals=[origin], at=reference)
            outcome = next((o for o in outcomes if o.terminal == origin), None)
            if outcome and outcome.ok:
                status.captured_now = True
            elif outcome and outcome.error:
                # A stale-but-usable frame still beats no answer, so carry on.
                status.notes.append(f"could not capture a fresh frame: {outcome.error}")

    limit = max_frames or config.vision.on_demand_max_frames
    pending = recent_frames(
        conn, config, origin, limit=limit, only_unextracted=True, at=reference
    )
    if pending:
        stats: ExtractionStats = extract_frames(
            conn, config, pending, client=client, job_name="check"
        )
        status.frames_read = stats.extracted
        status.cost_usd = round(stats.cost_usd, 6)
        if stats.budget_stopped:
            status.notes.append("monthly vision budget reached — showing what is on hand")
        status.notes.extend(stats.errors[:2])

    window_start = reference - timedelta(minutes=config.vision.on_demand_max_age_minutes)
    rows = _observations_for(conn, config, origin, window_start)
    readings = [_reading(row, config, reference) for row in rows]

    status.latest = next((r for r in readings if r.usable), readings[0] if readings else None)
    status.trend = readings[:limit]
    if not readings:
        status.notes.append(
            "no recent frames at this terminal — is capture running, or is the camera down?"
        )
    elif status.latest is not None and not status.latest.usable:
        status.notes.append(
            f"latest frame is not usable ({status.latest.visibility or 'unclear'}) — "
            "night or weather"
        )
    return status


def check_and_compare(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date | None = None,
    depart_hhmm: str | None = None,
    capture_fresh: bool = True,
    client=None,
    at: datetime | None = None,
) -> dict:
    """The morning-of-departure answer: what it looks like now, and how days like this go.

    The historical half costs nothing — it reads sailing records that already exist.
    """
    from .query import (
        comparable_report_bounds,
        default_sailing_time,
        query_distribution,
        sailing_times,
        upcoming_sailings,
    )

    reference = at or now_utc()
    target_date = target_date or local(reference, config.tz).date()

    if not depart_hhmm:
        upcoming = upcoming_sailings(config, origin=origin, at=reference, limit=1)
        if upcoming:
            depart_hhmm = upcoming[0].depart_hhmm
            target_date = upcoming[0].service_date
        else:
            times = sailing_times(config, origin, target_date)
            depart_hhmm = default_sailing_time(config, times, target_date, at=reference)

    status = check_now(
        conn,
        config,
        origin=origin,
        capture_fresh=capture_fresh,
        client=client,
        at=reference,
    )

    distribution = None
    line_bounds = None
    if depart_hhmm:
        distribution = query_distribution(
            conn, config, origin=origin, target_date=target_date, depart_hhmm=depart_hhmm
        ).to_dict()
        line_bounds = comparable_report_bounds(
            conn,
            config,
            origin=origin,
            target_date=target_date,
            depart_hhmm=depart_hhmm,
            cutline_minutes_before=distribution["typical_fill_minutes_before"],
        )

    return {
        "status": status.to_dict(),
        "sailing": {
            "origin": origin,
            "destination": config.route.terminal(origin).destination,
            "service_date": target_date.isoformat(),
            "depart_hhmm": depart_hhmm,
        },
        "history": distribution,
        "line_bounds": line_bounds,
        "cost_usd": status.cost_usd,
    }
