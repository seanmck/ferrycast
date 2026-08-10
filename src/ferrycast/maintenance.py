"""Retention and health.

R1 requires frames to be kept for at least a season, with older ones downsampled or pruned.
The P1 anomaly digest lives here too: capture success rate, coverage, and spend, in a form
that is worth emailing only when something is actually wrong.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .schedule import load_schedule_cached, sailings_for_day
from .timeutil import iso, now_utc, parse_iso
from .vision import month_to_date_cost


@dataclass
class PruneResult:
    deleted: int = 0
    downsampled: int = 0
    bytes_freed: int = 0
    dry_run: bool = False
    kept_unextracted: int = 0
    thinned_unextracted: int = 0


def _unlink(config: Config, relative_path: str | None) -> int:
    if not relative_path:
        return 0
    path = Path(config.data_dir) / relative_path
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


def _thin_unextracted(
    conn: sqlite3.Connection, config: Config, result: PruneResult, *, dry_run: bool
) -> None:
    """Thin aged, never-read frames down to the ones an extraction would actually use.

    "Archive frames now, analyse later" only works if the archive stays bounded. Keeping
    every unread frame forever is a disk leak; deleting them outright throws away the whole
    point. So after a while we keep exactly the frames nearest the essential offsets around
    each departure — the set `extract --essential` would read — and drop the rest. Every
    sailing remains fully analysable, at roughly half the disk.
    """
    from .selection import essential_frames_for_day

    cutoff = iso(now_utc() - timedelta(days=config.retention.thin_unextracted_after_days))
    dates = [
        row[0]
        for row in conn.execute(
            """SELECT DISTINCT service_date FROM frames
                WHERE path IS NOT NULL AND captured_at < ?
                  AND NOT EXISTS (SELECT 1 FROM observations o
                                   WHERE o.frame_id = frames.id AND o.prompt_version = ?)
                ORDER BY service_date""",
            (cutoff, config.vision.prompt_version),
        ).fetchall()
    ]

    for service_date in dates:
        day = date.fromisoformat(service_date)
        # Pick the keepers from all frames, not just unread ones, so the choice does not
        # shift as extraction happens.
        keep = {
            row["id"]
            for row in essential_frames_for_day(conn, config, day, only_unextracted=False)
        }
        doomed = conn.execute(
            """SELECT id, path FROM frames
                WHERE path IS NOT NULL AND service_date = ? AND captured_at < ?
                  AND NOT EXISTS (SELECT 1 FROM observations o
                                   WHERE o.frame_id = frames.id AND o.prompt_version = ?)""",
            (service_date, cutoff, config.vision.prompt_version),
        ).fetchall()
        for row in doomed:
            if row["id"] in keep:
                continue
            if not dry_run:
                result.bytes_freed += _unlink(config, row["path"])
                conn.execute("UPDATE frames SET path = NULL WHERE id = ?", (row["id"],))
            result.thinned_unextracted += 1

    if not dry_run:
        conn.commit()


def prune_frames(conn: sqlite3.Connection, config: Config, *, dry_run: bool = False) -> PruneResult:
    """Delete image files past the retention horizon; thin the middle-aged ones.

    Only the image files go — the frame rows and their observations stay, because the
    extracted numbers are the actual dataset and cost almost nothing to keep.

    With extraction deferred (on-demand or sparse), an unread frame is not redundant — it
    is the only remaining record of that moment. So by default pruning skips frames that
    have never been extracted, and reports how many it held back. Set
    `keep_unextracted = false` if you would rather reclaim the disk and accept the gap.
    """
    result = PruneResult(dry_run=dry_run)
    now = now_utc()
    retention = config.retention

    # An unread frame is not redundant — it is the only remaining record of that moment.
    extracted_only = (
        """ AND EXISTS (SELECT 1 FROM observations o
                         WHERE o.frame_id = frames.id AND o.prompt_version = ?)"""
        if retention.keep_unextracted
        else ""
    )
    guard_params = [config.vision.prompt_version] if retention.keep_unextracted else []

    delete_before = iso(now - timedelta(days=retention.keep_frames_days))
    rows = conn.execute(
        f"""SELECT id, path FROM frames
             WHERE path IS NOT NULL AND captured_at < ?{extracted_only}""",
        (delete_before, *guard_params),
    ).fetchall()
    for row in rows:
        if not dry_run:
            result.bytes_freed += _unlink(config, row["path"])
            conn.execute("UPDATE frames SET path = NULL WHERE id = ?", (row["id"],))
        result.deleted += 1

    downsample_before = iso(now - timedelta(days=retention.downsample_after_days))
    candidates = conn.execute(
        f"""SELECT id, path, terminal, captured_at FROM frames
             WHERE path IS NOT NULL AND captured_at < ? AND captured_at >= ?
               {extracted_only}
             ORDER BY terminal, captured_at""",
        (downsample_before, delete_before, *guard_params),
    ).fetchall()

    kept_per_hour: dict[tuple[str, str], int] = {}
    for row in candidates:
        hour_key = (row["terminal"], parse_iso(row["captured_at"]).strftime("%Y-%m-%dT%H"))
        kept = kept_per_hour.get(hour_key, 0)
        if kept < retention.downsample_keep_per_hour:
            kept_per_hour[hour_key] = kept + 1
            continue
        if not dry_run:
            result.bytes_freed += _unlink(config, row["path"])
            conn.execute("UPDATE frames SET path = NULL WHERE id = ?", (row["id"],))
        result.downsampled += 1

    if retention.keep_unextracted and retention.thin_unextracted_after_days:
        _thin_unextracted(conn, config, result, dry_run=dry_run)

    if retention.keep_unextracted:
        result.kept_unextracted = (
            conn.execute(
                """SELECT COUNT(*) FROM frames
                    WHERE path IS NOT NULL AND captured_at < ?
                      AND NOT EXISTS (SELECT 1 FROM observations o
                                       WHERE o.frame_id = frames.id AND o.prompt_version = ?)""",
                (downsample_before, config.vision.prompt_version),
            ).fetchone()[0]
            or 0
        )

    if not dry_run:
        conn.commit()
    return result


@dataclass
class HealthReport:
    window_days: int
    expected_captures: int
    actual_captures: int
    capture_success_rate: float
    failed_captures: int
    deckspace_success_rate: float | None
    frames_awaiting_extraction: int
    unusable_share: float | None
    sailings_total: int
    sailings_with_record: int
    coverage: float
    month_to_date_cost: float
    budget: float
    last_capture_at: str | None
    problems: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.problems


def health_report(
    conn: sqlite3.Connection, config: Config, *, window_days: int = 7
) -> HealthReport:
    now = now_utc()
    since = iso(now - timedelta(days=window_days))

    cameras = sum(1 for t in config.route.terminals if t.configured_for_capture)
    per_day = (24 * 60) // max(1, config.capture.interval_minutes)
    expected = per_day * window_days * max(1, cameras)

    actual = (
        conn.execute(
            "SELECT COUNT(*) FROM frames WHERE status = 'ok' AND captured_at >= ?", (since,)
        ).fetchone()[0]
        or 0
    )
    failed = (
        conn.execute(
            "SELECT COUNT(*) FROM frames WHERE status = 'error' AND captured_at >= ?", (since,)
        ).fetchone()[0]
        or 0
    )
    rate = actual / expected if expected else 0.0

    deck_total = (
        conn.execute(
            "SELECT COUNT(*) FROM deck_space WHERE observed_at >= ?", (since,)
        ).fetchone()[0]
        or 0
    )
    deck_ok = (
        conn.execute(
            "SELECT COUNT(*) FROM deck_space WHERE observed_at >= ? AND fetch_status = 'ok'",
            (since,),
        ).fetchone()[0]
        or 0
    )
    deck_rate = (deck_ok / deck_total) if deck_total else None

    awaiting = (
        conn.execute(
            """SELECT COUNT(*) FROM frames f
                 LEFT JOIN observations o
                   ON o.frame_id = f.id AND o.prompt_version = ?
                WHERE f.status = 'ok' AND f.path IS NOT NULL AND o.id IS NULL""",
            (config.vision.prompt_version,),
        ).fetchone()[0]
        or 0
    )

    obs_total = (
        conn.execute(
            "SELECT COUNT(*) FROM observations WHERE prompt_version = ?",
            (config.vision.prompt_version,),
        ).fetchone()[0]
        or 0
    )
    obs_unusable = (
        conn.execute(
            "SELECT COUNT(*) FROM observations WHERE prompt_version = ? AND usable = 0",
            (config.vision.prompt_version,),
        ).fetchone()[0]
        or 0
    )
    unusable_share = (obs_unusable / obs_total) if obs_total else None

    start_day = (now - timedelta(days=window_days)).date()
    expected_sailings = 0
    try:
        blocks = load_schedule_cached(config.schedule_path)
        destinations = {t.code: t.destination for t in config.route.terminals}
        day = start_day
        while day <= now.date():
            expected_sailings += len(
                sailings_for_day(blocks, day, config.route.id, destinations, config.tz)
            )
            day += timedelta(days=1)
    except Exception:
        expected_sailings = 0

    with_record = (
        conn.execute(
            """SELECT COUNT(*) FROM sailings s
                 JOIN sailing_records r ON r.sailing_id = s.id
                WHERE s.service_date >= ? AND r.outcome != 'unknown'""",
            (start_day.isoformat(),),
        ).fetchone()[0]
        or 0
    )
    coverage = (with_record / expected_sailings) if expected_sailings else 0.0

    last_capture = conn.execute(
        "SELECT MAX(captured_at) FROM frames WHERE status = 'ok'"
    ).fetchone()[0]

    spend = month_to_date_cost(conn)

    problems: list[str] = []
    scheduled_capture = config.capture.scheduled

    # With capture on demand, the deck-space feed is the pipeline that must stay healthy —
    # it is what the historical record is built from. Frame capture is only judged when it
    # is actually meant to be running.
    if scheduled_capture:
        if cameras == 0:
            problems.append("no webcam URLs are configured, so no frames are being captured")
        if expected and rate < 0.95:
            problems.append(f"capture success rate {rate:.0%} is below the 95% target")
        if last_capture and (now - parse_iso(last_capture)) > timedelta(
            minutes=config.capture.interval_minutes * 4
        ):
            problems.append(f"no successful capture since {last_capture}")
        if awaiting > config.vision.max_frames_per_run * 4:
            problems.append(f"{awaiting} frames are waiting on extraction")
    else:
        expected = 0  # nothing was scheduled, so a rate against it would be meaningless

    if deck_rate is not None and deck_rate < 0.95:
        problems.append(f"deck-space scrape success rate {deck_rate:.0%} is below 95%")
    if deck_total == 0:
        problems.append(
            "no deck-space readings in this window — that feed is the historical record"
        )
    if config.vision.monthly_budget_usd and spend > config.vision.monthly_budget_usd * 0.9:
        problems.append(
            f"vision spend ${spend:.2f} is near the ${config.vision.monthly_budget_usd:.2f} budget"
        )
    if expected_sailings and coverage < 0.90:
        problems.append(f"only {coverage:.0%} of recent sailings have a record (target 90%)")

    return HealthReport(
        window_days=window_days,
        expected_captures=expected,
        actual_captures=actual,
        capture_success_rate=round(rate, 4),
        failed_captures=failed,
        deckspace_success_rate=round(deck_rate, 4) if deck_rate is not None else None,
        frames_awaiting_extraction=awaiting,
        unusable_share=round(unusable_share, 4) if unusable_share is not None else None,
        sailings_total=expected_sailings,
        sailings_with_record=with_record,
        coverage=round(coverage, 4),
        month_to_date_cost=round(spend, 4),
        budget=config.vision.monthly_budget_usd,
        last_capture_at=last_capture,
        problems=problems,
    )


def format_health(report: HealthReport) -> str:
    lines = [
        f"FerryCast health — last {report.window_days} days",
        f"  captures         {report.actual_captures}/{report.expected_captures} "
        f"({report.capture_success_rate:.0%}), {report.failed_captures} failed",
    ]
    if report.deckspace_success_rate is not None:
        lines.append(f"  deck space       {report.deckspace_success_rate:.0%} parsed")
    lines += [
        f"  extraction queue {report.frames_awaiting_extraction} frames",
        f"  sailing coverage {report.sailings_with_record}/{report.sailings_total} "
        f"({report.coverage:.0%})",
        f"  vision spend     ${report.month_to_date_cost:.2f} of ${report.budget:.2f} this month",
        f"  last capture     {report.last_capture_at or 'never'}",
    ]
    if report.problems:
        lines.append("  problems:")
        lines += [f"    - {p}" for p in report.problems]
    else:
        lines.append("  no problems detected")
    return "\n".join(lines)


def add_tag(
    conn: sqlite3.Connection, service_date: date, tag: str, note: str | None = None
) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO event_tags (service_date, tag, note, source, created_at)
           VALUES (?, ?, ?, 'manual', ?)""",
        (service_date.isoformat(), tag, note, iso(now_utc())),
    )
    conn.commit()
    return bool(cur.rowcount)


def list_tags(conn: sqlite3.Connection, service_date: date | None = None) -> list[dict]:
    if service_date:
        rows = conn.execute(
            "SELECT * FROM event_tags WHERE service_date = ? ORDER BY tag",
            (service_date.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM event_tags ORDER BY service_date DESC, tag"
        ).fetchall()
    return [dict(row) for row in rows]
