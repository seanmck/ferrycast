"""On-demand history: pay for a slot only when someone asks about it.

The property that makes this safe is that it spends money. Every test here is really about
one of three things: that it reads the *right* sailings, that it reads as *few* frames as
possible, and that it cannot be made to read more than it was allowed to.
"""

from datetime import date, timedelta

import pytest

from ferrycast.backfill import (
    fill_for_slot,
    fillable_sailings,
)
from ferrycast.schedule import day_type, season
from ferrycast.timeutil import combine_local, iso, parse_hhmm

from .test_query import seed_record


class FakeClient:
    """Counts calls. The real client would cost money and need a network."""

    def __init__(self):
        self.calls = 0


def add_sailing(conn, config, service_date: date, hhmm: str, origin="SLT", outcome=None):
    departure = combine_local(service_date, parse_hhmm(hhmm), config.tz)
    conn.execute(
        """INSERT OR IGNORE INTO sailings
               (route, origin, destination, service_date, scheduled_departure,
                depart_hhmm, day_type, season)
           VALUES (?, ?, 'ERL', ?, ?, ?, ?, ?)""",
        (
            config.route.id,
            origin,
            service_date.isoformat(),
            departure.isoformat(),
            hhmm,
            day_type(service_date),
            season(service_date),
        ),
    )
    sailing_id = conn.execute(
        "SELECT id FROM sailings WHERE origin = ? AND scheduled_departure = ?",
        (origin, departure.isoformat()),
    ).fetchone()["id"]
    if outcome:
        conn.execute(
            """INSERT OR REPLACE INTO sailing_records
                   (sailing_id, outcome, n_frames, computed_at)
               VALUES (?, ?, 4, '2026-01-01T00:00:00Z')""",
            (sailing_id, outcome),
        )
    conn.commit()
    return sailing_id


def add_frames(conn, config, service_date: date, hhmm: str, origin="SLT", every=5):
    """Frames across a sailing's whole window, as capture would have left them."""
    departure = combine_local(service_date, parse_hhmm(hhmm), config.tz)
    for offset in range(-60, 46, every):
        captured = departure + timedelta(minutes=offset)
        conn.execute(
            """INSERT OR IGNORE INTO frames
                   (route, terminal, captured_at, service_date, path, status)
               VALUES (?, ?, ?, ?, ?, 'ok')""",
            (
                config.route.id,
                origin,
                iso(captured),
                service_date.isoformat(),
                f"frames/{origin}/{captured:%Y%m%dT%H%M%S}.jpg",
            ),
        )
    conn.commit()


# Mondays in the peak-summer bucket, so they share day_type and season with the target.
# NB 2026-08-03 is BC Day, which buckets as sunday_holiday rather than weekday — using it
# as the target here would silently compare against the wrong day type.
MONDAYS = [date(2026, 7, 6), date(2026, 7, 13), date(2026, 7, 20), date(2026, 7, 27)]
TARGET = date(2026, 8, 17)


def test_only_comparable_sailings_are_candidates(conn, config):
    """Spending on a sailing that would not answer the question is spending on nothing."""
    for day in MONDAYS:
        add_sailing(conn, config, day, "12:30")
    add_sailing(conn, config, date(2026, 7, 4), "12:30")  # a Saturday
    add_sailing(conn, config, date(2026, 7, 6), "08:30")  # too far from 12:30

    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert {c.service_date for c in found} == {d.isoformat() for d in MONDAYS}


def test_sailings_that_already_have_a_record_are_skipped(conn, config):
    """Extraction is idempotent, so the second person to ask pays nothing."""
    for day in MONDAYS:
        add_sailing(conn, config, day, "12:30")
    seed_record(conn, config, MONDAYS[0], "12:30", "boarded")

    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert MONDAYS[0].isoformat() not in {c.service_date for c in found}


def test_unknown_records_are_still_fillable(conn, config):
    """`unknown` means the evidence was never read, not that it was read and inconclusive."""
    add_sailing(conn, config, MONDAYS[0], "12:30", outcome="unknown")
    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert [c.service_date for c in found] == [MONDAYS[0].isoformat()]


def test_future_sailings_are_never_candidates(conn, config):
    """Today's 12:30 has not happened. Reading its frames would answer nothing."""
    add_sailing(conn, config, TARGET, "12:30")
    add_sailing(conn, config, date(2026, 8, 24), "12:30")
    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert found == []


def test_the_newest_sailings_are_read_first(conn, config):
    """If the budget stops the run partway, the sailings paid for should be the better ones."""
    for day in MONDAYS:
        add_sailing(conn, config, day, "12:30")
    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert [c.service_date for c in found] == [d.isoformat() for d in reversed(MONDAYS)]


def test_the_candidate_limit_is_respected(conn, config):
    for day in MONDAYS:
        add_sailing(conn, config, day, "12:30")
    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", limit=2, now=TARGET
    )
    assert len(found) == 2


def test_only_the_essential_frames_are_read(conn, config, monkeypatch):
    """22 frames sit in the window; the four configured offsets decide the outcome.

    This is the difference between ~$0.005 and ~$0.03 for one sailing, every time.
    """
    add_sailing(conn, config, MONDAYS[0], "12:30")
    add_frames(conn, config, MONDAYS[0], "12:30")

    read = []

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        read.extend(frames)
        stats = ExtractionStats()
        stats.extracted = len(frames)
        return stats

    from ferrycast import backfill

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)
    result = backfill.fill_for_slot(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )

    total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    assert total > 10, "expected a full window of frames to choose from"
    assert len(read) == len(config.vision.essential_offsets_minutes)
    assert result.filled == 1


