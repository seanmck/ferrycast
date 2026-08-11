"""Proof-of-life for the collector.

A new install answers the query page from past comparable sailings it does not have yet,
so it is correctly blank for weeks — which is indistinguishable from a broken scraper. That
ambiguity cost real debugging time, so the page now says what it has collected instead of
just what it cannot answer.

Two rules these tests protect. This is *evidence*, never an answer: it must not turn into a
distribution over three sailings by another name. And it is evidence about the sailing on
screen: scoped the way the distribution is scoped, so it cannot become the same list of
unrelated crossings pinned to every page.
"""

from datetime import date, timedelta

from ferrycast.query import collection_status
from ferrycast.timeutil import combine_local, iso, parse_hhmm

from .test_query import seed_record

# The slot every test asks about: Tuesday 12:30 out of Saltery Bay.
TARGET = date(2026, 8, 11)
TIME = "12:30"


def status(conn, config, *, origin="SLT", target_date=TARGET, depart_hhmm=TIME, **kw):
    return collection_status(
        conn, config, origin=origin, target_date=target_date, depart_hhmm=depart_hhmm, **kw
    )


def add_departure_report(
    conn, config, service_date: date, hhmm: str, left_hhmm: str, *, origin="SLT", boarded=1
):
    """A first-hand report that says when the vessel actually went."""
    conn.execute(
        """INSERT INTO sailing_reports
               (route, origin, service_date, depart_hhmm, departed_at, boarded,
                source, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, 'test', ?)""",
        (
            config.route.id,
            origin,
            service_date.isoformat(),
            hhmm,
            iso(combine_local(service_date, parse_hhmm(left_hhmm), config.tz)),
            boarded,
            iso(combine_local(service_date, parse_hhmm(left_hhmm), config.tz)),
        ),
    )
    conn.commit()


def weekdays(count: int, *, start: date = TARGET):
    """Consecutive Tuesdays back from the target date, newest first — all plain weekdays.

    Not Mondays: the first one back would be BC Day, which is a different day type and so
    correctly not an equivalent sailing at all."""
    return [start - timedelta(days=7 * i) for i in range(count)]


def test_a_fresh_install_reports_nothing_rather_than_failing(conn, config):
    result = status(conn, config)
    assert result["recent"] == []
    assert result["recorded"] == 0
    assert result["since"] is None


def test_recorded_sailings_are_listed_newest_first(conn, config):
    for day, outcome in zip(weekdays(3), ["filled", "boarded", "waited_1"], strict=True):
        seed_record(conn, config, day, TIME, outcome)

    recent = status(conn, config)["recent"]
    assert [r["service_date"] for r in recent] == ["2026-08-11", "2026-08-04", "2026-07-28"]
    assert [r["outcome"] for r in recent] == ["filled", "boarded", "waited_1"]


def test_only_equivalent_sailings_are_listed(conn, config):
    """The whole point of the scoping: this panel is about the sailing on screen.

    An unscoped list is the same list on every page, which reads as evidence about
    whichever sailing is selected and is evidence about none of them.
    """
    seed_record(conn, config, TARGET, TIME, "boarded")          # the slot itself
    seed_record(conn, config, TARGET, "12:40", "waited_1")      # inside the tolerance
    seed_record(conn, config, TARGET, "16:30", "waited_2plus")  # a different sailing
    seed_record(conn, config, TARGET, TIME, "filled", origin="ERL")  # the other direction
    seed_record(conn, config, date(2026, 8, 8), TIME, "filled")  # a Saturday

    result = status(conn, config)
    assert {(r["service_date"], r["depart_hhmm"]) for r in result["recent"]} == {
        ("2026-08-11", "12:30"),
        ("2026-08-11", "12:40"),
    }
    assert result["recorded"] == 2


