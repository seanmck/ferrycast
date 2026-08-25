"""The board at midnight, as it caught the live install on 2026-08-24.

The scrape at 00:00:07 found the 23rd's board still up — seven sailings, every one
"Departed", the last of them "Departed 10:06 pm" with the "loading maximum" note — and
filed all of it under the 24th. The board turned over by the next scrape, but the damage
was done: for the rest of the 24th each sailing's *latest departed reading* was the 23rd's,
so the header showed the 21:00 as having left at 22:06 while it was still 21:16, and the
24th's records inherited the 23rd's fill note.

Three defences, each tested here: the store files a page by the day its sailings belong
to; `_board_departure` refuses a departure the scrape could not have witnessed yet; and
the v11 migration refiles the pages already written the old way.
"""

from datetime import UTC, date, datetime, timedelta

from ferrycast.aggregate import _board_departure
from ferrycast.db import SCHEMA_VERSION, init_db, schema_version
from ferrycast.deckspace import parse_deck_space, store_rows
from ferrycast.timeutil import combine_local, iso, parse_hhmm

SLT_TIMES = {"05:35", "07:25", "09:25", "11:45", "14:30", "16:55", "19:05", "21:00"}

# Yesterday's board, still up seven seconds into today. Verbatim shape of the live page.
YESTERDAYS_BOARD = """
<html><body>
<div>Departures</div><div>Ferry tracking</div>
<table>
<tr><td>5:35 am<br>Malaspina Sky<br>ETA : Variable</td></tr>
<tr><td>7:25 am <em>Departed</em> 7:34 am<br>Malaspina Sky<br>Arrived: 8:24 am</td></tr>
<tr><td>9:25 am <em>Departed</em> 9:49 am<br>Malaspina Sky<br>Arrived: 10:41 am</td></tr>
<tr><td>2:30 pm <em>Departed</em> 3:03 pm<br>Malaspina Sky</td></tr>
<tr><td>9:00 pm <em>Departed</em> 10:06 pm<br>Malaspina Sky
    <p>Peak travel. Loading maximum number of vehicles</p></td></tr>
</table>
<footer><a href="/cancelled">Cancelled Sailings</a></footer>
</body></html>
"""

# The same board five minutes later, turned over to the new day.
TODAYS_BOARD = """
<html><body>
<div>Departures</div><div>Ferry tracking</div>
<table>
<tr><td>5:35 am<br>Malaspina Sky<br>Upcoming</td></tr>
<tr><td>7:25 am<br>Malaspina Sky<br>Upcoming</td></tr>
<tr><td>9:00 pm<br>Malaspina Sky<br>Upcoming</td></tr>
</table>
</body></html>
"""


def _stored(conn, observed_at):
    return {
        row["sailing_hhmm"]: row["service_date"]
        for row in conn.execute(
            "SELECT sailing_hhmm, service_date FROM deck_space WHERE observed_at = ?",
            (iso(observed_at),),
        )
    }


def _scrape(conn, config, page, observed_at):
    store_rows(conn, config, "SLT", observed_at, parse_deck_space(page, SLT_TIMES))
    return _stored(conn, observed_at)


# --- the store ------------------------------------------------------------------------


def test_yesterdays_board_still_up_after_midnight_is_filed_under_yesterday(conn, config):
    at = datetime(2026, 8, 24, 7, 0, 7, tzinfo=UTC)  # 00:00:07 local
    filed = _scrape(conn, config, YESTERDAYS_BOARD, at)
    # Every row, including the one with no departure time of its own: the board is a
    # day's board, and one departed row that cannot be today's dates the whole page.
    assert filed == dict.fromkeys(["05:35", "07:25", "09:25", "14:30", "21:00"], "2026-08-23")


def test_the_board_turned_over_is_todays(conn, config):
    at = datetime(2026, 8, 24, 7, 5, 9, tzinfo=UTC)  # 00:05:09 local
    filed = _scrape(conn, config, TODAYS_BOARD, at)
    assert filed == dict.fromkeys(["05:35", "07:25", "21:00"], "2026-08-24")


