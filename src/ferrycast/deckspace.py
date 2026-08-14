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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Config
from .db import JobRun
from .fetching import fetch
from .timeutil import iso, local, now_utc

TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE)
PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
FULL_WORDS = re.compile(r"\bfull\b", re.IGNORECASE)
AVAILABLE_WORDS = re.compile(r"\bavailable|\bspace\b|\bremaining\b", re.IGNORECASE)
CANCELLED_RE = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
# A sailing's status is the FIRST status word after its time. Searching the segment for
# "cancelled" anywhere marked the last sailing of the day cancelled, because the site's
# footer carries a "Cancelled Sailings" link — the status that matters is "Upcoming",
# which appears first.
STATUS_RE = re.compile(r"\b(cancell?ed|departed|upcoming|arrived|delayed)\b", re.IGNORECASE)
# "9:25 am Departed 9:56 am" — the actual departure, which is the only source of one at a
# terminal whose camera does not face the berth. Anchored on the word so the arrival time
# on the next line ("Arrived: 10:41 am") cannot be mistaken for it.
DEPARTED_RE = re.compile(
    r"\bdeparted\b[^\d]{0,12}(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE
)

# How far past a sailing time its own status can run. Without a cap the final entry's
# segment swallows the page footer, and the site's "Cancelled Sailings" link with it.
SEGMENT_LIMIT = 400

# Where the per-sailing text ends and the page's own chrome begins.
FOOTER_RE = re.compile(r"Last updated:|Refresh details|Ferry tracking", re.IGNORECASE)

# The operator's own words for "this sailing filled". Deliberately a phrase match on
# published wording rather than an inference: it says how they loaded, which is a different
# claim from "somebody was turned away", and only the second is an overload.
FULL_NOTE_RE = re.compile(r"loading maximum number of vehicles", re.IGNORECASE)

# How much of an unrecognised page to keep for diagnosis. Enough to see the structure,
# bounded so a redesigned site cannot fill the volume one scrape at a time.
RAW_LIMIT = 4000


@dataclass
class DeckSpaceRow:
    sailing_hhmm: str | None
    percent_available: int | None
    vessel: str | None = None
    status_text: str | None = None
    departed_hhmm: str | None = None


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


def parse_deck_space(html: str, expected: Sequence[str] | None = None) -> list[DeckSpaceRow]:
    """Pull per-sailing status out of a conditions page.

    **Anchored on the timetable.** Every `HH:MM` on the page used to be treated as a
    sailing, which is how a live install spent a day recording the page's own
    "Last updated: 12:20 pm" stamp as a cancelled 12:20 sailing — at both terminals, every
    fifteen minutes, with no deck-space figure ever captured. Times that are not scheduled
    departures are now discarded, so junk fails loudly (nothing parsed) instead of quietly
    (plausible nonsense). Passing `expected=None` keeps the old permissive behaviour and is
    only for tests.

    What the page carries for a first-come-first-served route is a departures board —
    scheduled time, Departed/Upcoming, and an operator note like "Peak travel. Loading
    maximum number of vehicles". The note is recorded even when no percentage exists,
    because it is the only per-sailing text this page has.

    It is **not** evidence of overload, and must not be treated as such. It states a cause
    the operator chose to publish, not a measurement of the queue: loading to capacity does
    not mean the lot failed to clear, and a late departure could be weather or crewing. Only
    a percentage reaching zero, a counted queue, or a human report establishes that a
    vehicle was turned away. Some routes do publish a percentage, worded both ways over the
    years ("45% full" and "45% available"), so the surrounding wording decides the polarity.
    """
    text = visible_text(html)
    accepted: list[tuple[re.Match, str]] = []
    for match in TIME_RE.finditer(text):
        hhmm = _to_24h(int(match.group(1)), int(match.group(2)), match.group(3))
        if hhmm is None:
            continue
        if expected is not None and hhmm not in expected:
            continue
        accepted.append((match, hhmm))

    rows: list[DeckSpaceRow] = []
    for index, (match, hhmm) in enumerate(accepted):
        # Run to the next *scheduled* sailing, not the next time on the page: an entry's
        # status sits between its own time and the following one, interrupted by actual
        # departure and arrival times that are not sailings.
        end = accepted[index + 1][0].start() if index + 1 < len(accepted) else len(text)
        segment = text[match.end() : min(end, match.end() + SEGMENT_LIMIT)]
        # The last sailing has no following time to stop at, so its segment runs on into the
        # page furniture. Cut at the footer marker: chrome is not a note about a sailing, and
        # a phrase match over it would eventually find something it should not.
        footer = FOOTER_RE.search(segment)
        if footer:
            segment = segment[: footer.start()]

        status = STATUS_RE.search(segment)
        if status is not None and CANCELLED_RE.fullmatch(status.group(1)):
            rows.append(DeckSpaceRow(hhmm, None, status_text="cancelled"))
            continue

        percent_match = PERCENT_RE.search(segment)
        available = None
        if percent_match:
            value = int(percent_match.group(1))
            if value <= 100:
                window = segment[: percent_match.end() + 40]
                if FULL_WORDS.search(window) and not AVAILABLE_WORDS.search(window):
                    available = 100 - value
                else:
                    available = value

        departed = DEPARTED_RE.search(segment)
        departed_hhmm = (
            _to_24h(int(departed.group(1)), int(departed.group(2)), departed.group(3))
            if departed
            else None
        )

        note = _snippet(segment)
        if available is None and not note:
            continue
        rows.append(
            DeckSpaceRow(hhmm, available, status_text=note, departed_hhmm=departed_hhmm)
        )

    return _dedupe(rows)


