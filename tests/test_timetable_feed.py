"""The schedule file against the operator's published feed.

What these hold to: the check reports differences and never papers over a feed it could
not read, and the timetable running out is said in advance and said as a fact about the
file — never as "nothing sails".
"""

import io
import zipfile
from datetime import date, time

import pytest

from ferrycast.schedule import (
    ScheduleBlock,
    coverage_problem,
    covered_through,
    in_timetable,
)
from ferrycast.timetable_feed import FeedError, compare, read_feed

ROUTE = "route7"
TERMINALS = ("SLT", "ERL")
STOPS = {"100": "SLT", "200": "ERL"}
ALL_DAYS = frozenset(range(7))


def block(terminal, start, end, departures, days=ALL_DAYS):
    return ScheduleBlock(
        terminal=terminal,
        effective_from=start,
        effective_to=end,
        days=days,
        departures=tuple(time.fromisoformat(d) for d in departures),
    )


def feed_zip(*, trips, calendar, calendar_dates=(), feed_end=None, route_id="7"):
    """A GTFS archive with just the files the check reads.

    `trips` is (service_id, first stop_id, departure HH:MM:SS); each gets a second stop so
    that "the first stop" is something the reader has to work out.
    """
    files = {
        "trips.txt": "route_id,service_id,trip_id\n"
        + "".join(f"{route_id},{service},t{i}\n" for i, (service, _, _) in enumerate(trips))
        + "other,weekday,elsewhere\n",
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        + "".join(
            f"t{i},{dep},{dep},999,2\nt{i},{dep},{dep},{stop},1\n"
            for i, (_, stop, dep) in enumerate(trips)
        )
        + "elsewhere,01:00:00,01:00:00,100,1\n",
        "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n" + "".join(",".join(row) + "\n" for row in calendar),
        "calendar_dates.txt": "service_id,date,exception_type\n"
        + "".join(",".join(row) + "\n" for row in calendar_dates),
    }
    if feed_end:
        files["feed_info.txt"] = f"feed_publisher_name,feed_end_date\nTest,{feed_end}\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


WEEK = ("1", "1", "1", "1", "1", "1")


def winter_feed(**overrides):
    """Mon–Sat two sailings each way; Sundays lose the first round trip."""
    arguments = {
        "trips": [
            ("mon_sat", "100", "05:35:00"),
            ("daily", "100", "07:25:00"),
            ("mon_sat", "200", "06:30:00"),
            ("daily", "200", "08:20:00"),
        ],
        "calendar": [
            ("mon_sat", *WEEK, "0", "20261101", "20261130"),
            ("daily", *WEEK, "1", "20261101", "20261130"),
        ],
        "feed_end": "20261130",
    }
    arguments.update(overrides)
    return read_feed(feed_zip(**arguments), "7", STOPS)


def matching_blocks(end=date(2026, 11, 30)):
    start = date(2026, 11, 1)
    weekdays = frozenset(range(6))
    sunday = frozenset({6})
    return (
        block("SLT", start, end, ["05:35", "07:25"], weekdays),
        block("SLT", start, end, ["07:25"], sunday),
        block("ERL", start, end, ["06:30", "08:20"], weekdays),
        block("ERL", start, end, ["08:20"], sunday),
    )


def test_a_schedule_that_matches_the_feed_has_no_differences():
    differences = compare(
        matching_blocks(), winter_feed(), ROUTE, TERMINALS, date(2026, 11, 1), date(2026, 11, 30)
    )
    assert differences == []


def test_the_departure_terminal_is_the_first_stop_not_the_first_row():
    feed = winter_feed()
    assert feed.departures(date(2026, 11, 2)) == {
        "SLT": frozenset({"05:35", "07:25"}),
        "ERL": frozenset({"06:30", "08:20"}),
    }


def test_a_changed_time_is_reported_with_both_sides():
    blocks = (
        block("SLT", date(2026, 11, 1), date(2026, 11, 30), ["05:35", "07:30"], frozenset(range(6))),
        *matching_blocks()[1:],
    )
    [difference] = compare(
        blocks, winter_feed(), ROUTE, TERMINALS, date(2026, 11, 1), date(2026, 11, 30)
    )
    assert difference.terminal == "SLT"
    assert difference.days == 25  # every Monday to Saturday in the month
    text = difference.describe()
    assert "published but not scheduled: 07:25" in text
    assert "scheduled but not published: 07:30" in text


def test_days_differing_the_same_way_are_one_finding_even_when_not_consecutive():
    # A uniform week, where the feed drops the first sailing on Sundays.
    blocks = (
        block("SLT", date(2026, 11, 1), date(2026, 11, 30), ["05:35", "07:25"]),
        *matching_blocks()[2:],
    )
    [difference] = compare(
        blocks, winter_feed(), ROUTE, TERMINALS, date(2026, 11, 1), date(2026, 11, 30)
    )
    assert (difference.first, difference.last, difference.days) == (
        date(2026, 11, 1),
        date(2026, 11, 29),
        5,
    )
    assert "scheduled but not published: 05:35" in difference.describe()


def test_a_schedule_that_ran_out_differs_on_every_day_the_feed_still_covers():
    differences = compare(
        matching_blocks(end=date(2026, 11, 14)),
        winter_feed(),
        ROUTE,
        TERMINALS,
        date(2026, 11, 15),
        date(2026, 11, 30),
    )
    assert differences
    assert all(d.scheduled == () for d in differences)
    assert "the schedule has nothing" in differences[0].describe()


