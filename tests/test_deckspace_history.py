"""Building the historical record from deck space alone — no vision cost.

With frames parsed only on demand, the "day like today" distribution needs a free source.
Deck space is already scraped every 15 minutes, and it can say whether a sailing ran out of
room and when. What it cannot say is how many vehicles were still queued on the approach
road, which is exactly the gap the PRD identifies — so a sailing that fills gets its own
`filled` outcome rather than being passed off as `waited_1`.
"""

from datetime import date, timedelta

from ferrycast.aggregate import aggregate_day, classify_from_deck_space
from ferrycast.query import arrival_curve, query_distribution
from ferrycast.timeutil import combine_local, iso, parse_hhmm

from .conftest import add_observation
from .test_query import PLAIN_FRIDAY


def add_deck_space(conn, config, terminal, service_date, hhmm, readings, *, route=None):
    """`readings` is a list of (minutes_before_departure, percent_available)."""
    departure = combine_local(service_date, parse_hhmm(hhmm), config.tz)
    for minutes_before, percent in readings:
        observed = departure - timedelta(minutes=minutes_before)
        conn.execute(
            """INSERT OR IGNORE INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    percent_available, fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
            (
                route or config.route.id,
                terminal,
                iso(observed),
                service_date.isoformat(),
                hhmm,
                percent,
            ),
        )
    conn.commit()


def _series(conn, config, service_date, hhmm, terminal="SLT"):
    from ferrycast.aggregate import _deck_space_series

    return _deck_space_series(
        conn, config.route.id, terminal, service_date.isoformat(), hhmm
    )


# ------------------------------------------------------------------ classification rules


def test_a_sailing_that_never_ran_out_of_room_boarded(conn, config):
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(90, 60), (45, 40), (15, 22)])
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    outcome, filled_at, confidence = classify_from_deck_space(
        _series(conn, config, day, "12:30"), departure
    )

    assert outcome == "boarded"
    assert filled_at is None
    assert confidence


def test_a_sailing_that_hit_zero_is_filled_not_waited(conn, config):
    """`filled` deliberately stops short of claiming how long anyone waited."""
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(90, 40), (45, 10), (30, 0), (15, 0)])
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    outcome, filled_at, _ = classify_from_deck_space(
        _series(conn, config, day, "12:30"), departure
    )

    assert outcome == "filled"
    assert outcome not in ("waited_1", "waited_2plus")
    # The first zero reading is the moment it stopped being possible to get on.
    assert filled_at == iso(departure - timedelta(minutes=30))


def test_fill_time_is_the_first_zero_not_the_last(conn, config):
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(60, 0), (45, 0), (15, 0)])
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    _, filled_at, _ = classify_from_deck_space(_series(conn, config, day, "12:30"), departure)

    assert filled_at == iso(departure - timedelta(minutes=60))


def test_readings_after_departure_do_not_count(conn, config):
    """A later sailing's figures must not be read as this one filling up."""
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(30, 50), (-30, 0)])
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    outcome, filled_at, _ = classify_from_deck_space(
        _series(conn, config, day, "12:30"), departure
    )

    assert outcome == "boarded"
    assert filled_at is None


def test_a_feed_that_stopped_early_yields_unknown(conn, config):
    """Plenty of room 90 minutes out proves nothing about the last 90 minutes."""
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(120, 80), (90, 70)])
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    outcome, _, _ = classify_from_deck_space(_series(conn, config, day, "12:30"), departure)

    assert outcome == "unknown"


def test_no_deck_space_data_yields_unknown(conn, config):
    departure = combine_local(date(2026, 8, 14), parse_hhmm("12:30"), config.tz)
    assert classify_from_deck_space([], departure)[0] == "unknown"


def test_a_cancelled_sailing_is_recognised(conn, config):
    day = date(2026, 8, 14)
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                percent_available, status_text, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30', NULL, 'cancelled', 'ok')""",
        (config.route.id, iso(combine_local(day, parse_hhmm("11:00"), config.tz)), day.isoformat()),
    )
    conn.commit()
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)

    assert classify_from_deck_space(_series(conn, config, day, "12:30"), departure)[0] == "cancelled"


# ------------------------------------------------------------------- end-to-end aggregation


