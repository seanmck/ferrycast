"""The departures board, as BC Ferries actually publishes it for Route 7.

The live install spent a day recording the page's own "Last updated: 12:20 pm" stamp as a
cancelled 12:20 sailing — at both terminals, every fifteen minutes, 22 rows, not one of them
real, and every liveness signal green throughout. These tests are built from the real page
text so that failure cannot come back.

The page carries no fill percentage for this route. It carries a status board, and the note
line ("Peak travel. Loading maximum number of vehicles") is the overload signal.
"""

from datetime import UTC, datetime

from ferrycast.deckspace import expected_times, parse_deck_space

# Verbatim from the live page, Saltery Bay -> Earls Cove, 2026-08-10.
PAGE = """
<html><body>
<p>Sailing Delay</p>
<div>Departures</div><div>Ferry tracking</div>
<p>Last updated: 12:20 pm <a href="#">Refresh details</a></p>
<table><tr><th>STATUS</th></tr>
<tr><td>5:35 am <em>Departed</em> 5:39 am<br>Malaspina Sky<br>Arrived: 6:22 am</td></tr>
<tr><td>7:25 am <em>Departed</em> 7:39 am<br>Malaspina Sky<br>Arrived: 8:24 am
    <p>We are working to complete ticket sales</p></td></tr>
<tr><td>9:25 am <em>Departed</em> 9:56 am<br>Malaspina Sky<br>Arrived: 10:41 am
    <p>Peak travel. Loading maximum number of vehicles</p></td></tr>
<tr><td>11:45 am<br>Malaspina Sky<br>Upcoming</td></tr>
<tr><td>2:30 pm<br>Malaspina Sky<br>Upcoming</td></tr>
</table>
<footer><a href="/cancelled">Cancelled Sailings</a></footer>
</body></html>
"""

SLT_TIMES = {"05:35", "07:25", "09:25", "11:45", "14:30", "16:55", "19:05", "21:00"}


def parsed():
    return {row.sailing_hhmm: row for row in parse_deck_space(PAGE, SLT_TIMES)}


def test_only_scheduled_departures_are_recorded():
    """The bug: every HH:MM on the page counted as a sailing."""
    assert set(parsed()) == {"05:35", "07:25", "09:25", "11:45", "14:30"}


def test_the_last_updated_stamp_is_not_a_sailing():
    """This exact line produced 14 junk rows on the live install."""
    assert "12:20" not in parsed()


def test_actual_departure_and_arrival_times_are_not_sailings():
    """5:39, 6:22, 7:39, 8:24, 9:56 and 10:41 all appear on the page as real times."""
    for stray in ("05:39", "06:22", "07:39", "08:24", "09:56", "10:41"):
        assert stray not in parsed(), f"{stray} is not a departure"


def test_the_footer_cancelled_link_does_not_cancel_the_last_sailing():
    """Without a segment cap, the final entry swallows the page footer — and this site has
    a "Cancelled Sailings" link in it. That would have marked the 14:30 cancelled."""
    assert parsed()["14:30"].status_text != "cancelled"


def test_the_overload_note_is_captured():
    """No percentage exists for this route, so this note is the whole signal."""
    assert "Peak travel" in parsed()["09:25"].status_text


def test_a_status_row_is_kept_even_with_no_percentage():
    """The old parser dropped any sailing without a percent, which for this route is all
    of them — so a fetch that worked perfectly stored nothing usable."""
    row = parsed()["11:45"]
    assert row.percent_available is None
    assert "Upcoming" in row.status_text


def test_a_percentage_is_still_read_when_a_route_publishes_one():
    html = "<p>9:25 am</p><p>40% available</p><p>11:45 am</p><p>Upcoming</p>"
    rows = {r.sailing_hhmm: r for r in parse_deck_space(html, SLT_TIMES)}
    assert rows["09:25"].percent_available == 40


def test_full_wording_is_inverted():
    html = "<p>9:25 am</p><p>85% full</p>"
    assert parse_deck_space(html, SLT_TIMES)[0].percent_available == 15


def test_a_real_cancellation_still_registers():
    html = "<p>9:25 am</p><p>Cancelled</p><p>11:45 am</p><p>Upcoming</p>"
    rows = {r.sailing_hhmm: r for r in parse_deck_space(html, SLT_TIMES)}
    assert rows["09:25"].status_text == "cancelled"
    assert rows["11:45"].status_text != "cancelled"


def test_an_unrecognisable_page_yields_nothing_rather_than_guessing():
    """Loud failure. The whole point: no rows beats plausible rows."""
    assert parse_deck_space("<p>Last updated: 12:20 pm</p><p>Cancelled Sailings</p>", SLT_TIMES) == []


def test_expected_times_span_midnight(config):
    """A 22:00 sailing is still on the board after midnight, and tomorrow's first before it."""
    at = datetime(2026, 8, 11, 7, 0, tzinfo=UTC)  # 00:00 local
    times = expected_times(config, "SLT", at)
    assert times, "expected the surrounding days to contribute departures"


def test_expected_times_are_per_terminal(config):
    at = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)
    assert expected_times(config, "SLT", at) != expected_times(config, "ERL", at)


def test_health_flags_fetching_fine_and_recognising_nothing(conn, config):
    """The exact live failure: 22 successful scrapes, zero usable rows, all signals green."""
    from ferrycast.maintenance import health_report
    from ferrycast.timeutil import iso, now_utc

    for minute in range(0, 60, 15):
        conn.execute(
            """INSERT INTO deck_space (route, terminal, observed_at, service_date,
                   sailing_hhmm, fetch_status)
               VALUES (?, 'SLT', ?, '2026-08-10', NULL, 'ok')""",
            (config.route.id, iso(now_utc().replace(minute=minute, second=0, microsecond=0))),
        )
    conn.commit()

    problems = health_report(conn, config, window_days=30).problems
    assert any("none named a scheduled sailing" in p for p in problems), problems
