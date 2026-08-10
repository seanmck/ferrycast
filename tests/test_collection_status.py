"""Proof-of-life for the collector.

A new install answers the query page from past comparable sailings it does not have yet,
so it is correctly blank for weeks — which is indistinguishable from a broken scraper. That
ambiguity cost real debugging time, so the page now says what it has collected instead of
just what it cannot answer.

The rule these tests protect: this is *evidence*, never an answer. It must not turn into a
distribution over three sailings by another name.
"""

from datetime import date, timedelta

from ferrycast.query import collection_status
from ferrycast.timeutil import iso, now_utc

from .test_query import seed_record


def add_reading(conn, config, *, minutes_ago: int, terminal="SLT", percent=40, hhmm="09:25"):
    observed = now_utc() - timedelta(minutes=minutes_ago)
    conn.execute(
        """INSERT OR REPLACE INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                percent_available, fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(observed),
            observed.date().isoformat(),
            hhmm,
            percent,
        ),
    )
    conn.commit()


def test_a_fresh_install_reports_nothing_rather_than_failing(conn, config):
    status = collection_status(conn, config)
    assert status["reading"] is None
    assert status["recent"] == []
    assert status["recorded"] == 0
    assert status["collecting"] is False


def test_a_recent_reading_means_the_collector_is_alive(conn, config):
    add_reading(conn, config, minutes_ago=3)
    status = collection_status(conn, config)
    assert status["collecting"] is True
    assert status["reading"]["minutes_ago"] == 3
    assert status["reading"]["percent_available"] == 40
    # The terminal is named, not coded: this line is read by a person, not a script.
    assert status["reading"]["terminal"] == "Saltery Bay"


def test_one_missed_cycle_is_not_an_outage(conn, config):
    """A single failed scrape is normal. Flagging it would train the reader to ignore it."""
    add_reading(conn, config, minutes_ago=config.capture.interval_minutes + 2)
    assert collection_status(conn, config)["collecting"] is True


def test_a_long_silence_is_flagged(conn, config):
    add_reading(conn, config, minutes_ago=config.capture.interval_minutes * 4)
    assert collection_status(conn, config)["collecting"] is False


def test_failed_scrapes_do_not_count_as_a_reading(conn, config):
    """A row recording that the fetch failed is not evidence the feed is working."""
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm, fetch_status, error)
           VALUES (?, 'SLT', ?, '2026-08-10', '09:25', 'error', 'timeout')""",
        (config.route.id, iso(now_utc())),
    )
    conn.commit()
    assert collection_status(conn, config)["reading"] is None


def test_recorded_sailings_are_listed_newest_first(conn, config):
    seed_record(conn, config, date(2026, 8, 3), "09:25", "boarded")
    seed_record(conn, config, date(2026, 8, 10), "09:25", "filled")
    seed_record(conn, config, date(2026, 7, 27), "09:25", "boarded")

    recent = collection_status(conn, config)["recent"]
    assert [r["service_date"] for r in recent] == ["2026-08-10", "2026-08-03", "2026-07-27"]
    assert recent[0]["outcome"] == "filled"


def test_unknown_sailings_are_counted_but_not_listed(conn, config):
    """Day one is all unknowns. Listing them would read as data; hiding them would
    overstate coverage. So: counted, not listed."""
    seed_record(conn, config, date(2026, 8, 10), "09:25", "unknown")
    seed_record(conn, config, date(2026, 8, 10), "11:45", "boarded")

    status = collection_status(conn, config)
    assert status["unknown"] == 1
    assert status["recorded"] == 1
    assert [r["depart_hhmm"] for r in status["recent"]] == ["11:45"]


def test_the_list_is_capped(conn, config):
    for day in range(1, 12):
        seed_record(conn, config, date(2026, 8, day), "09:25", "boarded")
    assert len(collection_status(conn, config, limit=6)["recent"]) == 6
    # The total still reflects everything, so the cap cannot understate coverage.
    assert collection_status(conn, config, limit=6)["recorded"] == 11


def test_another_route_does_not_leak_in(conn, config):
    """Same guarantee the distribution has: a second route must not pollute this one."""
    seed_record(conn, config, date(2026, 8, 10), "09:25", "boarded")
    conn.execute(
        """INSERT INTO sailings
               (route, origin, destination, service_date, scheduled_departure,
                depart_hhmm, day_type, season)
           VALUES ('route99', 'SLT', 'ERL', '2026-08-11', '2026-08-11T16:25:00Z',
                   '09:25', 'weekday', 'peak_summer')""",
    )
    other = conn.execute("SELECT id FROM sailings WHERE route = 'route99'").fetchone()["id"]
    conn.execute(
        """INSERT INTO sailing_records (sailing_id, outcome, n_frames, computed_at)
           VALUES (?, 'filled', 8, '2026-01-01T00:00:00Z')""",
        (other,),
    )
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                percent_available, fetch_status)
           VALUES ('route99', 'SLT', ?, '2026-08-11', '09:25', 10, 'ok')""",
        (iso(now_utc()),),
    )
    conn.commit()

    status = collection_status(conn, config)
    assert status["recorded"] == 1
    assert [r["service_date"] for r in status["recent"]] == ["2026-08-10"]
    assert status["reading"] is None  # the other route's reading is not proof of this one