def test_a_boat_that_left_a_few_minutes_early_stays_on_its_own_day(conn, config):
    page = """
    <div>Departures</div>
    <table><tr><td>9:25 am <em>Departed</em> 9:21 am<br>Malaspina Sky</td></tr></table>
    """
    at = datetime(2026, 8, 24, 16, 22, tzinfo=UTC)  # 09:22 local: the slot is 3 min ahead
    assert _scrape(conn, config, page, at) == {"09:25": "2026-08-24"}


def test_a_late_boat_still_upcoming_stays_on_its_own_day(conn, config):
    page = """
    <div>Departures</div>
    <table><tr><td>9:00 pm<br>Malaspina Sky<br>Upcoming</td></tr></table>
    """
    at = datetime(2026, 8, 25, 6, 50, tzinfo=UTC)  # 23:50 local, the 21:00 not yet gone
    assert _scrape(conn, config, page, at) == {"21:00": "2026-08-24"}


def test_tomorrows_first_sailing_shown_before_midnight_is_tomorrows(conn, config):
    page = """
    <div>Departures</div>
    <table>
    <tr><td>7:25 am <em>Departed</em> 7:34 am<br>Malaspina Sky</td></tr>
    <tr><td>9:00 pm <em>Departed</em> 10:06 pm<br>Malaspina Sky</td></tr>
    <tr><td>5:35 am<br>Malaspina Sky<br>Upcoming</td></tr>
    </table>
    """
    at = datetime(2026, 8, 25, 6, 50, tzinfo=UTC)  # 23:50 local on the 24th
    filed = _scrape(conn, config, page, at)
    assert filed == {"07:25": "2026-08-24", "21:00": "2026-08-24", "05:35": "2026-08-25"}


def test_a_page_of_nothing_but_cancellations_is_taken_at_its_date(conn, config):
    """Cancellations are announced in advance, so a cancelled row proves nothing about
    which day the page describes. Without a departed row to date it, the scrape's own
    date stands — a guess either way, and the current behaviour is the honest one."""
    page = """
    <div>Departures</div>
    <table><tr><td>9:25 am<br>Cancelled</td></tr></table>
    """
    at = datetime(2026, 8, 24, 13, 0, tzinfo=UTC)  # 06:00 local, the 09:25 cancelled early
    assert _scrape(conn, config, page, at) == {"09:25": "2026-08-24"}


# --- the reader -----------------------------------------------------------------------


