"""The day board — every sailing on one date, answered at once.

The phone answers one sailing per page load, so "which boat should I take" can only be
asked by asking it ten times. The board is the wide layout's answer to that, which makes
two things load-bearing: a row must never disagree with the panel it opens, and ten answers
must not cost ten times one answer.
"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from ferrycast.query import day_board, query_distribution
from ferrycast.web.app import create_app

from .test_query import fridays, seed_record

# Fri 14 Aug 2026 — an ordinary Friday, no long weekend attached. SLT runs 08:30/12:30/16:30.
FRIDAY = date(2026, 8, 14)


@pytest.fixture
def client(conn, config):
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        yield test_client


def board_for(conn, config, day=FRIDAY, origin="SLT"):
    return {row.depart_hhmm: row for row in day_board(conn, config, origin=origin, target_date=day)}


# ---- The query ---------------------------------------------------------------------------


def test_the_board_covers_every_sailing_in_the_timetable(conn, config):
    rows = day_board(conn, config, origin="SLT", target_date=FRIDAY)
    assert [row.depart_hhmm for row in rows] == ["08:30", "12:30", "16:30"]


def test_a_sailing_with_no_history_is_a_row_not_a_gap(conn, config):
    """The timetable is the board's spine. Dropping the sailings nothing is known about
    would silently shorten the day and hide exactly the gaps worth seeing."""
    seed_record(conn, config, date(2026, 7, 3), "12:30", "boarded")

    rows = board_for(conn, config)
    assert rows["08:30"].n == 0
    assert rows["08:30"].boarded_share == 0.0
    assert rows["12:30"].n == 1


def test_a_row_counts_what_the_panel_counts(conn, config):
    """The row a reader clicks and the answer that opens are one fact, so they walk the
    same ladder. A board saying 46% beside a headline of 54% reads as two findings."""
    for day, outcome in zip(
        fridays(4), ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)

    row = board_for(conn, config)["12:30"]
    panel = query_distribution(
        conn, config, origin="SLT", target_date=FRIDAY, depart_hhmm="12:30"
    )

    assert row.n == panel.n
    assert row.counts == panel.counts
    assert row.filled_share == panel.filled_share
    assert row.arrive_by == panel.typical_fill_local
    assert row.fill_minutes_before == panel.typical_fill_minutes_before
    assert row.sufficient == panel.sufficient


def test_made_it_is_the_boarded_share(conn, config):
    for day, outcome in zip(
        fridays(4), ["boarded", "boarded", "boarded", "waited_1"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)

    row = board_for(conn, config)["12:30"]
    assert row.boarded_share == 0.75
    assert row.filled_share == 0.25


def test_a_widened_row_says_so(conn, config):
    """Winter has nothing in it, so the search widens to all seasons. The board has no room
    to name the rung, but it must not present a widened count as an exact one."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "boarded")

    exact = board_for(conn, config)["12:30"]
    widened = board_for(conn, config, day=date(2026, 12, 4))["12:30"]

    assert not exact.relaxed
    assert widened.relaxed
    assert widened.n == exact.n  # same sailings, reached by a longer route


def test_a_whole_day_costs_a_handful_of_queries(conn, config, monkeypatch):
    """Ten sailings must not cost ten times one sailing. The ladder's buckets are shared
    across the day and read once each, so this stays flat as the timetable grows."""
    for day in fridays(6):
        for hhmm in ("08:30", "12:30", "16:30"):
            seed_record(conn, config, day, hhmm, "boarded")

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        day_board(conn, config, origin="SLT", target_date=FRIDAY)
    finally:
        conn.set_trace_callback(None)

    candidate_reads = [sql for sql in seen if "JOIN sailing_records" in sql]
    assert len(candidate_reads) <= 3, f"{len(candidate_reads)} candidate reads for 3 sailings"


# ---- The board on the page ---------------------------------------------------------------


def test_the_board_is_rendered_server_side(client, conn, config):
    """Same rule as the phone's answer: the first paint carries it, no round trip."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "waited_1")

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert body.count('<a class="board-row') == 3       # every sailing in the timetable
    assert 'class="board-time">12:30<' in body
    assert ' on" href="/?origin=SLT&amp;service_date=2026-08-14&amp;time=12:30"' in body


def test_every_row_is_a_link_to_the_page_it_already_had(client, conn, config):
    """No script anywhere in the board: picking a sailing is a navigation, which is what
    the app was already doing when the phone submitted its form."""
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "/?origin=SLT&amp;service_date=2026-08-14&amp;time=16:30" in body


def test_each_row_says_what_it_is(client, conn, config):
    """The column headings label cells a screen reader is never told about, so they are
    marked decorative and the row carries the sentence instead."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "100% got on across 4 comparable sailings" in body
    assert "12:30, no comparable history yet" not in body
    assert "08:30, no comparable history yet" in body


