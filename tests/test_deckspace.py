from ferrycast.deckspace import parse_deck_space, scrape_once

AVAILABLE_PAGE = """
<html><body>
  <div class="sailing"><h3>12:30 pm</h3><span>45% Available</span><p>Island Sky</p></div>
  <div class="sailing"><h3>4:30 pm</h3><span>0% Available</span></div>
</body></html>
"""

FULL_PAGE = """
<html><body>
  <div class="sailing"><h3>12:30 pm</h3><span>80% Full</span></div>
</body></html>
"""

CANCELLED_PAGE = """
<html><body>
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


def test_unparsed_page_is_distinguished_from_a_fetch_error(conn, config, monkeypatch):
    from ferrycast import deckspace
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        deckspace,
        "fetch",
        lambda *a, **k: FetchResult(ok=True, text="<html>nothing useful</html>"),
    )
    scrape_once(conn, config)

    statuses = {
        row["fetch_status"]
        for row in conn.execute("SELECT fetch_status FROM deck_space").fetchall()
    }
    assert statuses == {"unparsed"}


# Each terminal publishes its own departures, so the fixtures differ per URL. Serving one
# page to both terminals used to "pass": the parser accepted any time it saw, so Earls Cove
# happily recorded Saltery Bay's departures as its own.
ERL_PAGE = """
<html><body>
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
