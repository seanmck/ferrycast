from ferrycast.deckspace import notice_says_full, parse_deck_space, scrape_once

AVAILABLE_PAGE = """
<html><body>
  <ul><li><a href="#tabs-1">Departures</a></li></ul>
  <div class="sailing"><h3>12:30 pm</h3><span>45% Available</span><p>Island Sky</p></div>
  <div class="sailing"><h3>4:30 pm</h3><span>0% Available</span></div>
</body></html>
"""

FULL_PAGE = """
<html><body>
  <ul><li><a href="#tabs-1">Departures</a></li></ul>
  <div class="sailing"><h3>12:30 pm</h3><span>80% Full</span></div>
</body></html>
"""

CANCELLED_PAGE = """
<html><body>
  <ul><li><a href="#tabs-1">Departures</a></li></ul>
  <div class="sailing"><h3>8:30 am</h3><span>Cancelled</span></div>
  <div class="sailing"><h3>12:30 pm</h3><span>30% Available</span></div>
</body></html>
"""


def test_parses_percent_available():
    rows = parse_deck_space(AVAILABLE_PAGE)
    assert [(r.sailing_hhmm, r.percent_available) for r in rows] == [
        ("12:30", 45),
        ("16:30", 0),
    ]


def test_percent_full_is_inverted_to_available():
    rows = parse_deck_space(FULL_PAGE)
    assert rows[0].percent_available == 20


def test_cancelled_sailings_are_recorded_without_a_percentage():
    rows = parse_deck_space(CANCELLED_PAGE)
    by_time = {r.sailing_hhmm: r for r in rows}
    assert by_time["08:30"].percent_available is None
    assert by_time["08:30"].status_text == "cancelled"
    assert by_time["12:30"].percent_available == 30


def test_am_pm_conversion():
    rows = parse_deck_space("<p>12:05 am</p><p>10% Available</p>")
    assert rows[0].sailing_hhmm == "00:05"
    rows = parse_deck_space("<p>12:05 pm</p><p>10% Available</p>")
    assert rows[0].sailing_hhmm == "12:05"


def test_unrecognised_page_yields_nothing_rather_than_raising():
    assert parse_deck_space("<html><body><p>Site maintenance</p></body></html>") == []
    assert parse_deck_space("") == []


def test_scripts_are_ignored():
    html = "<html><script>var t='3:25 pm'; var p='99%';</script><body></body></html>"
    assert parse_deck_space(html) == []


def test_failed_scrape_is_logged_and_does_not_raise(conn, config, monkeypatch):
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace, "fetch", lambda *a, **k: FetchResult(ok=False, error="HTTP 503")
    )
    results = scrape_once(conn, config)

    assert all(not r["ok"] for r in results)
    rows = conn.execute(
        "SELECT fetch_status, error FROM deck_space WHERE fetch_status = 'error'"
    ).fetchall()
    assert len(rows) == 2
    assert "503" in rows[0]["error"]


# A board is on offer — the tab is there — but nothing on it can be read. This is the
# regression case: the site reshuffled, or the wording moved, and somebody must look.
BROKEN_BOARD_PAGE = """
<html><body>
  <ul><li><a href="#tabs-1">Departures</a></li><li><a href="#tabs-2">Ferry tracking</a></li></ul>
  <div id="tabs-1">nothing we recognise</div>
</body></html>
"""

# The Earls Cove page, in miniature: a route title, a service notice, ferry tracking, and no
# departures tab anywhere. BC Ferries publishes no board for this direction at all.
NO_BOARD_PAGE = """
<html><body>
  <h3>Sunshine Coast (Earls Cove) to Powell River (Saltery Bay)</h3>
  <ul><li><a href="#tabs-2">Ferry tracking</a></li></ul>
  <nav><a href="/current-conditions/departures">Departures &amp; arrivals</a></nav>
</body></html>
"""


def test_unparsed_page_is_distinguished_from_a_fetch_error(conn, config, monkeypatch):
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace,
        "fetch",
        lambda *a, **k: FetchResult(ok=True, text=BROKEN_BOARD_PAGE),
    )
    scrape_once(conn, config)

    statuses = {
        row["fetch_status"]
        for row in conn.execute("SELECT fetch_status FROM deck_space").fetchall()
    }
    assert statuses == {"unparsed"}


def test_a_direction_with_no_board_is_not_a_parse_failure(conn, config, monkeypatch):
    """BC Ferries publishes no departures board for Earls Cove -> Saltery Bay. Recording
    that as `unparsed` made a permanent fact of the route look like a broken parser, at
    five-minute intervals, and buried the alarm that would mean something."""
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace, "fetch", lambda *a, **k: FetchResult(ok=True, text=NO_BOARD_PAGE)
    )
    results = scrape_once(conn, config)

    rows = conn.execute("SELECT fetch_status, raw FROM deck_space").fetchall()
    assert {row["fetch_status"] for row in rows} == {"not_published"}
    # Nothing to diagnose, so nothing is kept — this page arrives 288 times a day.
    assert all(row["raw"] is None for row in rows)
    # And the scheduler must not report it among the failures.
    assert all(r.get("skipped") for r in results)


