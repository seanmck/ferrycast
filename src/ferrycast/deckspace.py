"""R2 — BC Ferries deck-space scraper.

Deck space is the published *current* number and explicitly excludes vehicles still queued
outside the terminal, which is exactly why FerryCast exists. It is still worth recording:
it anchors sailing boundaries and it keeps flowing when the cameras are dark.

The parser works on the page's visible text rather than its DOM structure, so a CSS or
markup reshuffle degrades to `unparsed` instead of throwing. Per R2, a scrape failure must
not disturb the webcam pipeline.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .db import JobRun
from .fetching import fetch
from .timeutil import iso, local, now_utc

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
FULL_WORDS = re.compile(r"\bfull\b", re.IGNORECASE)
AVAILABLE_WORDS = re.compile(r"\bavailable|\bspace\b|\bremaining\b", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)


@dataclass
class DeckSpaceRow:
    sailing_hhmm: str | None
    percent_available: int | None
    vessel: str | None = None
    status_text: str | None = None


def visible_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a declared dependency
        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"[ \t\xa0]+", " ", soup.get_text("\n"))


def _to_24h(hour: int, minute: int, meridiem: str | None) -> str | None:
    if not 0 <= minute <= 59:
        return None
    if meridiem:
        marker = meridiem.replace(".", "").lower()
        if not 1 <= hour <= 12:
            return None
        if marker == "pm" and hour != 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_deck_space(html: str) -> list[DeckSpaceRow]:
    """Pull (sailing time, % deck space available) pairs out of a conditions page.

    BC Ferries has published this figure both ways over the years ("45% full" and
    "45% available"), so the surrounding wording decides the polarity; when the page says
    neither, we assume the number is space *available*, which is the current wording.
    """
    text = visible_text(html)
    times = list(TIME_RE.finditer(text))
    rows: list[DeckSpaceRow] = []

    for index, match in enumerate(times):
        hhmm = _to_24h(int(match.group(1)), int(match.group(2)), match.group(3))
        if hhmm is None:
            continue
        end = times[index + 1].start() if index + 1 < len(times) else len(text)
        segment = text[match.end() : end]

        if CANCELLED_RE.search(segment):
            rows.append(DeckSpaceRow(hhmm, None, status_text="cancelled"))
            continue

        percent_match = PERCENT_RE.search(segment)
        if not percent_match:
            continue
        value = int(percent_match.group(1))
        if value > 100:
            continue

        window = segment[: percent_match.end() + 40]
        if FULL_WORDS.search(window) and not AVAILABLE_WORDS.search(window):
            available = 100 - value
        else:
            available = value
        rows.append(DeckSpaceRow(hhmm, available, status_text=_snippet(segment)))

    return _dedupe(rows)


def _snippet(segment: str, limit: int = 80) -> str:
    return " ".join(segment.split())[:limit] or None


def _dedupe(rows: list[DeckSpaceRow]) -> list[DeckSpaceRow]:
    seen: set[str | None] = set()
    unique: list[DeckSpaceRow] = []
    for row in rows:
        if row.sailing_hhmm in seen:
            continue
        seen.add(row.sailing_hhmm)
        unique.append(row)
    return unique


def store_rows(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    observed_at: datetime,
    rows: list[DeckSpaceRow],
) -> int:
    service_date = local(observed_at, config.tz).date().isoformat()
    stored = 0
    for row in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    percent_available, vessel, status_text, fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok')""",
            (
                config.route.id,
                terminal,
                iso(observed_at),
                service_date,
                row.sailing_hhmm,
                row.percent_available,
                row.vessel,
                row.status_text,
            ),
        )
        stored += cur.rowcount or 0
    conn.commit()
    return stored


def _record_problem(
    conn: sqlite3.Connection,
    config: Config,
    terminal: str,
    observed_at: datetime,
    status: str,
    error: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                fetch_status, error)
           VALUES (?, ?, ?, ?, NULL, ?, ?)""",
        (
            config.route.id,
            terminal,
            iso(observed_at),
            local(observed_at, config.tz).date().isoformat(),
            status,
            error,
        ),
    )
    conn.commit()


def scrape_once(conn: sqlite3.Connection, config: Config) -> list[dict]:
    observed_at = now_utc()
    results: list[dict] = []
    with JobRun(conn, "deckspace") as run:
        for terminal in config.route.terminals:
            if not terminal.deck_space_url:
                results.append(
                    {"terminal": terminal.code, "ok": False, "skipped": True, "rows": 0}
                )
                continue
            run.attempted += 1
            result = fetch(
                terminal.deck_space_url,
                user_agent=config.capture.user_agent,
                timeout=config.capture.timeout_seconds,
                max_retries=config.capture.max_retries,
            )
            if not result.ok or not result.text:
                _record_problem(
                    conn,
                    config,
                    terminal.code,
                    observed_at,
                    "error",
                    result.error or "empty response",
                )
                results.append(
                    {
                        "terminal": terminal.code,
                        "ok": False,
                        "rows": 0,
                        "error": result.error or "empty response",
                    }
                )
                continue

            rows = parse_deck_space(result.text)
            if not rows:
                _record_problem(
                    conn,
                    config,
                    terminal.code,
                    observed_at,
                    "unparsed",
                    "page fetched but no sailing/deck-space pairs recognised",
                )
                results.append(
                    {
                        "terminal": terminal.code,
                        "ok": False,
                        "rows": 0,
                        "error": "page format not recognised",
                    }
                )
                continue

            stored = store_rows(conn, config, terminal.code, observed_at, rows)
            run.succeeded += 1
            results.append({"terminal": terminal.code, "ok": True, "rows": stored})
    return results
