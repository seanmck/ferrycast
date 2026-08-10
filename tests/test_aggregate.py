"""R4 is the inferential heart of the project — these tests pin the overload rule down."""

from datetime import date, datetime, timedelta

from ferrycast.aggregate import aggregate_day, classify
from ferrycast.timeutil import combine_local, parse_hhmm

from .conftest import add_observation, build_sailing_frames


def _classify(**kwargs):
    base = dict(
        residual=0,
        queue_at_departure=40,
        departure_seen=True,
        capacity=125,
        residual_threshold=5,
    )
    return classify(**{**base, **kwargs})


def test_cleared_queue_means_everyone_boarded():
    outcome, overload, cancelled, carryover = _classify(residual=1)
    assert (outcome, overload, cancelled, carryover) == ("boarded", False, False, 0)


def test_residual_queue_after_departure_is_an_overload():
    outcome, overload, cancelled, carryover = _classify(residual=30)
    assert outcome == "waited_1"
    assert overload is True
    assert carryover == 30


def test_carryover_beyond_one_vessel_means_two_or_more_sailings():
    outcome, overload, _, carryover = _classify(residual=200)
    assert outcome == "waited_2plus"
    assert overload is True
    assert carryover == 200


def test_persistent_queue_with_no_vessel_is_a_cancellation():
    outcome, overload, cancelled, carryover = _classify(residual=60, departure_seen=False)
    assert outcome == "cancelled"
    assert cancelled is True
    assert overload is False
    assert carryover == 60


def test_missing_frames_yield_unknown_rather_than_a_guess():
    assert _classify(residual=None)[0] == "unknown"
    assert _classify(queue_at_departure=None)[0] == "unknown"


def test_threshold_is_inclusive_boundary():
    assert _classify(residual=4)[0] == "boarded"
    assert _classify(residual=5)[0] == "waited_1"


def _departure(config, day: date, hhmm: str) -> datetime:
    return combine_local(day, parse_hhmm(hhmm), config.tz)


def test_end_to_end_overloaded_sailing(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(
        conn, config, departure, before=[20, 55, 90], after=[40, 45], dock_before=True
    )

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.* FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()
    assert row["outcome"] == "waited_1"
    assert row["overload"] == 1
    assert row["peak_queue"] == 90
    assert row["queue_at_departure"] == 90
    assert row["residual_queue"] == 40
    assert row["carryover"] == 40


def test_end_to_end_cleared_sailing(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(conn, config, departure, before=[10, 25, 30], after=[0, 2])

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome, r.peak_queue FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30'"""
    ).fetchone()
    assert row["outcome"] == "boarded"
    assert row["peak_queue"] == 30


def test_sailing_with_no_frames_is_unknown_not_empty(conn, config):
    day = date(2026, 8, 14)
    counts = aggregate_day(conn, config, day)
    # Every scheduled sailing still gets a row, marked unknown.
    assert counts["unknown"] == 6
    total = conn.execute("SELECT COUNT(*) FROM sailing_records").fetchone()[0]
    assert total == 6


def test_unusable_observations_are_excluded(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    # A dark frame claiming an empty compound must not be read as "the queue cleared".
    add_observation(conn, config, "SLT", departure - timedelta(minutes=15), 80)
    add_observation(
        conn, config, "SLT", departure + timedelta(minutes=15), 0, usable=False, confidence=0.05
    )

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["outcome"] == "unknown"


def test_truncated_queue_is_flagged(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    add_observation(
        conn, config, "SLT", departure - timedelta(minutes=15), 120, beyond_frame=True
    )
    add_observation(conn, config, "SLT", departure + timedelta(minutes=15), 60)

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.queue_truncated FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["queue_truncated"] == 1


def test_deck_space_alone_can_confirm_a_departure(conn, config):
    """Night sailings may have no usable camera view of the vessel; the feed still anchors it."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    # Queue persists, and no vessel is ever seen at the dock.
    build_sailing_frames(
        conn, config, departure, before=[50, 70], after=[40], dock_before=False
    )
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                percent_available, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30', 0, 'ok')""",
        (config.route.id, "2026-08-14T19:00:00Z", day.isoformat()),
    )
    conn.commit()

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome, r.deck_space_min FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    # The sailing ran, so this is an overload rather than a cancellation.
    assert row["outcome"] == "waited_1"
    assert row["deck_space_min"] == 0


def test_aggregation_is_idempotent(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(conn, config, departure, before=[20, 55], after=[40])

    aggregate_day(conn, config, day)
    aggregate_day(conn, config, day)

    assert conn.execute("SELECT COUNT(*) FROM sailings").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM sailing_records").fetchone()[0] == 6


def test_frames_from_the_previous_sailing_do_not_leak_in(conn, config):
    """A 12:30 residual must not be mistaken for the 16:30's queue build-up."""
    day = date(2026, 8, 14)
    earlier = _departure(config, day, "12:30")
    # A big queue right after the 12:30 departure.
    add_observation(conn, config, "SLT", earlier + timedelta(minutes=15), 95)
    later = _departure(config, day, "16:30")
    add_observation(conn, config, "SLT", later - timedelta(minutes=15), 10)
    add_observation(conn, config, "SLT", later + timedelta(minutes=15), 0)

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.peak_queue, r.outcome FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '16:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["peak_queue"] == 10
    assert row["outcome"] == "boarded"