def _board_row(conn, config, observed_at, service_date, hhmm, departed, note="Departed"):
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                departed_hhmm, status_text, fetch_status)
           VALUES (?, 'SLT', ?, ?, ?, ?, ?, 'ok')""",
        (config.route.id, iso(observed_at), service_date, hhmm, departed, note),
    )
    conn.commit()


def test_a_departure_the_scrape_could_not_have_witnessed_is_not_believed(conn, config):
    """The misfiled row itself, as it sat in the live database: today's 21:00, "departed
    22:06", read at 00:00. Whatever filed it, the board cannot report a departure before
    it has happened, so the reader owes the header nothing from it."""
    midnight = datetime(2026, 8, 24, 7, 0, 7, tzinfo=UTC)
    _board_row(conn, config, midnight, "2026-08-24", "21:00", "22:06")

    assert _board_departure(conn, config.route.id, "SLT", "2026-08-24", "21:00", config) is None


def test_the_genuine_reading_is_still_believed_over_the_impossible_one(conn, config):
    midnight = datetime(2026, 8, 24, 7, 0, 7, tzinfo=UTC)
    _board_row(conn, config, midnight, "2026-08-24", "21:00", "22:06")
    left = combine_local(date(2026, 8, 24), parse_hhmm("21:12"), config.tz)
    _board_row(conn, config, left + timedelta(minutes=1), "2026-08-24", "21:00", "21:12")

    assert _board_departure(conn, config.route.id, "SLT", "2026-08-24", "21:00", config) == left


# --- the migration --------------------------------------------------------------------


def _v10_database(path):
    """A database written by the old store: the 23rd's board read across midnight."""
    conn = init_db(path)

    def row(observed_at, service_date, hhmm, departed, note):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    departed_hhmm, status_text, fetch_status)
               VALUES ('route7', 'SLT', ?, ?, ?, ?, ?, 'ok')""",
            (observed_at, service_date, hhmm, departed, note),
        )

    # 23:55 local on the 23rd — correctly the 23rd's.
    row("2026-08-24T06:55:04Z", "2026-08-23", "21:00", "22:06", "Departed 10:06 pm")
    row("2026-08-24T06:55:04Z", "2026-08-23", "05:35", None, "Malaspina Sky ETA : Variable")
    # 00:00:07 on the 24th — the same board, stamped with the 24th.
    row("2026-08-24T07:00:07Z", "2026-08-24", "21:00", "22:06", "Departed 10:06 pm")
    row("2026-08-24T07:00:07Z", "2026-08-24", "05:35", None, "Malaspina Sky ETA : Variable")
    # 00:05:09 — the board has turned over.
    row("2026-08-24T07:05:09Z", "2026-08-24", "21:00", None, "Malaspina Sky Upcoming")
    row("2026-08-24T07:05:09Z", "2026-08-24", "05:35", None, "Malaspina Sky Upcoming")
    # Not a misfile: the 07:25 left at 07:25 two days running, hours apart, and no reading
    # of either day says "Upcoming" afterwards.
    row("2026-08-22T14:26:00Z", "2026-08-22", "07:25", "07:25", "Departed 7:25 am")
    row("2026-08-23T14:26:00Z", "2026-08-23", "07:25", "07:25", "Departed 7:25 am")
    # Not a misfile: a board glitch — departed, then "Upcoming" again — with no matching
    # reading on the day before to say it was ever the previous day's board.
    row("2026-08-20T19:41:00Z", "2026-08-20", "12:30", "12:40", "Departed 12:40 pm")
    row("2026-08-20T19:46:00Z", "2026-08-20", "12:30", None, "Malaspina Sky Upcoming")
    # The other terminal's "we looked, no board" marker from the same scrape.
    conn.execute(
        """INSERT INTO deck_space (route, terminal, observed_at, service_date, sailing_hhmm,
                                   fetch_status, error)
           VALUES ('route7', 'ERL', '2026-08-24T07:00:07Z', '2026-08-24', NULL,
                   'not_published', 'no board')"""
    )
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()


def _filed(conn):
    return {
        (r["terminal"], r["observed_at"], r["sailing_hhmm"]): r["service_date"]
        for r in conn.execute(
            "SELECT terminal, observed_at, sailing_hhmm, service_date FROM deck_space"
        )
    }


def test_the_migration_refiles_the_midnight_scrape_and_nothing_else(tmp_path):
    path = tmp_path / "v10.db"
    _v10_database(path)

    conn = init_db(path)

    assert schema_version(conn) == SCHEMA_VERSION
    filed = _filed(conn)
    # The whole misfiled scrape goes back a day, the row with no departure time included.
    assert filed[("SLT", "2026-08-24T07:00:07Z", "21:00")] == "2026-08-23"
    assert filed[("SLT", "2026-08-24T07:00:07Z", "05:35")] == "2026-08-23"
    # The turned-over board is the 24th's and stays so.
    assert filed[("SLT", "2026-08-24T07:05:09Z", "21:00")] == "2026-08-24"
    assert filed[("SLT", "2026-08-24T07:05:09Z", "05:35")] == "2026-08-24"
    # The coincidence and the glitch are left where they were.
    assert filed[("SLT", "2026-08-23T14:26:00Z", "07:25")] == "2026-08-23"
    assert filed[("SLT", "2026-08-20T19:41:00Z", "12:30")] == "2026-08-20"
    # And the other terminal's marker is not a board reading at all.
    assert filed[("ERL", "2026-08-24T07:00:07Z", None)] == "2026-08-24"


def test_the_migration_is_idempotent(tmp_path):
    """Running it twice must not walk the same rows back another day."""
    from ferrycast.db import _refile_midnight_board_rows

    path = tmp_path / "v10.db"
    _v10_database(path)
    conn = init_db(path)
    before = _filed(conn)
    _refile_midnight_board_rows(conn)
    assert _filed(conn) == before
