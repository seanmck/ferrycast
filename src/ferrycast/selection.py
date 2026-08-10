"""Choosing which frames are worth paying to read.

Capture is free; extraction is not. Every stored frame *can* be read, but only a few per
sailing actually change the answer, and only some sailings are ever asked about. These
selectors let capture stay continuous while extraction is targeted:

* `essential_frames_for_day` — the minimum needed to classify each sailing (R4).
* `frames_for_sailing` — everything in one sailing's window, for a detailed backfill.
* `recent_frames` — the freshest frames at a terminal, for an on-demand status check.

Frames stay on disk either way, so a frame skipped today can still be read next month.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .config import Config
from .schedule import load_schedule_cached, sailings_for_day
from .timeutil import iso, now_utc, parse_iso


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


BASE_COLUMNS = "f.id, f.terminal, f.captured_at, f.path"


def _unextracted_clause(prompt_version: str) -> tuple[str, list]:
    return (
        """ AND NOT EXISTS (
                SELECT 1 FROM observations o
                 WHERE o.frame_id = f.id AND o.prompt_version = ?
            )""",
        [prompt_version],
    )


def frames_between(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    start: datetime,
    end: datetime,
    *,
    only_unextracted: bool = True,
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT {BASE_COLUMNS} FROM frames f
         WHERE f.terminal = ? AND f.status = 'ok' AND f.path IS NOT NULL
           AND f.captured_at >= ? AND f.captured_at <= ?
    """
    params: list = [terminal, iso(start), iso(end)]
    if only_unextracted:
        clause, extra = _unextracted_clause(config.vision.prompt_version)
        sql += clause
        params += extra
    sql += " ORDER BY f.captured_at"
    return _rows(conn, sql, tuple(params))


def _nearest(rows: list[sqlite3.Row], target: datetime) -> sqlite3.Row | None:
    if not rows:
        return None
    return min(rows, key=lambda row: abs((parse_iso(row["captured_at"]) - target).total_seconds()))


def essential_frames_for_day(
    conn: sqlite3.Connection,
    config: Config,
    day: date,
    *,
    origin: str | None = None,
    only_unextracted: bool = True,
) -> list[sqlite3.Row]:
    """The few frames per sailing that actually determine its outcome.

    Aggregation needs the queue shortly before departure and what remained once the vessel
    had gone. Reading every frame in the two-hour window buys detail the outcome doesn't
    depend on, so by default only the frames nearest the configured offsets are read.
    """
    route = config.route
    blocks = load_schedule_cached(config.schedule_path)
    sailings = sailings_for_day(
        blocks, day, route.id, route.destinations, config.tz, origin=origin
    )
    tolerance = timedelta(minutes=config.vision.essential_tolerance_minutes)

    chosen: dict[int, sqlite3.Row] = {}
    for sailing in sailings:
        for offset in config.vision.essential_offsets_minutes:
            target = sailing.scheduled_departure + timedelta(minutes=offset)
            candidates = frames_between(
                conn,
                config,
                sailing.origin,
                target - tolerance,
                target + tolerance,
                only_unextracted=only_unextracted,
            )
            row = _nearest(candidates, target)
            if row is not None:
                chosen[row["id"]] = row

    return sorted(chosen.values(), key=lambda row: row["captured_at"])


def frames_for_sailing(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    departure: datetime,
    only_unextracted: bool = True,
) -> list[sqlite3.Row]:
    """Every frame in one sailing's aggregation window — the detailed (pricier) option."""
    cfg = config.aggregate
    return frames_between(
        conn,
        config,
        origin,
        departure - timedelta(minutes=cfg.lookback_minutes),
        departure + timedelta(minutes=cfg.post_window_minutes),
        only_unextracted=only_unextracted,
    )


def recent_frames(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    *,
    max_age_minutes: int | None = None,
    limit: int | None = None,
    only_unextracted: bool = False,
    at: datetime | None = None,
) -> list[sqlite3.Row]:
    """The newest frames at a terminal, newest first — the basis of an on-demand check."""
    max_age = max_age_minutes or config.vision.on_demand_max_age_minutes
    limit = limit or config.vision.on_demand_max_frames
    cutoff = (at or now_utc()) - timedelta(minutes=max_age)

    sql = f"""
        SELECT {BASE_COLUMNS} FROM frames f
         WHERE f.terminal = ? AND f.status = 'ok' AND f.path IS NOT NULL
           AND f.captured_at >= ?
    """
    params: list = [terminal, iso(cutoff)]
    if only_unextracted:
        clause, extra = _unextracted_clause(config.vision.prompt_version)
        sql += clause
        params += extra
    sql += " ORDER BY f.captured_at DESC LIMIT ?"
    params.append(limit)
    return _rows(conn, sql, tuple(params))
