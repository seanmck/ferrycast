"""R1 — frame capture.

Pulls one frame per terminal per run, stores it on disk and records the attempt. A camera
that is down produces an `error` row rather than an exception, so a bad camera never stops
the other one and never breaks the cron job.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import JobRun
from .fetching import FetchResult, fetch
from .timeutil import iso, local, now_utc

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


@dataclass
class CaptureOutcome:
    terminal: str
    ok: bool
    frame_id: int | None = None
    path: Path | None = None
    error: str | None = None
    skipped: bool = False


def sniff_extension(content: bytes) -> str | None:
    for signature, ext in IMAGE_SIGNATURES:
        if content.startswith(signature):
            if ext == "webp" and content[8:12] != b"WEBP":
                continue
            return ext
    return None


def image_dimensions(content: bytes) -> tuple[int | None, int | None]:
    """Best-effort dimensions. Pillow is optional at runtime; capture must not depend on it."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(content)) as img:
            return img.width, img.height
    except Exception:
        return None, None


def frame_path(config: Config, terminal: str, captured_at: datetime, ext: str) -> Path:
    when = local(captured_at, config.tz)
    return (
        config.frames_dir
        / terminal
        / when.strftime("%Y")
        / when.strftime("%m")
        / when.strftime("%d")
        / f"{terminal}_{when.strftime('%Y%m%dT%H%M%S')}.{ext}"
    )


def _record_failure(
    conn: sqlite3.Connection, config: Config, terminal: str, captured_at: datetime, error: str
) -> CaptureOutcome:
    conn.execute(
        """INSERT OR IGNORE INTO frames
               (route, terminal, captured_at, service_date, status, error)
           VALUES (?, ?, ?, ?, 'error', ?)""",
        (
            config.route.id,
            terminal,
            iso(captured_at),
            local(captured_at, config.tz).date().isoformat(),
            error,
        ),
    )
    conn.commit()
    return CaptureOutcome(terminal=terminal, ok=False, error=error)


def store_frame(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    captured_at: datetime,
    result: FetchResult,
) -> CaptureOutcome:
    if not result.ok or not result.content:
        return _record_failure(
            conn, config, terminal, captured_at, result.error or "empty response"
        )

    ext = sniff_extension(result.content)
    if ext is None:
        # A webcam URL that returns an HTML page is the classic misconfiguration; say so
        # rather than silently archiving a page of markup as if it were a frame.
        return _record_failure(
            conn,
            config,
            terminal,
            captured_at,
            f"response was not an image (content-type={result.content_type or 'unknown'})",
        )

    path = frame_path(config, terminal, captured_at, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.content)
    width, height = image_dimensions(result.content)

    cur = conn.execute(
        """INSERT OR IGNORE INTO frames
               (route, terminal, captured_at, service_date, path, sha256, bytes,
                width, height, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(captured_at),
            local(captured_at, config.tz).date().isoformat(),
            str(path.relative_to(config.data_dir)),
            hashlib.sha256(result.content).hexdigest(),
            len(result.content),
            width,
            height,

        ),
    )
    conn.commit()
    frame_id = cur.lastrowid if cur.rowcount else None
    return CaptureOutcome(terminal=terminal, ok=True, frame_id=frame_id, path=path)


def capture_once(
    conn: sqlite3.Connection,
    config: Config,
    *,
    terminals: list[str] | None = None,
    at: datetime | None = None,
) -> list[CaptureOutcome]:
    """Capture one frame per configured camera, or just the terminals named.

    Targeting a single terminal is what an on-demand status check uses — there is no point
    waking the far-side camera to answer "how is the queue where I'm standing".
    """
    captured_at = at or now_utc()
    outcomes: list[CaptureOutcome] = []
    wanted = set(terminals) if terminals else None
    with JobRun(conn, "capture") as run:
        for terminal in config.route.terminals:
            if wanted is not None and terminal.code not in wanted:
                continue
            if not terminal.configured_for_capture:
                outcomes.append(
                    CaptureOutcome(
                        terminal=terminal.code,
                        ok=False,
                        skipped=True,
                        error="no webcam_url configured",
                    )
                )
                continue
            run.attempted += 1
            result = fetch(
                terminal.webcam_url,
                user_agent=config.capture.user_agent,
                timeout=config.capture.timeout_seconds,
                max_retries=config.capture.max_retries,
            )
            outcome = store_frame(conn, config, terminal.code, captured_at, result)
            if outcome.ok:
                run.succeeded += 1
            outcomes.append(outcome)
    return outcomes