def test_the_navigation_link_is_not_mistaken_for_a_board(conn, config):
    """Every page on the site links to "Departures & arrivals". Only a page that actually
    offers the board carries "Departures" as a heading of its own."""
    from ferrycast.deckspace import publishes_departures

    assert not publishes_departures(NO_BOARD_PAGE)
    assert publishes_departures(BROKEN_BOARD_PAGE)


# Each terminal publishes its own departures, so the fixtures differ per URL. Serving one
# page to both terminals used to "pass": the parser accepted any time it saw, so Earls Cove
# happily recorded Saltery Bay's departures as its own.
ERL_PAGE = """
<html><body>
  <ul><li><a href="#tabs-1">Departures</a></li></ul>
  <div class="sailing"><h3>9:30 am</h3><span>45% Available</span><p>Island Sky</p></div>
  <div class="sailing"><h3>1:30 pm</h3><span>0% Available</span></div>
</body></html>
"""


def _page_for(url: str) -> str:
    return ERL_PAGE if "erl" in url.lower() else AVAILABLE_PAGE


def test_successful_scrape_stores_rows(conn, config, monkeypatch):
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace, "fetch", lambda url, **k: FetchResult(ok=True, text=_page_for(url))
    )
    results = scrape_once(conn, config)

    assert all(r["ok"] for r in results)
    assert conn.execute("SELECT COUNT(*) FROM deck_space").fetchone()[0] == 4  # 2 rows x 2 terminals


def test_one_terminals_sailings_are_not_recorded_against_the_other(conn, config, monkeypatch):
    """Saltery Bay's board served at the Earls Cove URL must record nothing."""
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace, "fetch", lambda *a, **k: FetchResult(ok=True, text=AVAILABLE_PAGE)
    )
    scrape_once(conn, config)

    stored = conn.execute(
        "SELECT DISTINCT terminal FROM deck_space WHERE fetch_status = 'ok'"
    ).fetchall()
    assert [r[0] for r in stored] == ["SLT"]
    # ...and the failure is recorded with the page, so it can be diagnosed later.
    raw = conn.execute(
        "SELECT raw FROM deck_space WHERE terminal = 'ERL' AND fetch_status = 'unparsed'"
    ).fetchone()
    assert raw and "12:30" in raw[0]


# --- the operator's note about how a sailing loaded --------------------------------------

BOARD_TWICE = """
<html><body>
  11:45 am Departed 12:03 pm Malaspina Sky
  11:45 am Departed 12:03 pm Malaspina Sky Arrived: 12:47 pm
     Peak travel. Loading maximum number of vehicles
  2:30 pm Malaspina Sky Upcoming
  Last updated: 9:27 PM Refresh details Book to guarantee your spot
</body></html>
"""


def test_the_note_survives_the_boards_habit_of_printing_each_sailing_twice():
    """The board prints a terse line then a fuller one carrying the arrival time and the
    operator's note. Keeping the first and discarding the rest threw the note away every
    time — across 2368 stored rows not one carried it, though the page publishes it daily."""
    rows = parse_deck_space(BOARD_TWICE, expected={"11:45", "14:30"})
    by_time = {r.sailing_hhmm: r for r in rows}

    assert notice_says_full(by_time["11:45"].status_text)
    # and the merge keeps what the terse line had, rather than trading one for the other
    assert by_time["11:45"].departed_hhmm == "12:03"


def test_page_furniture_is_not_read_as_a_note_about_a_sailing():
    """The last sailing has no following time to stop at, so its segment runs into the page
    chrome. A phrase match over that would eventually find something it should not."""
    rows = parse_deck_space(BOARD_TWICE, expected={"11:45", "14:30"})
    last = {r.sailing_hhmm: r for r in rows}["14:30"]
    assert "Last updated" not in (last.status_text or "")
    assert "Book to guarantee" not in (last.status_text or "")


def test_loading_to_capacity_is_not_the_same_claim_as_leaving_someone_behind():
    """Kept apart deliberately. The note describes the deck; whether anyone was turned away
    is about the compound. Only the second is an overload."""
    assert notice_says_full("Peak travel. Loading maximum number of vehicles")
    assert not notice_says_full("Departed 2:49 pm Malaspina Sky Arrived: 3:39 pm")
    assert not notice_says_full(None)
    assert not notice_says_full("cancelled")
