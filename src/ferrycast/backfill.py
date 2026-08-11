"""On-demand history: analyse a slot's comparable sailings only when someone asks.

The compromise this implements. Frames are archived continuously and free; the vision
model is the only thing that costs money, so it runs when a person actually looks at a
sailing rather than nightly over everything. Nobody pays to analyse the 05:35 on a wet
Tuesday that no one will ever ask about.

This works because the two halves have opposite reversibility. A frame not captured at
14:15 is gone for good; an *unread* frame can be read at any later date. So capture is
eager and extraction is lazy, and the history is not lost in the meantime — merely not yet
paid for.

Extraction is idempotent per (frame, prompt_version), so a slot is paid for once. The
second person to ask about the Friday 15:40 gets the answer for nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .schedule import day_type, season
from .timeutil import parse_hhmm, parse_iso

# Never analyse the whole backlog because someone opened a page. The default is the
# smallest number that can produce a distribution the query layer will not flag as thin.
DEFAULT_MAX_SAILINGS = 6


@dataclass
class Candidate:
    sailing_id: int
    service_date: str
    depart_hhmm: str
    scheduled_departure: str
    origin: str
    frames: int = 0


@dataclass
class FillResult:
    candidates: int = 0
    attempted: int = 0
    filled: int = 0
    frames_read: int = 0
    cost_usd: float = 0.0
    budget_stopped: bool = False
    skipped_no_frames: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidates": self.candidates,
            "attempted": self.attempted,
            "filled": self.filled,
            "frames_read": self.frames_read,
            "cost_usd": round(self.cost_usd, 4),
            "budget_stopped": self.budget_stopped,
            "skipped_no_frames": self.skipped_no_frames,
            "errors": self.errors,
        }


def _minutes(hhmm: str) -> int:
    parsed = parse_hhmm(hhmm)
    return parsed.hour * 60 + parsed.minute


def fillable_sailings(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    limit: int = DEFAULT_MAX_SAILINGS,
    now: date | None = None,
) -> list[Candidate]:
    """Past sailings comparable to this slot that have frames but no usable outcome.

    Comparability matches the query layer — same terminal, day type, season and departure
    time within tolerance — so what gets analysed is exactly what would answer the question
    asked. Analysing anything else would be spending money on a different question.

    Newest first: a recent Friday resembles this Friday more than one from April does, and
    if the budget stops the run partway the sailings that were paid for are the better ones.
    """
    today = now or date.today()
    route = config.route
    tolerance = config.query.time_tolerance_minutes
    target = _minutes(depart_hhmm)

    rows = conn.execute(
        """SELECT s.id, s.service_date, s.depart_hhmm, s.scheduled_departure, s.origin
             FROM sailings s
             LEFT JOIN sailing_records r ON r.sailing_id = s.id
            WHERE s.route = ? AND s.origin = ? AND s.day_type = ? AND s.season = ?
              AND s.service_date < ?
              AND (r.sailing_id IS NULL OR r.outcome = 'unknown')
            ORDER BY s.scheduled_departure DESC""",
        (
            route.id,
            origin,
            day_type(target_date),
            season(target_date),
            today.isoformat(),
        ),
    ).fetchall()

    candidates: list[Candidate] = []
    for row in rows:
        if abs(_minutes(row["depart_hhmm"]) - target) > tolerance:
            continue
        candidates.append(
            Candidate(
                sailing_id=row["id"],
                service_date=row["service_date"],
                depart_hhmm=row["depart_hhmm"],
                scheduled_departure=row["scheduled_departure"],
                origin=row["origin"],
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def estimate_frames(
    conn: sqlite3.Connection, config: Config, candidates: list[Candidate]
) -> list[Candidate]:
    """Count the unread frames each candidate would cost, without reading any of them."""
    from .selection import essential_frames_for_sailing

    for candidate in candidates:
        candidate.frames = len(
            essential_frames_for_sailing(
                conn,
                config,
                origin=candidate.origin,
                departure=parse_iso(candidate.scheduled_departure),
            )
        )
    return candidates


def fill_for_slot(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    max_sailings: int = DEFAULT_MAX_SAILINGS,
    dry_run: bool = False,
    client=None,
    now: date | None = None,
) -> FillResult:
    """Read the frames that answer this slot, then re-aggregate the days involved.

    Aggregation is per-day rather than per-sailing because that is the unit the aggregator
    works in, and re-running it is cheap: it touches no model, only the frames already
    extracted. Each affected day is aggregated once, however many of its sailings were read.
    """
    from .aggregate import aggregate_day
    from .selection import essential_frames_for_sailing
    from .vision import extract_frames

    result = FillResult()
    candidates = estimate_frames(
        conn,
        config,
        fillable_sailings(
            conn,
            config,
            origin=origin,
            target_date=target_date,
            depart_hhmm=depart_hhmm,
            limit=max_sailings,
            now=now,
        ),
    )
    result.candidates = len(candidates)

    touched_days: set[str] = set()
    for candidate in candidates:
        if not candidate.frames:
            # Archived nothing for that window — usually a day before capture was running.
            # Not an error, and not worth a model call to discover.
            result.skipped_no_frames += 1
            continue

        frames = essential_frames_for_sailing(
            conn,
            config,
            origin=candidate.origin,
            departure=parse_iso(candidate.scheduled_departure),
        )
        result.attempted += 1
        stats = extract_frames(
            conn, config, frames, client=client, dry_run=dry_run, job_name="backfill"
        )
        result.frames_read += stats.extracted
        result.cost_usd += stats.cost_usd
        result.errors.extend(stats.errors)
        if stats.budget_stopped:
            result.budget_stopped = True
            break
        if stats.extracted:
            result.filled += 1
            touched_days.add(candidate.service_date)

    if not dry_run:
        for service_date in sorted(touched_days):
            aggregate_day(conn, config, date.fromisoformat(service_date))

    return result


def describe(result: FillResult) -> str:
    if not result.candidates:
        return "nothing to fill: every comparable sailing already has a record"
    parts = [
        f"{result.filled}/{result.attempted} sailing(s) filled",
        f"{result.frames_read} frame(s) read",
        f"${result.cost_usd:.4f}",
    ]
    if result.skipped_no_frames:
        parts.append(f"{result.skipped_no_frames} had no archived frames")
    if result.budget_stopped:
        parts.append("stopped on the monthly budget")
    return ", ".join(parts)


# A frame at the configured width costs about this much to read. Measured, not derived:
# 20 frames came to $0.0200 at Haiku list price. Used only to price the button — the real
# cost is metered per call and recorded as it is spent.
TYPICAL_INPUT_TOKENS = 800
TYPICAL_OUTPUT_TOKENS = 40


def estimate_cost(config: Config, frames: int) -> float:
    return (
        frames * TYPICAL_INPUT_TOKENS / 1_000_000 * config.vision.input_usd_per_mtok
        + frames * TYPICAL_OUTPUT_TOKENS / 1_000_000 * config.vision.output_usd_per_mtok
    )


def fill_offer(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    target_date: date,
    depart_hhmm: str,
    distribution: dict | None,
    limit: int = DEFAULT_MAX_SAILINGS,
) -> dict | None:
    """What filling this slot would buy, or None if there is nothing worth buying.

    Offered in two cases, which are different questions:

    - The sample is thin. The answer needs this, so the offer is the point of the page.
    - The sample is sufficient but there are *newer* comparable sailings unread. Without
      this the distribution would freeze at whatever the first fill produced and age
      silently — still showing five sailings, still saying "sufficient", quietly describing
      a season that has passed.

    Returns None once every comparable sailing has a record, so the button disappears rather
    than inviting a tap that would do nothing.
    """
    candidates = [
        candidate
        for candidate in estimate_frames(
            conn,
            config,
            fillable_sailings(
                conn,
                config,
                origin=origin,
                target_date=target_date,
                depart_hhmm=depart_hhmm,
                limit=limit,
            ),
        )
        if candidate.frames
    ]
    if not candidates:
        return None

    n = (distribution or {}).get("n") or 0
    sufficient = bool((distribution or {}).get("sufficient"))
    newest_sample = max(
        (s["service_date"] for s in (distribution or {}).get("samples") or []), default=""
    )
    newer_available = any(c.service_date > newest_sample for c in candidates)

    if sufficient and not newer_available:
        return None

    frames = sum(c.frames for c in candidates)
    return {
        "sailings": len(candidates),
        "frames": frames,
        "cost_usd": round(estimate_cost(config, frames), 4),
        # "more" when it is deepening a thin answer, "newer" when it is refreshing a
        # sufficient one — the two read differently and should not share a label.
        "reason": "thin" if not sufficient else "newer",
        "has_history": n > 0,
    }