def test_nothing_is_compared_past_the_end_of_the_feed():
    # The feed's silence after its last date is not a day without sailings.
    differences = compare(
        matching_blocks(end=date(2026, 12, 31)),
        winter_feed(),
        ROUTE,
        TERMINALS,
        date(2026, 11, 1),
        date(2026, 12, 31),
    )
    assert differences == []


def test_calendar_dates_add_and_remove_service():
    feed = winter_feed(
        calendar_dates=[
            ("mon_sat", "20261111", "2"),  # a holiday Wednesday without the first trip
            ("mon_sat", "20261115", "1"),  # and a Sunday given one
        ]
    )
    assert feed.departures(date(2026, 11, 11))["SLT"] == frozenset({"07:25"})
    assert feed.departures(date(2026, 11, 15))["SLT"] == frozenset({"05:35", "07:25"})


def test_a_departure_past_midnight_belongs_to_the_next_local_day():
    feed = winter_feed(
        trips=[("daily", "100", "24:10:00")],
        calendar=[("daily", *WEEK, "1", "20261101", "20261101")],
    )
    assert feed.departures(date(2026, 11, 1)) == {}
    assert feed.departures(date(2026, 11, 2)) == {"SLT": frozenset({"00:10"})}


def test_an_unmapped_departure_stop_fails_the_read_rather_than_dropping_the_trip():
    with pytest.raises(FeedError, match="stop_id '300'"):
        winter_feed(trips=[("daily", "300", "07:25:00")])


def test_a_feed_without_the_route_is_an_error_not_an_empty_timetable():
    with pytest.raises(FeedError, match="no trips for route_id"):
        read_feed(feed_zip(trips=[], calendar=[]), "7", STOPS)


def test_something_that_is_not_a_zip_is_an_error():
    with pytest.raises(FeedError, match="not a zip"):
        read_feed(b"<html>please wait in the queue</html>", "7", STOPS)


def test_without_feed_info_the_last_date_is_the_last_service_date():
    assert winter_feed(feed_end=None).last_date == date(2026, 11, 30)


# ---- how long the schedule file has left


def test_a_day_inside_a_block_is_in_the_timetable_even_when_nothing_sails_on_it():
    blocks = (block("SLT", date(2026, 11, 1), date(2026, 11, 30), ["07:25"], frozenset(range(6))),)
    sunday = date(2026, 11, 15)
    assert not any(b.covers(sunday) for b in blocks)
    assert in_timetable(blocks, sunday, ROUTE, "SLT")
    assert not in_timetable(blocks, date(2026, 12, 1), ROUTE, "SLT")
    assert not in_timetable(blocks, sunday, ROUTE, "ERL")


def test_coverage_ends_at_the_first_gap_not_at_the_latest_block():
    blocks = (
        block("SLT", date(2026, 1, 1), date(2026, 10, 12), ["07:25"]),
        block("ERL", date(2026, 1, 1), date(2026, 10, 12), ["08:20"]),
        block("SLT", date(2026, 11, 11), date(2027, 3, 31), ["07:25"]),
        block("ERL", date(2026, 11, 11), date(2027, 3, 31), ["08:20"]),
    )
    assert covered_through(blocks, ROUTE, TERMINALS, date(2026, 10, 1)) == date(2026, 10, 12)
    assert covered_through(blocks, ROUTE, TERMINALS, date(2026, 10, 13)) is None
    assert covered_through(blocks, ROUTE, TERMINALS, date(2026, 11, 14)) == date(2027, 3, 31)


def test_coverage_is_only_as_long_as_the_shorter_direction():
    blocks = (
        block("SLT", date(2026, 1, 1), date(2026, 12, 31), ["07:25"]),
        block("ERL", date(2026, 1, 1), date(2026, 6, 30), ["08:20"]),
    )
    assert covered_through(blocks, ROUTE, TERMINALS, date(2026, 6, 1)) == date(2026, 6, 30)


def test_the_lapse_is_warned_about_a_month_ahead_and_not_before():
    early = (
        block("SLT", date(2026, 1, 1), date(2026, 11, 30), ["07:25"]),
        block("ERL", date(2026, 1, 1), date(2026, 11, 30), ["08:20"]),
    )
    assert coverage_problem(early, ROUTE, TERMINALS, date(2026, 10, 31)) is None
    warning = coverage_problem(early, ROUTE, TERMINALS, date(2026, 11, 14))
    assert "runs out after 2026-11-30" in warning and "16 days" in warning
    lapsed = coverage_problem(early, ROUTE, TERMINALS, date(2026, 12, 1))
    assert "no block covering today" in lapsed


def test_the_feed_stops_must_name_exactly_the_routes_terminals(config):
    from ferrycast.config import ConfigError, load_config

    text = config.source_path.read_text()
    feed = '\n[route.timetable_feed]\nurl = "https://example.invalid/g.zip"\nroute_id = "7"\n'
    marker = "[[route.terminals]]"
    head, tail = text.split(marker, 1)

    config.source_path.write_text(
        head + feed.strip() + '\nstops = { SLT = "100", ERL = "200" }\n\n' + marker + tail
    )
    loaded = load_config(config.source_path).route.timetable_feed
    assert loaded.terminal_by_stop == {"100": "SLT", "200": "ERL"}

    config.source_path.write_text(
        head + feed.strip() + '\nstops = { SLT = "100" }\n\n' + marker + tail
    )
    with pytest.raises(ConfigError, match="a stop for each terminal"):
        load_config(config.source_path)