@pytest.fixture
def friday_lunchtime(monkeypatch):
    """Fri 14 Aug 2026, 13:00 in Vancouver — the 08:30 and 12:30 have gone, the 16:30 has not."""
    when = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: when)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: when)


def test_a_sailing_that_has_gone_recedes_rather_than_vanishing(client, friday_lunchtime):
    body = client.get("/?origin=SLT&service_date=2026-08-14").text
    assert body.count("board-row past") == 2
    assert "SAILED" in body.upper()


def test_the_now_rule_sits_between_the_boats(client, friday_lunchtime):
    body = client.get("/?origin=SLT&service_date=2026-08-14").text
    assert "board-now" in body
    assert "Now · 13:00" in body


def test_no_now_rule_on_a_day_that_has_not_started(client, friday_lunchtime):
    """A rule above the first row marks nothing at all."""
    body = client.get("/?origin=SLT&service_date=2026-08-21").text
    # The markup, not the class name — the stylesheet that defines it is on every page.
    assert '<div class="board-now">' not in body


def test_the_next_sailing_carries_the_countdown(client, friday_lunchtime):
    body = client.get("/?origin=SLT&service_date=2026-08-14").text
    assert "in 3 h 30 min" in body


def test_a_future_day_is_read_by_outlook_not_by_countdown(client, conn, config, friday_lunchtime):
    """Next Friday has no "next departure" worth counting down to; what each boat usually
    does is the column's real job there."""
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-21").text

    assert "room most days" in body
    assert "in 3 h" not in body


def test_an_outlook_is_never_stated_off_a_thin_bucket(client, conn, config, friday_lunchtime):
    """"Expect to wait" off two sailings is an editorial, not a finding."""
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_2plus")

    body = client.get("/?origin=SLT&service_date=2026-08-21").text

    assert "expect to wait" not in body
    assert "toss-up" not in body


def test_the_worst_sailing_of_the_day_is_named(client, conn, config, friday_lunchtime):
    for day in fridays(6):
        seed_record(conn, config, day, "08:30", "boarded")
        seed_record(conn, config, day, "12:30", "waited_2plus")
        seed_record(conn, config, day, "16:30", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-21").text
    assert "worst of day" in body


def test_no_worst_when_every_boat_boards(client, conn, config, friday_lunchtime):
    """Labelling the least-good boat of an easy day "worst" warns about a sailing that
    boards nine times in ten."""
    for day in fridays(6):
        for hhmm in ("08:30", "12:30", "16:30"):
            seed_record(conn, config, day, hhmm, "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-21").text
    assert "worst of day" not in body


def test_the_widening_mark_is_explained_where_it_is_used(client, conn, config):
    """A bare asterisk in a column of counts is a footnote to nothing."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-12-04&time=12:30").text

    assert "board-n r relaxed" in body
    assert "the search was widened" in body


def test_no_widening_mark_when_nothing_was_widened(client, conn, config):
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "board-n r relaxed" not in body
    assert "the search was widened" not in body


def test_a_sailing_with_no_history_is_not_marked_as_widened(conn, config):
    """It walks the whole ladder and ends on the last rung, but nothing was widened to
    reach that — there was nothing anywhere to reach. The mark would point at an absence,
    and the footnote it triggers would explain a search that never happened."""
    rows = board_for(conn, config)  # nothing seeded at all

    assert all(row.n == 0 for row in rows.values())
    assert not any(row.relaxed for row in rows.values())


# ---- What the wide layout must not disturb ------------------------------------------------


def test_the_phone_keeps_its_own_picker(client):
    """The board replaces the picker only where there is room for it. The form itself stays
    in the document, hidden by a media query — it is still the whole control on a phone."""
    body = client.get("/").text
    assert 'class="card picker"' in body
    assert 'name="time"' in body


def test_the_board_and_chrome_are_absent_below_the_breakpoint(client):
    """Not "hidden by chance" — the default state of both is display:none, and only a
    min-width query turns them on."""
    body = client.get("/").text
    assert ".chrome, .board { display: none; }" in body


def test_the_board_is_capped_and_the_chrome_shares_the_measure(client):
    """A row is read across, from the departure to the share that got on, and that reading
    fails long before a 4K monitor runs out of pixels — stretched full width, a row put its
    own time and its own percentage a metre apart.

    Both the slab and the bar above it are pinned to one custom property. Hard-coding the
    width twice is the failure worth guarding against: the two drift, and the wordmark
    stops sitting over the first column.
    """
    body = client.get("/").text

    assert "--deck-max:" in body
    assert "max-width: var(--deck-max)" in body          # the slab
    assert "calc((100% - var(--deck-max)) / 2" in body   # the bar, on the same measure


def test_the_health_page_has_no_board(client):
    """`/health` shares the shell but is not this design. It must not inherit a day board,
    a route switcher or a date stepper."""
    body = client.get("/health").text
    assert "board-row" not in body
    assert "chrome-dir" not in body
