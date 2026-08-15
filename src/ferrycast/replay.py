"""Archive a camera that publishes its recent past, not just its present.

Every other source FerryCast collects is capture-or-lose-forever. A deck-space reading not
taken at 14:15 is unrecoverable; so is the frame. That is why `capture` runs on a timer and
why a missed run is a permanent hole in the record.

Some camera feeds are not like that. DriveBC publishes, alongside the current image, an
index of the stills it took over the last twenty-four hours and the address of each one. A
feed like that inverts the collection problem:

* **A daily sweep gets complete coverage.** The window is a day wide and the frames inside
  it are already taken, so one run a day recovers all of them at the feed's own cadence.
  There is nothing to be gained by polling every fifteen minutes.
* **It is self-healing.** A failed run costs nothing so long as a later one lands before the
  window rolls over. Compare a live camera, where the same failure is a hole forever.
* **It starts with history.** The first sweep ever run already banks a day.

What it is not is unlimited. The window is all there is — miss it by a day and that day is
gone as surely as any live capture, so this still wants a schedule, just a forgiving one.

Everything here is keyed on (terminal, camera, captured_at) and writes through
`capture.store_frame`, so a sweep is idempotent: run it twice and the second run fetches
nothing. `captured_at` is the feed's own timestamp, not the time of the sweep, which is what
makes that true and what puts the frames in the archive at the moment they were taken.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .capture import store_frame
from .config import Camera, Config
from .db import JobRun
from .fetching import fetch
from .timeutil import iso

#: Ceiling on frames fetched from one camera in one sweep. A day of quarter-hourly stills is
#: 96, so this is roomy for the intended case and a brake on the unintended one — an index
#: URL pointing at something that is not an index, answering with thousands of entries.
MAX_FRAMES_PER_SWEEP = 500


class ReplayError(Exception):
    pass


@dataclass
class ReplayStats:
    terminal: str = ""
    camera: str = ""
    offered: int = 0          # timestamps the feed listed
    already_held: int = 0     # of those, ones the archive already has
    fetched: int = 0
    failed: int = 0
    capped: int = 0           # missing frames left for the next sweep by MAX_FRAMES_PER_SWEEP
    errors: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return self.offered - self.already_held


def parse_index(payload: str, camera: Camera) -> list[datetime]:
    """Timestamps from the feed's index, oldest first.

    The feed's order is not relied on. DriveBC happens to answer oldest first, but a sweep
    that has to stop early should stop at the newest end rather than wherever the publisher
    chose to begin, so they are sorted here and the caller works forward from the oldest.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReplayError(f"replay index was not JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ReplayError(
            f"replay index was {type(raw).__name__}, not a list of timestamps — "
            "check `replay_index_url` points at the index and not at the camera page"
        )
    out = []
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            out.append(
                datetime.strptime(item, camera.replay_timestamp_format).replace(tzinfo=UTC)
            )
        except ValueError:
            continue
    if raw and not out:
        raise ReplayError(
            f"none of the {len(raw)} entries in the replay index parsed as "
            f"{camera.replay_timestamp_format!r} timestamps"
        )
    return sorted(out)


def _held(
    conn: sqlite3.Connection, terminal: str, camera: str, moments: list[datetime]
) -> set[str]:
    """Which of these the archive already has, as a settled fact rather than a gap.

    A row that failed is deliberately *not* counted as held. Refetching it is the whole point
    of a feed with a window: the earlier attempt can be retried while the frame is still
    published, which is a thing no live camera ever offers.
    """
    if not moments:
        return set()
    rows = conn.execute(
        f"""SELECT captured_at FROM frames
             WHERE terminal = ? AND camera = ? AND status = 'ok'
               AND captured_at IN ({",".join("?" * len(moments))})""",
        (terminal, camera, *[iso(m) for m in moments]),
    ).fetchall()
    return {row["captured_at"] for row in rows}


def _clear_failed(conn: sqlite3.Connection, terminal: str, camera: str, moment: datetime) -> None:
    """Drop a previous failed attempt so the retry can be recorded.

    Necessary because every writer here uses INSERT OR IGNORE: without this the refetched
    bytes would land on disk and the row would stay `error` forever, leaving a file nothing
    references. Only rows that failed are removed, and a failed row has no path and no
    observations hanging off it.
    """
    conn.execute(
        "DELETE FROM frames WHERE terminal = ? AND camera = ? AND captured_at = ? "
        "AND status = 'error'",
        (terminal, camera, iso(moment)),
    )


def sweep_camera(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    camera: Camera,
    *,
    limit: int = MAX_FRAMES_PER_SWEEP,
    dry_run: bool = False,
) -> ReplayStats:
    """Fetch every frame in this camera's published window that the archive is missing."""
    stats = ReplayStats(terminal=terminal, camera=camera.id)
    if not camera.has_replay:
        stats.errors.append(f"camera {camera.id!r} has no replay feed configured")
        return stats

    index = fetch(
        camera.replay_index_url,
        user_agent=config.capture.user_agent,
        timeout=config.capture.timeout_seconds,
        max_retries=config.capture.max_retries,
    )
    if not index.ok or index.text is None:
        stats.errors.append(f"replay index: {index.error or 'empty response'}")
        return stats

    try:
        moments = parse_index(index.text, camera)
    except ReplayError as exc:
        stats.errors.append(str(exc))
        return stats

    stats.offered = len(moments)
    held = _held(conn, terminal, camera.id, moments)
    wanted = [m for m in moments if iso(m) not in held]
    stats.already_held = stats.offered - len(wanted)

    if len(wanted) > limit:
        # Keep the newest, which are the ones a background rebuild and any read of "how is
        # it right now" both want first. The rest stay missing and the next sweep collects
        # them — assuming it runs before the window rolls past them, which is why the number
        # dropped is reported rather than quietly absorbed.
        stats.capped = len(wanted) - limit
        wanted = wanted[-limit:]

    if dry_run:
        return stats

    for moment in wanted:
        url = camera.replay_frame_url.format(
            timestamp=moment.strftime(camera.replay_timestamp_format)
        )
        result = fetch(
            url,
            user_agent=config.capture.user_agent,
            timeout=config.capture.timeout_seconds,
            max_retries=config.capture.max_retries,
        )
        _clear_failed(conn, terminal, camera.id, moment)
        outcome = store_frame(conn, config, terminal, moment, result, camera.id)
        if outcome.ok:
            stats.fetched += 1
        else:
            stats.failed += 1
            if outcome.error and len(stats.errors) < 5:
                stats.errors.append(f"{moment.isoformat()}: {outcome.error}")
    return stats


def sweep(
    conn: sqlite3.Connection,
    config: Config,
    *,
    terminals: list[str] | None = None,
    limit: int = MAX_FRAMES_PER_SWEEP,
    dry_run: bool = False,
) -> list[ReplayStats]:
    """Sweep every camera on the route that publishes a replay window."""
    wanted = set(terminals) if terminals else None
    out: list[ReplayStats] = []
    with JobRun(conn, "replay") as run:
        for terminal in config.route.terminals:
            if wanted is not None and terminal.code not in wanted:
                continue
            for camera in terminal.replay_cameras:
                run.attempted += 1
                stats = sweep_camera(
                    conn, config, terminal.code, camera, limit=limit, dry_run=dry_run
                )
                # A sweep that found nothing missing has succeeded, not failed. Requiring a
                # fetch would mark every run after the first as a failure and bury the
                # signal `ferrycast health` exists to raise.
                if not stats.errors and stats.failed == 0:
                    run.succeeded += 1
                out.append(stats)
    return out