def test_a_sailing_with_no_archived_frames_costs_nothing(conn, config, monkeypatch):
    """Days before capture was running. Discovering that must not need a model call."""
    add_sailing(conn, config, MONDAYS[0], "12:30")  # no frames

    calls = []

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        calls.append(frames)
        return ExtractionStats()

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)
    result = fill_for_slot(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )

    assert calls == []
    assert result.skipped_no_frames == 1
    assert result.cost_usd == 0.0


def test_a_dry_run_spends_nothing(conn, config, monkeypatch):
    add_sailing(conn, config, MONDAYS[0], "12:30")
    add_frames(conn, config, MONDAYS[0], "12:30")

    result = fill_for_slot(
        conn,
        config,
        origin="SLT",
        target_date=TARGET,
        depart_hhmm="12:30",
        dry_run=True,
        now=TARGET,
    )
    assert result.cost_usd == 0.0
    assert result.frames_read == 0


def test_the_run_stops_when_the_budget_is_reached(conn, config, monkeypatch):
    """A slot nobody has asked about before must not be able to spend the month's budget."""
    for day in MONDAYS:
        add_sailing(conn, config, day, "12:30")
        add_frames(conn, config, day, "12:30")

    calls = []

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        calls.append(frames)
        stats = ExtractionStats()
        stats.extracted = len(frames)
        stats.budget_stopped = len(calls) >= 2
        return stats

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)
    result = fill_for_slot(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )

    assert len(calls) == 2, "should stop at the first budget signal, not finish the list"
    assert result.budget_stopped is True


def test_another_route_is_never_filled(conn, config):
    """Same guarantee as the distribution: spending is scoped to the active route."""
    conn.execute(
        """INSERT INTO sailings
               (route, origin, destination, service_date, scheduled_departure,
                depart_hhmm, day_type, season)
           VALUES ('route99', 'SLT', 'ERL', '2026-07-06', '2026-07-06T19:30:00Z',
                   '12:30', ?, ?)""",
        (day_type(MONDAYS[0]), season(MONDAYS[0])),
    )
    conn.commit()
    found = fillable_sailings(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )
    assert found == []


@pytest.mark.parametrize("origin", ["SLT", "ERL"])
def test_candidates_are_scoped_to_the_terminal_asked_about(conn, config, origin):
    add_sailing(conn, config, MONDAYS[0], "12:30", origin="SLT")
    add_sailing(conn, config, MONDAYS[0], "13:30", origin="ERL")

    found = fillable_sailings(
        conn,
        config,
        origin=origin,
        target_date=TARGET,
        depart_hhmm="12:30" if origin == "SLT" else "13:30",
        now=TARGET,
    )
    assert all(c.origin == origin for c in found)
    assert found


def test_a_sailing_read_but_unresolved_is_not_counted_as_recorded(conn, config, monkeypatch):
    """Reading a sailing's frames and classifying it are different things.

    With no berth view and no published departure time, a busy sailing reads perfectly and
    still comes out `unknown` — which the distribution excludes. Counting frames as success
    told somebody the answer now included frames that had changed nothing.
    """
    add_sailing(conn, config, MONDAYS[0], "12:30")
    add_frames(conn, config, MONDAYS[0], "12:30")

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        stats = ExtractionStats()
        stats.extracted = len(frames)
        return stats

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)
    result = fill_for_slot(
        conn, config, origin="SLT", target_date=TARGET, depart_hhmm="12:30", now=TARGET
    )

    # Frames were read, but nothing usable came of them: no observations were written by
    # the stub, so the sailing stays unknown.
    assert result.frames_read > 0
    assert result.recorded == 0
    assert result.unresolved == 1


def test_the_essential_set_does_not_move_as_frames_are_read(conn, config):
    """Choosing the nearest *unread* frame to each offset made the set shift: once the
    nearest four were read, the next call picked the next-nearest four. A slot never
    exhausted, so every fill spent money on progressively worse frames while the outcome
    never changed — and the button never went away.
    """
    from ferrycast.selection import essential_frames_for_sailing
    from ferrycast.timeutil import parse_iso

    sailing_id = add_sailing(conn, config, MONDAYS[0], "12:30")
    add_frames(conn, config, MONDAYS[0], "12:30")
    departure = parse_iso(
        conn.execute(
            "SELECT scheduled_departure FROM sailings WHERE id = ?", (sailing_id,)
        ).fetchone()[0]
    )

    first = essential_frames_for_sailing(conn, config, origin="SLT", departure=departure)
    assert len(first) == len(config.vision.essential_offsets_minutes)

    for frame in first:
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, usable, confidence,
                    created_at)
               VALUES (?, ?, ?, 10, 1, 0.9, '2026-01-01T00:00:00Z')""",
            (frame["id"], config.vision.prompt_version, config.vision.model),
        )
    conn.commit()

    assert essential_frames_for_sailing(conn, config, origin="SLT", departure=departure) == []
    # The canonical set is unchanged — it is the same four frames, now all read.
    everything = essential_frames_for_sailing(
        conn, config, origin="SLT", departure=departure, only_unextracted=False
    )
    assert [r["id"] for r in everything] == [r["id"] for r in first]