def test_the_tolerance_is_the_one_the_distribution_uses(conn, config):
    seed_record(conn, config, TARGET, "12:45", "boarded")  # exactly on the tolerance
    seed_record(conn, config, TARGET, "12:46", "boarded")  # one minute past it

    assert [r["depart_hhmm"] for r in status(conn, config)["recent"]] == ["12:45"]
    assert status(conn, config)["tolerance_minutes"] == config.query.time_tolerance_minutes


def test_unknown_sailings_are_counted_but_not_listed(conn, config):
    """Day one is all unknowns. Listing them would read as data; hiding them would
    overstate coverage. So: counted, not listed."""
    seed_record(conn, config, TARGET, TIME, "unknown")
    seed_record(conn, config, date(2026, 8, 4), TIME, "boarded")

    result = status(conn, config)
    assert result["unknown"] == 1
    assert result["recorded"] == 1
    assert [r["service_date"] for r in result["recent"]] == ["2026-08-04"]


def test_the_list_is_capped(conn, config):
    for day in weekdays(11):
        seed_record(conn, config, day, TIME, "boarded")
    assert len(status(conn, config, limit=6)["recent"]) == 6
    # The total still reflects everything, so the cap cannot understate coverage.
    assert status(conn, config, limit=6)["recorded"] == 11


def test_the_actual_departure_is_shown_with_its_delta(conn, config):
    """The scheduled time is on the timetable; what a traveller wants is when it went."""
    seed_record(conn, config, TARGET, TIME, "filled")
    add_departure_report(conn, config, TARGET, TIME, "12:47")

    row = status(conn, config)["recent"][0]
    assert row["departed_local"] == "12:47"
    assert row["minutes_late"] == 17


def test_an_early_departure_reads_as_a_negative_delta(conn, config):
    seed_record(conn, config, TARGET, TIME, "boarded")
    add_departure_report(conn, config, TARGET, TIME, "12:26")

    assert status(conn, config)["recent"][0]["minutes_late"] == -4


def test_an_unreported_departure_is_blank_rather_than_guessed(conn, config):
    """Only a person in the line knows when the vessel went. No report, no claim."""
    seed_record(conn, config, TARGET, TIME, "boarded")

    row = status(conn, config)["recent"][0]
    assert row["departed_local"] is None
    assert row["minutes_late"] is None


def test_disagreeing_reports_settle_on_the_median(conn, config):
    seed_record(conn, config, TARGET, TIME, "filled")
    for left in ("12:34", "12:40", "12:52"):
        add_departure_report(conn, config, TARGET, TIME, left)

    assert status(conn, config)["recent"][0]["departed_local"] == "12:40"


def test_a_report_on_another_sailing_does_not_move_this_one(conn, config):
    seed_record(conn, config, TARGET, TIME, "boarded")
    add_departure_report(conn, config, TARGET, "16:30", "16:55")
    add_departure_report(conn, config, TARGET, TIME, "12:35", origin="ERL")

    assert status(conn, config)["recent"][0]["departed_local"] is None


def test_another_route_does_not_leak_in(conn, config):
    """Same guarantee the distribution has: a second route must not pollute this one."""
    seed_record(conn, config, TARGET, TIME, "boarded")
    conn.execute(
        """INSERT INTO sailings
               (route, origin, destination, service_date, scheduled_departure,
                depart_hhmm, day_type, season)
           VALUES ('route99', 'SLT', 'ERL', '2026-08-04', '2026-08-04T12:30:00-07:00',
                   '12:30', 'weekday', 'peak_summer')""",
    )
    other = conn.execute("SELECT id FROM sailings WHERE route = 'route99'").fetchone()["id"]
    conn.execute(
        """INSERT INTO sailing_records (sailing_id, outcome, n_frames, computed_at)
           VALUES (?, 'filled', 8, '2026-01-01T00:00:00Z')""",
        (other,),
    )
    conn.commit()

    result = status(conn, config)
    assert result["recorded"] == 1
    assert [r["service_date"] for r in result["recent"]] == ["2026-08-11"]
