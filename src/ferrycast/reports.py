"""Manual sailing reports — first-hand accounts from the line.

Deck space says how much room was left aboard the vessel; camera frames count the vehicles
waiting outside the terminal. Neither of them knows whether *you* got on. Somebody sitting
in that queue does, and it costs nothing to ask them.

So a report is deliberately small: when you joined the line, when the ferry actually left,
whether you got on, and how full the deck looked. Four facts a traveller knows without
looking anything up, and the first two are optional because a half-remembered trip is still
worth more than no record at all.

A report outranks both automatic sources when it comes to the outcome, because it is the
only direct observation of the thing the whole app is about. It is *not* allowed to move
the "arrive before" time, though — see `outcome_from_reports` for why that would flatter
the answer in the dangerous direction.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

from .config import Config
from .timeutil import combine_local, iso, local, now_utc, parse_hhmm, parse_iso

# An am/pm flip on a hand-typed departure: a 12:30 sailing reported as leaving at 00:30.
MAX_DEPARTURE_DRIFT_HOURS = 3

# How full the deck looked as the vessel left. Four steps rather than a percentage: nobody
# standing on a car deck can tell 60% from 70%, and a spurious number would be treated as
# though it were measured.
DECK_FULLNESS = {
    "room": "Plenty of room",
    "half": "About half full",
    "nearly_full": "Nearly full",
    "full": "Full — no room left",
}

# Sanity bounds on the two times. They exist to catch a mistyped or am/pm-flipped entry,
# not to police how early someone is willing to queue. Both clock times are read on the
# service date, so a route that sails past midnight would need this revisiting; this one
# does not have such a sailing, and a report that crossed midnight is refused rather than
# quietly filed against the wrong day.
MAX_QUEUE_HOURS = 12

# One sailing does not need a hundred accounts, and the form is unauthenticated.
MAX_REPORTS_PER_SAILING = 50

# How the report reached us. The distinction that matters is `checkin`: the join time was
# stamped while the person was standing in the queue rather than recalled an hour later.
# It is the same claim either way — a time somebody says they arrived — so it does not get
# its own column or outrank anything. It is recorded because an observed join time is
# better evidence than a remembered one, and there is no way to tell them apart afterwards
# if nobody writes it down now.
REPORT_SOURCES = frozenset({"web", "checkin"})


class ReportError(ValueError):
    """A report that cannot be stored as given. The message is shown to the submitter."""


def parse_clock(value: str | None) -> time | None:
    """Parse an optional HH:MM from a form field.

    `<input type="time">` sends HH:MM, but several browsers append seconds once the field
    has been touched with the keyboard, so anything past the minute is ignored rather than
    rejected.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        raise ReportError(f"{text!r} is not a time; expected HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        return time(hour, minute)
    except ValueError as exc:
        raise ReportError(f"{text!r} is not a time; expected HH:MM") from exc


def submit_report(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    service_date: date,
    depart_hhmm: str,
    boarded: bool,
    joined: time | None = None,
    departed: time | None = None,
    sailings_waited: int | None = None,
    deck_fullness: str | None = None,
    source: str = "web",
    now: datetime | None = None,
) -> int:
    """Validate and store one report, then re-derive the sailing it describes.

    Aggregation is otherwise a nightly job, so without the recompute a submitter would send
    their account and watch the page carry on saying the opposite. Re-running the day is
    idempotent, which is the whole reason R4 was built that way.
    """
    from .aggregate import aggregate_day
    from .query import sailing_times

    route = config.route
    if origin not in route.codes:
        raise ReportError(f"unknown terminal {origin!r}")

    times = sailing_times(config, origin, service_date)
    if depart_hhmm not in times:
        raise ReportError(
            f"the timetable has no {depart_hhmm} sailing from {origin} on {service_date}"
        )

    if deck_fullness and deck_fullness not in DECK_FULLNESS:
        raise ReportError(f"unknown deck fullness {deck_fullness!r}")

    # The web form carries this in a hidden field, so it is submitter-controlled like
    # everything else here and gets checked like everything else here.
    if source not in REPORT_SOURCES:
        raise ReportError(f"unknown report source {source!r}")

    scheduled = combine_local(service_date, parse_hhmm(depart_hhmm), config.tz)
    reference = local(now or now_utc(), config.tz)
    if scheduled > reference:
        raise ReportError("that sailing has not departed yet")

    # The board publishes the actual time to the minute ("9:25 am Departed 9:56 am"), so
    # the form pre-fills from it and nobody has to remember. But the board only carries
    # *today's* sailings — report on last Friday and there is nothing to read — so a typed
    # value is still accepted and still wins. Board first, person second, scheduled last.
    from .aggregate import _board_departure

    departed_at = combine_local(service_date, departed, config.tz) if departed else None
    if departed_at is not None:
        drift = abs((departed_at - scheduled).total_seconds()) / 3600
        if drift > MAX_DEPARTURE_DRIFT_HOURS:
            raise ReportError(
                f"a departure at {departed.strftime('%H:%M')} is too far from the scheduled "
                f"{depart_hhmm} to be the same sailing"
            )
    else:
        departed_at = _board_departure(
            conn, route.id, origin, service_date.isoformat(), depart_hhmm, config
        )

    if sailings_waited is not None:
        if boarded:
            raise ReportError("you cannot have waited for sailings you were on")
        if sailings_waited not in (1, 2):
            raise ReportError("say whether you waited one sailing or two or more")

    joined_at = combine_local(service_date, joined, config.tz) if joined else None
    if joined_at is not None:
        last_moment = departed_at or scheduled
        if joined_at > last_moment:
            raise ReportError("you cannot have joined the line after the ferry left")
        if last_moment - joined_at > timedelta(hours=MAX_QUEUE_HOURS):
            raise ReportError(
                f"joining more than {MAX_QUEUE_HOURS} hours before departure looks like a typo"
            )

    existing = conn.execute(
        """SELECT COUNT(*) FROM sailing_reports
            WHERE route = ? AND origin = ? AND service_date = ? AND depart_hhmm = ?""",
        (route.id, origin, service_date.isoformat(), depart_hhmm),
    ).fetchone()[0]
    if existing >= MAX_REPORTS_PER_SAILING:
        raise ReportError(
            f"this sailing already has {existing} reports, which is as many as it takes"
        )

    cur = conn.execute(
        """INSERT INTO sailing_reports
               (route, origin, service_date, depart_hhmm, joined_queue_at, departed_at,
                sailings_waited,
                boarded, deck_fullness, source, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            route.id,
            origin,
            service_date.isoformat(),
            depart_hhmm,
            iso(joined_at) if joined_at else None,
            iso(departed_at) if departed_at else None,
            sailings_waited,
            int(boarded),
            deck_fullness or None,
            source,
            iso(now or now_utc()),
        ),
    )
    conn.commit()

    aggregate_day(conn, config, service_date)
    return int(cur.lastrowid)


def fetch_reports(
    conn: sqlite3.Connection,
    route_id: str,
    origin: str,
    service_date: str,
    depart_hhmm: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT * FROM sailing_reports
                WHERE route = ? AND origin = ? AND service_date = ? AND depart_hhmm = ?
                ORDER BY submitted_at""",
            (route_id, origin, service_date, depart_hhmm),
        ).fetchall()
    )