def _snippet(segment: str, limit: int = 200) -> str:
    """The per-sailing text, kept whole enough to be searchable.

    Was 80, which cut "Peak travel. Loading maximum number of vehicles" to "...maximum num"
    — present in the database but matching no query anyone would write, which is worse than
    absent because it looks like the page never said it.
    """
    return " ".join(segment.split())[:limit] or None


def notice_says_full(status_text: str | None) -> bool:
    """Whether the board reported this sailing as loading to capacity.

    Not an overload on its own — see `parse_deck_space`. It describes the deck, which no
    camera can see; whether anyone was left behind is about the compound, which no board can
    see. The two together are worth more than either, and neither substitutes for the other.
    """
    return bool(status_text and FULL_NOTE_RE.search(status_text))


def status_says_departed(status_text: str | None) -> bool:
    """Whether the board has marked this sailing as having left.

    A fallback for rows stored before the `departed_hhmm` column existed: the phrase was
    always in `status_text`, it just was not parsed out. New rows should be answered from
    the column.
    """
    return bool(status_text and DEPARTED_RE.search(status_text))


def _merge(first: DeckSpaceRow, second: DeckSpaceRow) -> DeckSpaceRow:
    """Combine two views of one sailing, preferring whichever actually said something."""
    return DeckSpaceRow(
        sailing_hhmm=first.sailing_hhmm,
        percent_available=(
            first.percent_available if first.percent_available is not None
            else second.percent_available
        ),
        vessel=first.vessel or second.vessel,
        status_text=max(
            (first.status_text or ""), (second.status_text or ""), key=len
        ) or None,
        departed_hhmm=first.departed_hhmm or second.departed_hhmm,
    )


def _dedupe(rows: list[DeckSpaceRow]) -> list[DeckSpaceRow]:
    """One row per sailing, merged rather than first-wins.

    The board prints each sailing twice: a terse line, then a fuller one carrying the
    arrival time and the operator's note. Keeping the first and discarding the rest threw
    the fuller one away every time — which is why, across 2368 stored rows, not one carried
    "Loading maximum number of vehicles" despite the page publishing it several times a day.

    They are two views of the same sailing in the same scrape, not competing claims, so they
    are merged: any field one of them filled in is kept.
    """
    order: list[str | None] = []
    merged: dict[str | None, DeckSpaceRow] = {}
    for row in rows:
        key = row.sailing_hhmm
        if key in merged:
            merged[key] = _merge(merged[key], row)
        else:
            order.append(key)
            merged[key] = row
    return [merged[key] for key in order]


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
                    percent_available, vessel, status_text, departed_hhmm, fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')""",
            (
                config.route.id,
                terminal,
                iso(observed_at),
                service_date,
                row.sailing_hhmm,
                row.percent_available,
                row.vessel,
                row.status_text,
                row.departed_hhmm,
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
    raw: str | None = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                fetch_status, error, raw)
           VALUES (?, ?, ?, ?, NULL, ?, ?, ?)""",
        (
            config.route.id,
            terminal,
            iso(observed_at),
            local(observed_at, config.tz).date().isoformat(),
            status,
            error,
            raw,
        ),
    )
    conn.commit()


def expected_times(config: Config, terminal: str, at: datetime) -> set[str]:
    """Scheduled departures this terminal could plausibly be showing right now.

    Yesterday and tomorrow are included because the board spans midnight: a 22:00 sailing
    is still listed after it has departed, and tomorrow's first sailing appears before it.
    """
    from .schedule import load_schedule_cached, sailings_for_day

    blocks = load_schedule_cached(config.schedule_path)
    day = local(at, config.tz).date()
    times: set[str] = set()
    for offset in (-1, 0, 1):
        for sailing in sailings_for_day(
            blocks,
            day + timedelta(days=offset),
            config.route.id,
            config.route.destinations,
            config.tz,
            origin=terminal,
        ):
            times.add(sailing.depart_hhmm)
    return times


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

            rows = parse_deck_space(result.text, expected_times(config, terminal.code, observed_at))
            if not rows:
                # Keep the page itself. "Fetched fine, recognised nothing" is the failure
                # that hides best — it looks identical to a quiet day — and diagnosing it
                # without the markup meant a day of guessing from a screenshot.
                _record_problem(
                    conn,
                    config,
                    terminal.code,
                    observed_at,
                    "unparsed",
                    "page fetched but no scheduled sailing recognised",
                    raw=visible_text(result.text)[:RAW_LIMIT],
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