def test_aggregation_builds_history_with_no_frames_at_all(conn, config):
    """The point of the exercise: a full historical record for zero vision spend."""
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(90, 40), (45, 12), (25, 0)])
    add_deck_space(conn, config, "SLT", day, "16:30", [(90, 70), (45, 55), (10, 30)])

    aggregate_day(conn, config, day)

    rows = {
        row["depart_hhmm"]: row
        for row in conn.execute(
            """SELECT s.depart_hhmm, r.outcome, r.method, r.filled_at
                 FROM sailings s JOIN sailing_records r ON r.sailing_id = s.id
                WHERE s.origin = 'SLT' AND s.service_date = ?""",
            (day.isoformat(),),
        )
    }
    assert rows["12:30"]["outcome"] == "filled"
    assert rows["12:30"]["method"] == "deck_space"
    assert rows["12:30"]["filled_at"]
    assert rows["16:30"]["outcome"] == "boarded"
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0


def test_frames_and_deck_space_answer_different_questions(conn, config):
    """They were treated as rivals for one word, and the frames won by being read first.

    They are not rivals. The board watched the deck run out of room; the camera watched the
    compound empty. Both are true, and together they are the tightest kind of success — the
    thing the old single outcome had no way to say, because whichever source spoke last
    deleted the other's finding.
    """
    day = date(2026, 8, 14)
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)
    add_deck_space(conn, config, "SLT", day, "12:30", [(45, 10), (25, 0)])
    # Frames show the queue cleared, so nobody was actually left behind.
    add_observation(conn, config, "SLT", departure - timedelta(minutes=15), 30, ferry_at_dock=True)
    add_observation(conn, config, "SLT", departure + timedelta(minutes=15), 0)

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome, r.filled, r.left_behind, r.method, r.filled_at FROM sailings s
             JOIN sailing_records r ON r.sailing_id = s.id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30'"""
    ).fetchone()
    assert (row["filled"], row["left_behind"]) == (1, 0)
    assert row["outcome"] == "filled"
    # The queue was measured, so the record still rests on the better measurement...
    assert row["method"].startswith("frames")
    # ...and the board's reading is kept for the one thing only it can date: the cutoff.
    assert row["filled_at"]


def test_deck_space_of_another_route_is_not_borrowed(conn, config):
    day = date(2026, 8, 14)
    add_deck_space(conn, config, "SLT", day, "12:30", [(45, 0)], route="other")

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome FROM sailings s JOIN sailing_records r ON r.sailing_id = s.id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30'"""
    ).fetchone()
    assert row["outcome"] == "unknown"


# ------------------------------------------------------------------------------ querying


def _seed_history(conn, config, fill_offsets):
    """One Friday per entry; None means the sailing never filled."""
    days = [date(2026, 7, 3) + timedelta(days=7 * i) for i in range(len(fill_offsets))]
    for day, offset in zip(days, fill_offsets, strict=True):
        readings = [(90, 50), (60, 25)]
        if offset is None:
            readings.append((10, 15))
        else:
            readings.append((offset, 0))
        add_deck_space(conn, config, "SLT", day, "12:30", readings)
        aggregate_day(conn, config, day)
    return days


def test_distribution_reports_filled_sailings(conn, config):
    _seed_history(conn, config, [30, 45, None, 20])

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.n == 4
    assert result.counts["filled"] == 3
    assert result.counts["boarded"] == 1
    assert result.filled_share == 0.75


def test_distribution_reports_when_it_typically_fills(conn, config):
    """The actual answer to "how late can I turn up"."""
    _seed_history(conn, config, [30, 45, 20, 30])

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.typical_fill_minutes_before == 30
    assert result.typical_fill_local == "12:00"  # 30 min before a 12:30 departure


def test_distribution_says_what_the_evidence_can_support(conn, config):
    _seed_history(conn, config, [30, 45, 20, 30])

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.evidence == "deck_space"
    assert "not how many vehicles were still queued" in result.evidence_note
    assert all(s.evidence == "deck_space" for s in result.samples)


def test_samples_carry_the_fill_time(conn, config):
    _seed_history(conn, config, [30, 45, 20, 30])

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    filled = [s for s in result.samples if s.outcome == "filled"]
    assert all(s.filled_at_local for s in filled)
    assert {s.fill_minutes_before for s in filled} == {30, 45, 20}


def test_arrival_curve_falls_back_to_deck_space(conn, config):
    _seed_history(conn, config, [30, 45, 20, 30])

    curve = arrival_curve(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert curve["source"] == "deck_space"
    assert curve["unit"] == "% deck space left"
    assert curve["points"]
    # Space drains as departure approaches.
    medians = [p["median"] for p in curve["points"]]
    assert medians[0] >= medians[-1]
