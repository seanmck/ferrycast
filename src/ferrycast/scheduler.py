"""In-process scheduler, for hosts where cron isn't available.

On a VPS the crontab in `deploy/` is the right answer. On a container host — Railway, Fly,
Render, plain Docker — there is no cron, and splitting the work across services would mean
several processes writing one SQLite file over a shared volume, which is a good way to meet
lock contention. So a single container runs the web UI and this scheduler together.

Due-ness is read from the `job_runs` table rather than kept in memory, so a redeploy or
crash does not re-run everything, and does not skip anything either: the schedule survives
in the same volume as the data.

Nothing here spends money. Vision runs only when someone asks for it.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .config import Config
from .db import connect, init_db
from .timeutil import now_utc, parse_iso

TICK_SECONDS = 30


@dataclass
class Job:
    name: str
    interval: timedelta
    run: Callable[[object, Config], str]
    enabled: Callable[[Config], bool] = lambda config: True


def _capture(conn, config: Config) -> str:
    from .capture import capture_once

    outcomes = capture_once(conn, config)
    ok = sum(1 for o in outcomes if o.ok)
    failed = [o for o in outcomes if not o.ok and not o.skipped]
    detail = f"{ok} frame(s)"
    if failed:
        detail += f", {len(failed)} failed: " + "; ".join(
            f"{o.terminal} {o.error}" for o in failed
        )
    return detail


def _scrape(conn, config: Config) -> str:
    from .deckspace import scrape_once

    results = scrape_once(conn, config)
    rows = sum(r.get("rows", 0) for r in results)
    failed = [r for r in results if not r["ok"] and not r.get("skipped")]
    detail = f"{rows} deck-space row(s)"
    if failed:
        detail += ", " + "; ".join(f"{r['terminal']} {r.get('error')}" for r in failed)
    return detail


def _marine(conn, config: Config) -> str:
    from .marine import refresh

    result = refresh(conn, config)
    if not result["ok"]:
        return f"no forecast: {result.get('error', 'skipped')}"
    detail = f"{result['rows']} day(s) from {result['issued_at']}"
    if result.get("warning"):
        detail += f" — WARNING: {result['warning']}"
    return detail


def _aggregate(conn, config: Config) -> str:
    """Re-aggregate the last few days so today's sailings appear as the feed fills in."""
    from .aggregate import aggregate_range
    from .timeutil import local

    today = local(now_utc(), config.tz).date()
    counts = aggregate_range(conn, config, today - timedelta(days=2), today)
    recorded = sum(v for k, v in counts.items() if k != "unknown")
    return f"{recorded} sailing(s) recorded, {counts.get('unknown', 0)} unknown"


def _prune(conn, config: Config) -> str:
    from .maintenance import prune_frames

    result = prune_frames(conn, config)
    return (
        f"removed {result.deleted}, thinned {result.downsampled + result.thinned_unextracted},"
        f" freed {result.bytes_freed / 1e6:.1f} MB"
    )


JOBS: tuple[Job, ...] = (
    Job(
        "capture",
        timedelta(minutes=15),
        _capture,
        enabled=lambda c: c.capture.scheduled
        and any(t.configured_for_capture for t in c.route.terminals),
    ),
    Job(
        "deckspace",
        timedelta(minutes=15),
        _scrape,
        enabled=lambda c: any(t.deck_space_url for t in c.route.terminals),
    ),
    # ECCC issues marine forecasts roughly every six hours and amends between. Three hours
    # catches an amendment well before anyone plans around it, and costs a handful of small
    # directory listings — there is no "latest" path to ask for, so each run walks back
    # through hours until it finds one.
    Job(
        "marine",
        timedelta(hours=3),
        _marine,
        enabled=lambda c: c.route.marine is not None,
    ),
    Job("aggregate", timedelta(hours=1), _aggregate),
    Job("prune", timedelta(days=7), _prune),
)


def jobs_for(config: Config) -> list[Job]:
    """Active jobs, with capture/scrape following the configured poll interval."""
    interval = timedelta(minutes=config.capture.interval_minutes)
    active = []
    for job in JOBS:
        if not job.enabled(config):
            continue
        if job.name in ("capture", "deckspace"):
            job = Job(job.name, interval, job.run, job.enabled)
        active.append(job)
    return active


def last_run(conn, job_name: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(started_at) FROM job_runs WHERE job = ?", (job_name,)
    ).fetchone()
    return parse_iso(row[0]) if row and row[0] else None


def is_due(conn, job: Job, *, at: datetime | None = None) -> bool:
    previous = last_run(conn, job.name)
    if previous is None:
        return True
    return (at or now_utc()) - previous >= job.interval


def due_jobs(conn, config: Config, *, at: datetime | None = None) -> list[Job]:
    return [job for job in jobs_for(config) if is_due(conn, job, at=at)]


def run_due(conn, config: Config, *, at: datetime | None = None, log=print) -> list[str]:
    """Run whatever is due, once. A failing job is logged and never stops the others."""
    ran = []
    for job in due_jobs(conn, config, at=at):
        try:
            detail = job.run(conn, config)
            log(f"[scheduler] {job.name}: {detail}")
        except Exception as exc:  # a scheduler that dies on one bad job is useless
            log(f"[scheduler] {job.name} FAILED: {type(exc).__name__}: {exc}")
            log(traceback.format_exc())
        ran.append(job.name)
    return ran


def run_forever(config: Config, *, stop: threading.Event | None = None, log=print) -> None:
    stop = stop or threading.Event()
    conn = init_db(config.db_path)
    plan = ", ".join(f"{j.name} every {j.interval}" for j in jobs_for(config))
    log(f"[scheduler] started: {plan or 'nothing enabled'}")
    try:
        while not stop.is_set():
            try:
                run_due(conn, config, log=log)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive whatever happens
                log(f"[scheduler] tick failed: {type(exc).__name__}: {exc}")
            stop.wait(TICK_SECONDS)
    finally:
        conn.close()
        log("[scheduler] stopped")


def start_background(config: Config, *, log=print) -> tuple[threading.Thread, threading.Event]:
    """Start the scheduler alongside a web server in the same container."""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(config,),
        kwargs={"stop": stop, "log": log},
        name="ferrycast-scheduler",
        daemon=True,
    )
    thread.start()
    return thread, stop


def describe(conn, config: Config) -> list[dict]:
    """What is scheduled, when each job last ran, and when it is next due."""
    now = now_utc()
    rows = []
    for job in jobs_for(config):
        previous = last_run(conn, job.name)
        rows.append(
            {
                "job": job.name,
                "interval_minutes": int(job.interval.total_seconds() // 60),
                "last_run": previous.isoformat().replace("+00:00", "Z") if previous else None,
                "next_due": (previous + job.interval).isoformat().replace("+00:00", "Z")
                if previous
                else "now",
                "due": is_due(conn, job, at=now),
            }
        )
    return rows


def _today(config: Config) -> date:
    from .timeutil import local

    return local(now_utc(), config.tz).date()


__all__ = [
    "Job",
    "connect",
    "describe",
    "due_jobs",
    "is_due",
    "jobs_for",
    "run_due",
    "run_forever",
    "start_background",
]