def outcome_from_reports(rows: list[sqlite3.Row]) -> str | None:
    """The outcome a set of reports supports, or None if there are none.

    One person left behind is proof the sailing ran out of room; several people getting on
    is not proof that nobody was turned away later. So a single "no" outweighs any number
    of "yes"es.

    How long they then waited is a separate fact, and the form asks for it — without that
    question a report could only ever produce `filled`, so the distribution could never
    contain waited_1 or waited_2plus from the one source that actually knows. Unanswered it
    stays `filled`: "did not get on" is certain, "waited exactly one sailing" is not.

    The longest wait reported wins, for the same reason the single "no" does — somebody who
    waited two sailings was not on the one the person who waited one caught.
    """
    if not rows:
        return None
    missed = [row for row in rows if not row["boarded"]]
    if not missed:
        return "boarded"
    waits = [
        row["sailings_waited"]
        for row in missed
        if "sailings_waited" in row.keys() and row["sailings_waited"]
    ]
    if not waits:
        return "filled"
    return "waited_2plus" if max(waits) >= 2 else "waited_1"


def report_confidence(rows: list[sqlite3.Row]) -> float:
    """High, but never 1.0 — a person can misremember which sailing they were on."""
    return round(min(0.95, 0.8 + 0.05 * len(rows)), 2)


def describe_reports(
    config: Config,
    rows: list[sqlite3.Row],
    scheduled_departure: datetime,
) -> dict | None:
    """Render a sailing's reports for the page.

    The two bounds are the interesting part. Somebody who joined at 11:20 and did not get
    on establishes that the cutoff was *before* 11:20; somebody who joined at 11:05 and did
    get on establishes there was still room *at* 11:05. Those are the honest claims, and
    they are the reason a join time is worth collecting at all, even though it deliberately
    never becomes the record's `filled_at`.
    """
    if not rows:
        return None

    def clock(value: str | None) -> str | None:
        return local(parse_iso(value), config.tz).strftime("%H:%M") if value else None

    entries = []
    for row in rows:
        departed_at = parse_iso(row["departed_at"]) if row["departed_at"] else None
        entries.append(
            {
                "boarded": bool(row["boarded"]),
                "joined": clock(row["joined_queue_at"]),
                "departed": clock(row["departed_at"]),
                "minutes_late": (
                    int((departed_at - scheduled_departure).total_seconds() // 60)
                    if departed_at
                    else None
                ),
                "fullness": DECK_FULLNESS.get(row["deck_fullness"] or ""),
                "submitted": clock(row["submitted_at"]),
            }
        )

    left_behind = [e for e in entries if not e["boarded"]]
    got_on = [e for e in entries if e["boarded"]]
    cutoff = sorted(e["joined"] for e in left_behind if e["joined"])
    still_room = sorted(e["joined"] for e in got_on if e["joined"])
    late = [e["minutes_late"] for e in entries if e["minutes_late"] is not None]

    return {
        "n": len(entries),
        "entries": entries,
        "boarded": len(got_on),
        "left_behind": len(left_behind),
        # The strongest bound in each direction.
        "cutoff_before": cutoff[0] if cutoff else None,
        "still_room_at": still_room[-1] if still_room else None,
        "typical_minutes_late": sorted(late)[len(late) // 2] if late else None,
        "outcome": outcome_from_reports(rows),
    }
