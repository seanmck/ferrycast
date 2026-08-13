"""Raw data export — the "curious data tinkerer" user story.

Per-frame observations and per-sailing records both export, as CSV or JSON, so the dataset
can be analysed outside FerryCast without touching the SQLite file.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3

FRAME_EXPORT_SQL = """
    SELECT f.id            AS frame_id,
           f.route, f.terminal, f.captured_at, f.service_date,
           f.status, f.error, f.width, f.height, f.bytes, f.path,
           o.prompt_version, o.model, o.vehicle_count, o.lanes_occupied,
           o.queue_beyond_frame, o.ferry_at_dock, o.visibility, o.fullness, o.confidence,
           o.usable, o.notes, o.cost_usd
      FROM frames f
      LEFT JOIN observations o ON o.frame_id = f.id
"""

SAILING_EXPORT_SQL = """
    SELECT s.id AS sailing_id, s.route, s.origin, s.destination, s.service_date,
           s.scheduled_departure, s.depart_hhmm, s.day_type, s.season,
           r.outcome, r.peak_queue, r.queue_at_departure, r.residual_queue,
           r.carryover, r.overload, r.cancelled, r.n_frames, r.confidence,
           r.queue_truncated, r.deck_space_min, r.method,
           r.peak_fullness, r.fullness_at_departure, r.residual_fullness,
           r.queue_started_at, r.cleared_at, r.left_full, r.computed_at
      FROM sailings s
      LEFT JOIN sailing_records r ON r.sailing_id = s.id
"""

DECK_SPACE_EXPORT_SQL = """
    SELECT id, route, terminal, observed_at, service_date, sailing_hhmm,
           percent_available, vessel, status_text, fetch_status, error
      FROM deck_space
"""

REPORT_EXPORT_SQL = """
    SELECT id, route, origin, service_date, depart_hhmm, joined_queue_at, departed_at,
           boarded, deck_fullness, source, submitted_at
      FROM sailing_reports
"""

DATASETS = {
    "frames": (FRAME_EXPORT_SQL, "f.captured_at", "f.captured_at"),
    "sailings": (SAILING_EXPORT_SQL, "s.service_date", "s.scheduled_departure"),
    "deck_space": (DECK_SPACE_EXPORT_SQL, "observed_at", "observed_at"),
    "reports": (REPORT_EXPORT_SQL, "service_date", "service_date"),
}


def _build_query(dataset: str, since: str | None, until: str | None) -> tuple[str, tuple]:
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; choose from {', '.join(DATASETS)}")
    sql, filter_column, order_column = DATASETS[dataset]
    clauses: list[str] = []
    params: list = []
    if since:
        clauses.append(f"{filter_column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{filter_column} <= ?")
        params.append(until)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {order_column}"
    return sql, tuple(params)


def rows_for(
    conn: sqlite3.Connection, dataset: str, *, since: str | None = None, until: str | None = None
) -> list[dict]:
    sql, params = _build_query(dataset, since, until)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def to_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, default=str)


def export(
    conn: sqlite3.Connection,
    dataset: str,
    fmt: str = "csv",
    *,
    since: str | None = None,
    until: str | None = None,
) -> str:
    rows = rows_for(conn, dataset, since=since, until=until)
    if fmt == "csv":
        return to_csv(rows)
    if fmt == "json":
        return to_json(rows)
    raise ValueError(f"unknown format {fmt!r}; choose csv or json")
