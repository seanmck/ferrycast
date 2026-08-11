"""Bounds from the line, pooled over comparable sailings.

`describe_reports` states these for the one sailing on screen, which leaves them blank on
every future date — every date anybody plans against. Pooling them is what makes them
useful, and it is also what makes them dangerous, so these tests hold two lines.

The safe direction only. A traveller who was turned away proves arriving that late has
failed; a traveller who got on proves nothing that transfers, because whether it worked
depended on how many were ahead of them. The first may tighten the advice. The second is
never allowed to loosen it, and never becomes an "arrive by" time.

And pooling in lead time, not clock time. The tolerance admits neighbouring departures, so
11:20 means something different for a 12:30 than it does for an 11:45.
"""

from datetime import date, timedelta

from ferrycast.query import comparable_report_bounds
from ferrycast.timeutil import combine_local, iso, parse_hhmm

# Tuesday 12:30 out of Saltery Bay, as elsewhere in the suite.
TARGET = date(2026, 8, 11)
TIME = "12:30"


def bounds(conn, config, *, origin="SLT", target_date=TARGET, depart_hhmm=TIME, **kw):
    return comparable_report_bounds(
        conn, config, origin=origin, target_date=target_date, depart_hhmm=depart_hhmm, **kw
    )


def add_report(
    conn,
    config,
    service_date: date,
    hhmm: str,
    *,
    joined: str | None,
    boarded: bool,
    origin: str = "SLT",
):
    """A report from somebody in the line, with or without the optional join time."""
    joined_at = (
        iso(combine_local(service_date, parse_hhmm(joined), config.tz)) if joined else None
    )
    conn.execute(
        """INSERT INTO sailing_reports
               (route, origin, service_date, depart_hhmm, joined_queue_at, boarded,
                source, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, 'test', '2026-01-01T00:00:00Z')""",
        (
            config.route.id,
            origin,
            service_date.isoformat(),
            hhmm,
            joined_at,
            int(boarded),
        ),
    )
    conn.commit()


def tuesdays(count: int, *, start: date = TARGET):
    """Consecutive Tuesdays back from the target — all plain weekdays, all the same slot."""
    return [start - timedelta(days=7 * (i + 1)) for i in range(count)]


def test_nothing_to_say_returns_nothing(conn, config):
    assert bounds(conn, config) is None


def test_reports_without_a_join_time_are_not_a_bound(conn, config):
    """The join field is optional, and half a memory is still worth filing — but a report
    that does not say when they arrived cannot bound when the cutoff was."""
    for day in tuesdays(3):
        add_report(conn, config, day, TIME, joined=None, boarded=False)

    assert bounds(conn, config) is None


def test_someone_turned_away_bounds_the_cutoff(conn, config):
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:20", boarded=False)

    result = bounds(conn, config)
    assert result["turned_away_at"] == "11:20"
    assert result["turned_away_minutes_before"] == 70
    assert result["turned_away_date"] == "2026-08-04"


def test_the_earliest_turning_away_is_the_one_that_counts(conn, config):
    """Not a median. One person turned away is an observed failure, and the advice has to
    clear the earliest of them or it is telling somebody to arrive too late."""
    for day, joined in zip(tuesdays(3), ["11:20", "10:45", "12:00"], strict=True):
        add_report(conn, config, day, TIME, joined=joined, boarded=False)

    result = bounds(conn, config)
    assert result["turned_away_at"] == "10:45"
    assert result["turned_away_date"] == "2026-07-28"
    assert result["turned_away_sailings"] == 3


def test_a_bound_the_deck_space_cutline_already_covers_stays_quiet(conn, config):
    """11:20 is 70 minutes out; a cutline of 90 minutes already tells you to be earlier.
    Repeating it adds a line of type and no information."""
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:20", boarded=False)
    add_report(conn, config, tuesdays(2)[1], TIME, joined="11:10", boarded=True)
    add_report(conn, config, tuesdays(3)[2], TIME, joined="11:15", boarded=True)

    result = bounds(conn, config, cutline_minutes_before=90)
    assert result["turned_away_at"] is None
    # The descriptive half is unaffected: it was never about the cutline.
    assert result["boarded_by"] == "11:15"


def test_a_bound_earlier_than_the_cutline_is_kept(conn, config):
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:20", boarded=False)

    result = bounds(conn, config, cutline_minutes_before=40)
    assert result["turned_away_at"] == "11:20"


def test_boarding_alone_never_produces_an_arrival_time(conn, config):
    """The whole asymmetry in one test: people got on at 11:15, and the answer carries no
    time telling anybody to arrive at 11:15."""
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:15", boarded=True)
    add_report(conn, config, tuesdays(2)[1], TIME, joined="11:05", boarded=True)

    result = bounds(conn, config)
    assert result["turned_away_at"] is None
    assert result["boarded_by"] == "11:15"


def test_one_lucky_arrival_is_not_everyone(conn, config):
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:15", boarded=True)

    assert bounds(conn, config) is None


def test_bounds_are_pooled_in_lead_time_not_clock_time(conn, config):
    """The tolerance admits neighbouring departures. Joining at 11:20 is 70 minutes early
    for the 12:30 and 55 for the 12:15, so the 12:15 report is the *later* arrival even
    though the two clock times are identical. Carried onto the sailing being asked about,
    that is 11:35 — reading it as 11:20 would hand back 15 minutes nobody has."""
    add_report(conn, config, tuesdays(1)[0], "12:15", joined="11:20", boarded=False)

    result = bounds(conn, config)
    assert result["turned_away_minutes_before"] == 55
    assert result["turned_away_at"] == "11:35"


def test_only_comparable_sailings_count(conn, config):
    """Same scoping as the distribution: same terminal, same day type, same slot within the
    tolerance. A Saturday, the other terminal, or the 08:30 are all different questions."""
    saturday = date(2026, 8, 8)
    add_report(conn, config, saturday, TIME, joined="10:00", boarded=False)
    add_report(conn, config, tuesdays(1)[0], "08:30", joined="07:00", boarded=False)
    add_report(conn, config, tuesdays(2)[1], TIME, joined="10:30", boarded=False, origin="ERL")

    assert bounds(conn, config) is None


def test_the_date_being_asked_about_is_excluded(conn, config):
    """Its own reports are already on the page, stated as facts about that sailing rather
    than as evidence about days like it."""
    add_report(conn, config, TARGET, TIME, joined="11:20", boarded=False)

    assert bounds(conn, config) is None


def test_joining_after_the_ferry_left_cannot_answer_how_early_to_be(conn, config):
    """A late vessel makes this legal to file, and it is still a real record — it just says
    nothing about when to turn up."""
    add_report(conn, config, tuesdays(1)[0], TIME, joined="12:45", boarded=False)

    assert bounds(conn, config) is None


def test_both_bounds_travel_together_with_their_sample(conn, config):
    add_report(conn, config, tuesdays(1)[0], TIME, joined="11:20", boarded=False)
    add_report(conn, config, tuesdays(2)[1], TIME, joined="11:05", boarded=True)
    add_report(conn, config, tuesdays(3)[2], TIME, joined="10:50", boarded=True)

    result = bounds(conn, config, cutline_minutes_before=40)
    assert result["turned_away_at"] == "11:20"
    assert result["boarded_by"] == "11:05"
    assert result["n_sailings"] == 3
    assert result["n_reports"] == 3
    assert result["turned_away_sailings"] == 1
