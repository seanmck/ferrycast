from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from ferrycast.timeutil import combine_local, parse_hhmm
from ferrycast.web.app import _countdown, create_app

from .test_day_board import seed_deck_space, seed_tracking
from .test_query import PLAIN_FRIDAY, fridays, seed_record
from .test_report_bounds import add_report


@pytest.fixture
def client(conn, config):
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        yield test_client


def test_index_renders_without_any_data(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "FerryCast" in response.text


def test_index_renders_the_distribution_server_side(client, conn, config):
    for day, outcome in zip(
        fridays(4), ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)

    response = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")

    assert response.status_code == 200
    # The answer is in the first response, not fetched afterwards by script.
    assert "Waited 1 sailing" in response.text
    assert "4 comparable sailings" in response.text


def test_index_warns_when_the_sample_is_thin(client, conn, config):
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")
    response = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")
    assert "Small sample" in response.text


def test_index_carries_the_bounds_from_the_line(client, conn, config):
    """Reports on comparable sailings reach a date nobody has reported on — which is the
    only kind of date anybody plans against."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "filled")
    add_report(conn, config, date(2026, 7, 3), "12:30", joined="11:20", boarded=False)

    response = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")

    assert "On days like this" in response.text
    assert "11:20" in response.text


def test_the_index_never_turns_boarding_into_an_arrival_time(client, conn, config):
    """Two people got on at 11:15. The page says so and stops there: no "arrive by 11:15",
    because whether it worked depended on how many were ahead of them."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "boarded")
    add_report(conn, config, date(2026, 7, 3), "12:30", joined="11:15", boarded=True)
    add_report(conn, config, date(2026, 7, 10), "12:30", joined="11:05", boarded=True)

    response = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")

    assert "everyone who did get on had" in response.text
    assert "11:15" in response.text
    assert "not a target" in response.text
    assert "Arrive before" not in response.text


def test_index_offers_both_directions(client):
    response = client.get("/")
    assert "Saltery Bay" in response.text
    assert "Earls Cove" in response.text


@pytest.fixture
def at_ten_in_the_morning(monkeypatch):
    """Fri 14 Aug 2026, 10:00 in Vancouver. SLT has gone 08:30; ERL has gone 09:30."""
    monkeypatch.setattr(
        "ferrycast.query.now_utc", lambda: datetime(2026, 8, 14, 17, 0, tzinfo=UTC)
    )


def test_switching_terminal_lands_on_the_next_sailing_not_the_first(
    client, at_ten_in_the_morning
):
    """Every field is resubmitted together, so switching terminal arrives carrying the
    other terminal's departure time. That is never in this one's timetable, and falling
    back to the top of the list offered a sailing that had left hours earlier."""
    # 12:30 is a Saltery Bay time; Earls Cove runs 09:30, 13:30, 15:25.
    response = client.get("/?origin=ERL&service_date=2026-08-14&time=12:30")

    assert '<option value="13:30" selected>' in response.text
    assert '<option value="09:30" selected>' not in response.text


def test_a_sailing_that_has_gone_is_still_shown_when_it_is_asked_for(
    client, at_ten_in_the_morning
):
    """Only the fallback is time-aware. Naming a past sailing is how history is read."""
    response = client.get("/?origin=ERL&service_date=2026-08-14&time=09:30")
    assert '<option value="09:30" selected>' in response.text


def test_the_first_sailing_still_leads_on_a_date_that_is_not_today(
    client, at_ten_in_the_morning
):
    response = client.get("/?origin=ERL&service_date=2026-08-15&time=12:30")
    assert '<option value="09:30" selected>' in response.text


# ---- The live camera -------------------------------------------------------------------
#
# A photograph is persuasive in a way a caption cannot undo, so the rule is narrow: the
# terminal camera appears for the next departure and for nothing else.


@pytest.fixture
def at_ten_sharp(monkeypatch):
    """As above, but the web layer reads the clock too — for "has this sailing gone"."""
    monkeypatch.setattr(
        "ferrycast.query.now_utc", lambda: datetime(2026, 8, 14, 17, 0, tzinfo=UTC)
    )
    monkeypatch.setattr(
        "ferrycast.web.app.now_utc", lambda: datetime(2026, 8, 14, 17, 0, tzinfo=UTC)
    )


def test_the_camera_shows_for_the_next_sailing(client, at_ten_sharp):
    """10:00, and Saltery Bay's next departure is the 12:30 — the one you could still make."""
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "https://example.invalid/slt.jpg" in body
    assert "Saltery Bay now" in body


def test_the_camera_is_the_terminal_you_are_leaving_from(client, at_ten_sharp):
    body = client.get("/?origin=ERL&service_date=2026-08-14&time=13:30").text
    assert "https://example.invalid/erl.jpg" in body
    assert "https://example.invalid/slt.jpg" not in body


def test_no_camera_on_a_later_sailing_the_same_day(client, at_ten_sharp):
    """16:30 is hours out. What the compound looks like now says nothing about it."""
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=16:30").text
    assert "example.invalid" not in body


def test_no_camera_on_a_sailing_that_has_gone(client, at_ten_sharp):
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=08:30").text
    assert "example.invalid" not in body


def test_no_camera_on_another_day(client, at_ten_sharp):
    body = client.get("/?origin=SLT&service_date=2026-08-21&time=12:30").text
    assert "example.invalid" not in body


def test_no_camera_when_none_is_configured(client, config, at_ten_sharp):
    """Blank webcam_url is a supported setup — the whole panel simply does not appear."""
    from ferrycast.config import Terminal

    app = client.app
    app.state.config = replace(
        config,
        routes=(
            replace(
                config.route,
                terminals=tuple(
                    Terminal(
                        code=t.code,
                        name=t.name,
                        destination=t.destination,
                        webcam_url="",
                        deck_space_url=t.deck_space_url,
                    )
                    for t in config.route.terminals
                ),
            ),
        ),
    )

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "example.invalid" not in body
    assert "Saltery Bay now" not in body


def test_the_camera_url_is_cache_busted_on_the_minute(client, at_ten_sharp):
    """Without this a browser serves the frame it cached, and "live" is a lie. Bucketed
    rather than per-request so a reload storm does not become a fetch storm."""
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    expected = int(datetime(2026, 8, 14, 17, 0, tzinfo=UTC).timestamp()) // 60
    assert f"https://example.invalid/slt.jpg?t={expected}" in body


# The clock is not the whole rule. A boat running late is the case where "next departure"
# and "the sailing you might still catch" come apart, and the people in the frame are that
# late sailing's own line — so it keeps the camera until BC Ferries says it has gone.


@pytest.fixture
def at_one_o_clock(monkeypatch):
    """13:00 on the same Friday: the 12:30's hour has passed, the 16:30 is the next out."""
    when = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: when)
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: when)


def test_the_camera_stays_with_a_sailing_still_at_the_dock(
    client, conn, config, at_one_o_clock
):
    """13:00, and the board still lists the 12:30 with no departure against it. Those cars
    are waiting on that boat, and its page is where somebody deciding whether to drive down
    is looking."""
    seed_deck_space(conn, config, at="12:55", rows={"12:30": None, "16:30": None})

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "https://example.invalid/slt.jpg" in body
    assert "has not been called away" in body


def test_the_camera_leaves_a_sailing_the_board_has_called_away(
    client, conn, config, at_one_o_clock
):
    """The same 12:30, with BC Ferries saying it left at 12:41. The queue in the frame is
    the 16:30's now, and the picture goes with it."""
    seed_deck_space(conn, config, at="12:55", rows={"12:30": "12:41", "16:30": None})

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "example.invalid" not in body


def test_a_late_sailing_needs_live_evidence_to_keep_the_camera(client, at_one_o_clock):
    """With nothing read off the conditions page, "still at the dock" is a guess — and a
    photograph offered on a guess is the one thing a caption cannot walk back."""
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "example.invalid" not in body


def test_the_next_sailing_keeps_its_camera_while_the_one_before_runs_late(
    client, conn, config, at_one_o_clock
):
    """Both pages show the compound, because both are boats you could still catch. Only the
    late one claims the line as its own."""
    seed_deck_space(conn, config, at="12:55", rows={"12:30": None, "16:30": None})

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=16:30").text

    assert "https://example.invalid/slt.jpg" in body
    assert "has not been called away" not in body


@pytest.fixture
def at_ten_to_ten(monkeypatch):
    """09:50: the Earls Cove 09:30 is overdue, and no conditions page exists to say so."""
    when = datetime(2026, 8, 14, 16, 50, tzinfo=UTC)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: when)
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: when)


def test_the_tracker_holds_the_camera_where_nothing_publishes_a_board(
    client, conn, config, at_ten_to_ten
):
    """Earls Cove is the whole point of this: no board to read, the 09:30 twenty minutes
    overdue, and the vessel still on the water inbound. That compound full of cars is the
    09:30's queue, and its page is the one they are looking at."""
    seed_tracking(conn, config, [("09:35", "Under Way", "E"), ("09:47", "Under Way", "SE")])

    body = client.get("/?origin=ERL&service_date=2026-08-14&time=09:30").text

    assert "https://example.invalid/erl.jpg" in body
    assert "has not been called away" in body


def test_api_query_returns_the_distribution(client, conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1")

    response = client.get("/api/query", params={"origin": "SLT", "service_date": PLAIN_FRIDAY.isoformat(), "time": "12:30"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["n"] == 3
    assert payload["counts"]["waited_1"] == 3
    assert payload["day_type"] == "friday"
    assert len(payload["samples"]) == 3


def test_api_rejects_an_unknown_terminal(client):
    response = client.get("/api/query", params={"origin": "XXX"})
    assert response.status_code == 400
    assert "unknown terminal" in response.json()["detail"]


def test_api_rejects_a_malformed_date(client):
    response = client.get("/api/query", params={"origin": "SLT", "service_date": "14-08-2026"})
    assert response.status_code == 400


def test_api_sailings_lists_upcoming_departures(client):
    response = client.get("/api/sailings")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_api_health_reports_status(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert "capture_success_rate" in payload
    assert "healthy" in payload


def test_export_endpoint_serves_csv(client, conn, config):
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")
    response = client.get("/export/sailings.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "waited_1" in response.text


def test_export_endpoint_rejects_an_unknown_dataset(client):
    assert client.get("/export/nonsense.csv").status_code == 400


def test_arrival_curve_endpoint(client):
    response = client.get(
        "/api/arrival-curve", params={"origin": "SLT", "time": "12:30"}
    )
    assert response.status_code == 200
    assert "points" in response.json()


def test_health_page_renders(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "Pipeline" in response.text
    assert "Season cover" in response.text


def test_health_page_surfaces_every_problem(client, conn, config):
    """An empty pipeline has problems, and each one must reach the page."""
    from ferrycast.maintenance import health_report

    report = health_report(conn, config, window_days=30)
    assert report.problems, "expected an empty database to report problems"

    response = client.get("/health")
    for problem in report.problems:
        assert problem.capitalize() in response.text


def test_health_page_survives_an_empty_database(client):
    """The strip and latest-frame panel must render before any data exists."""
    response = client.get("/health")
    assert response.status_code == 200
    assert "No frames have been extracted yet" in response.text


def test_fonts_are_served_locally(client):
    response = client.get("/static/fonts/ibmplexmono-400.woff2")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"


def test_brand_assets_are_served(client):
    """Every file the pages link to. These ship as package data, and the container installs
    the package rather than copying src/ — so a missing entry there is invisible until the
    icons quietly 404 in production."""
    for path, media in [
        ("/static/brand/mark.png", "image/png"),
        ("/static/brand/og.png", "image/png"),
        ("/static/brand/apple-touch-icon.png", "image/png"),
        ("/static/brand/favicon.ico", "image/vnd.microsoft.icon"),
        ("/favicon.ico", "image/x-icon"),
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == media, path


def test_every_page_carries_the_mark(client):
    for path in ["/", "/health"]:
        assert "/static/brand/mark.png" in client.get(path).text, path


def test_link_preview_image_is_absolute(client):
    response = client.get("/")
    assert 'content="http://testserver/static/brand/og.png"' in response.text


def test_link_preview_image_follows_the_forwarded_scheme(client):
    """Behind a TLS-terminating proxy the app itself only ever sees http, and an http
    og:image on an https page is dropped by several unfurlers."""
    response = client.get("/", headers={"x-forwarded-proto": "https"})
    assert 'content="https://testserver/static/brand/og.png"' in response.text


def test_index_does_not_reference_a_font_cdn(client):
    """The page must not fetch anything third-party — it loads at the roadside."""
    response = client.get("/")
    assert "fonts.googleapis.com" not in response.text
    assert "fonts.gstatic.com" not in response.text


def test_index_shows_the_share_that_waited(client, conn, config):
    for day, outcome in zip(
        fridays(4), ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)
    response = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")
    assert "75%" in response.text
    assert "ran out of room" in response.text


def test_a_sailing_that_filled_counts_in_the_headline(client, conn, config):
    """The live page showed "0% waited at least one sailing" directly above "1 filled up".
    `filled` means the vessel ran out of room; excluding it made the panel contradict
    itself in two adjacent lines."""
    import re

    seed_record(conn, config, date(2026, 7, 3), "12:30", "filled")
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    # The headline figure itself, not any "100%" in the stylesheet.
    headline = re.search(r'<span class="pct">([^<]+)</span>', body).group(1)
    assert headline == "100%", f"headline said {headline} for a sailing that filled"
    assert "filled up" in body


def test_arrival_curve_exposes_both_band_edges(client, conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1")
    response = client.get("/api/arrival-curve", params={"origin": "SLT", "time": "12:30"})
    points = response.json()["points"]
    for point in points:
        assert "p25" in point and "p75" in point


def test_pages_are_compressed(client):
    """Inline CSS is only affordable roadside if it goes over the wire compressed."""
    response = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"




# ---- Link previews --------------------------------------------------------------------
#
# The preview is deliberately generic — the logo and one line about the app, whichever
# sailing the link names. These read the served HTML rather than the preview module,
# because the failure that actually matters is a tag that never reached the page.


def _meta(html: str, key: str) -> str:
    """The content of one meta tag, by property or name."""
    import re

    match = re.search(rf'<meta (?:property|name)="{re.escape(key)}" content="([^"]*)"', html)
    assert match, f"no {key} meta tag in the response"
    return match.group(1)


def test_preview_introduces_the_app(client):
    html = client.get("/").text

    assert _meta(html, "og:title") == "FerryCast — Saltery Bay - Earls Cove"
    assert _meta(html, "og:description").startswith("On a day like today, what\u2019s the wait")
    assert _meta(html, "og:site_name") == "FerryCast"
    assert _meta(html, "twitter:card") == "summary_large_image"


def test_preview_stays_generic_whichever_sailing_the_link_names(client, conn, config):
    """A shared link keeps answering for whoever opens it, so the preview must not pin one
    sailing's numbers — a scraper caches the card, and the numbers would go stale."""
    for day, outcome in zip(
        fridays(4), ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)

    bare = client.get("/").text
    sailing = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert _meta(sailing, "og:title") == _meta(bare, "og:title")
    assert _meta(sailing, "og:description") == _meta(bare, "og:description")
    # The page still answers; only the preview is generic.
    assert "75%" in sailing
    assert "75%" not in _meta(sailing, "og:description")


def test_preview_url_is_the_bare_page(client):
    html = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert _meta(html, "og:url") == "http://testserver/"


def test_configured_base_url_wins(client, conn, config):
    """The forwarded scheme covers the common proxy; naming the origin settles the rest."""
    app = client.app
    app.state.config = replace(
        config, web=replace(config.web, base_url="https://ferrycast.example.com/")
    )

    html = client.get("/", headers={"x-forwarded-proto": "http"}).text

    assert _meta(html, "og:image") == "https://ferrycast.example.com/static/brand/og.png"
    assert _meta(html, "og:url") == "https://ferrycast.example.com/"


def test_the_share_card_is_served_at_the_size_it_claims(client):
    """og:image:width and :height are a promise; a card cropped against them looks broken."""
    import io

    from PIL import Image

    response = client.get("/static/brand/og.png")

    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as card:
        assert card.size == (1200, 630)


def test_health_page_has_its_own_preview(client):
    html = client.get("/health").text
    assert _meta(html, "og:title") == "FerryCast — pipeline health"


def test_the_empty_page_shows_what_has_been_collected(client, conn, config):
    """A blank page cannot distinguish "collecting fine" from "scraper broken"."""
    from .test_collection_status import add_departure_report

    # Earlier the same day: the distribution excludes the date being asked about, so this
    # is collected evidence without being an answer.
    seed_record(conn, config, date(2026, 8, 10), "12:30", "filled")
    add_departure_report(conn, config, date(2026, 8, 10), "12:30", "12:47")

    body = client.get("/?origin=SLT&service_date=2026-08-10&time=12:30").text
    assert "No history yet" in body        # still honest about the answer
    assert "Collected for the 12:30" in body
    assert "2026-08-10" in body            # the sailing it does know about
    assert "12:47" in body                 # when it actually went
    assert "+17" in body                   # and how far off the timetable that was


def test_the_collected_list_is_only_this_sailing(client, conn, config):
    """Unscoped, this panel was the same list on every page — evidence about none of them."""
    seed_record(conn, config, date(2026, 8, 10), "16:30", "waited_2plus")

    body = client.get("/?origin=SLT&service_date=2026-08-10&time=12:30").text
    assert "Nothing recorded for this sailing yet" in body
    assert "Most recent" not in body  # the 16:30 is not evidence about the 12:30


def test_collection_evidence_is_hidden_once_there_is_a_real_answer(client, conn, config):
    """The proof-of-life panel is scaffolding for a new install, not a permanent fixture —
    and it must never sit next to a distribution looking like a second, contradictory one."""
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "waited_1")

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "comparable sailing" in body
    assert "Collected for the" not in body


def test_the_page_never_claims_frames_for_a_deck_space_answer(client, conn, config):
    """A fixed footer used to assert every outcome was counted from webcam frames. That was
    true when frames were the only source; a deck-space answer says the opposite, and the
    two ended up one above the other. Provenance belongs to the answer, not to the page."""
    from ferrycast.aggregate import aggregate_day

    from .test_deckspace_history import add_deck_space

    for day in fridays(4):
        add_deck_space(conn, config, "SLT", day, "12:30", [(90, 50), (60, 25), (30, 0)])
        aggregate_day(conn, config, day)

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "not how many vehicles were still queued" in body  # what this evidence is
    assert "counted from terminal webcam frames" not in body  # and what it is not


# ---- The countdown ---------------------------------------------------------------------
#
# "tomorrow" is a claim about the calendar, so it has to be measured on the calendar. The
# stopwatch version — elapsed minutes divided into 24-hour blocks — agrees only when the
# departure's wall-clock time is no earlier in the day than now, and a ferry timetable
# that starts at 05:35 spends every evening in the half that disagrees.


def _countdown_at(config, monkeypatch, now_utc_value, service_date, depart_hhmm):
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: now_utc_value)
    return _countdown(config, service_date, depart_hhmm)


def test_the_day_after_tomorrow_is_not_called_tomorrow(config, monkeypatch):
    """Mon 20:00 in Vancouver, looking at Wednesday's 08:30 — 36 hours out.

    Thirty-six hours is one elapsed day and change, which is how a sailing two dates away
    came to be labelled "tomorrow"."""
    monday_evening = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)  # Mon 10 Aug, 20:00 PDT
    label = _countdown_at(config, monkeypatch, monday_evening, date(2026, 8, 12), "08:30")
    assert label == "in 2 days"


def test_tomorrow_still_means_tomorrow(config, monkeypatch):
    """Mon 08:00, Tuesday's 16:30 — 32 hours out, and genuinely the next date."""
    monday_morning = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)  # Mon 10 Aug, 08:00 PDT
    label = _countdown_at(config, monkeypatch, monday_morning, date(2026, 8, 11), "16:30")
    assert label == "tomorrow"


def test_the_countdown_stays_in_hours_inside_the_day(config, monkeypatch):
    """Under 24 hours the answer is hours, whichever date it lands on — an hour count is
    unambiguous in a way a day name is not."""
    monday_evening = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)  # Mon 10 Aug, 20:00 PDT
    label = _countdown_at(config, monkeypatch, monday_evening, date(2026, 8, 11), "08:30")
    assert label == "in 12 h 30 min"


def test_a_date_less_request_answers_about_the_route_s_today(client, monkeypatch):
    """The server runs on UTC. At 20:00 on the coast that is already tomorrow, and every
    request that did not name a date was answered about the wrong day's timetable."""
    monday_evening = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)  # Mon 10 Aug, 20:00 PDT
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: monday_evening)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: monday_evening)

    payload = client.get("/api/query?origin=SLT").json()
    assert payload["service_date"] == "2026-08-10"


def test_the_page_itself_does_not_promise_tomorrow_two_days_out(client, monkeypatch):
    """The whole path, as it reaches the phone: 20:20 on Monday, looking at Wednesday's
    first sailing. The headline read "06:30 tomorrow" for a boat leaving the day after."""
    monday_evening = datetime(2026, 8, 11, 3, 20, tzinfo=UTC)  # Mon 10 Aug, 20:20 PDT
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: monday_evening)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: monday_evening)

    body = client.get("/?origin=SLT&service_date=2026-08-12&time=08:30").text

    assert '<span class="in">in 2 days</span>' in body
    assert "tomorrow" not in body


# ---- The departure header --------------------------------------------------------------
#
# The big time is the timetable's, and the label says so. Once the vessel has gone, the
# time the board published joins it as a mirrored block — but only when the two disagree;
# an on-time boat would repeat the same number, so it gets a word instead, as does a
# departed sailing the board never timed.


def _pin_clock(monkeypatch, when):
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: when)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: when)


# Mid-afternoon on SAILED's own Friday — its 12:30 has gone, nothing else has changed.
FRIDAY_AFTERNOON = datetime(2026, 7, 3, 22, 0, tzinfo=UTC)  # Fri 3 Jul, 15:00 PDT


def test_the_departure_time_is_named_as_scheduled(client, conn, config, monkeypatch):
    _pin_clock(monkeypatch, datetime(2026, 8, 14, 17, 0, tzinfo=UTC))
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "Scheduled departure" in body
    assert "Actual departure" not in body


def test_a_late_departure_shows_the_board_s_time_beside_the_schedule(
    client, conn, config, monkeypatch
):
    from .test_reports import board_says_departed

    _pin_clock(monkeypatch, FRIDAY_AFTERNOON)
    board_says_departed(conn, config, "12:42", hhmm="12:30")

    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text

    assert "Actual departure" in body
    assert "12:42" in body
    assert "12 min late" in body


def test_an_early_departure_is_called_early(client, conn, config, monkeypatch):
    from .test_reports import board_says_departed

    _pin_clock(monkeypatch, FRIDAY_AFTERNOON)
    board_says_departed(conn, config, "12:24", hhmm="12:30")

    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text

    assert "Actual departure" in body
    assert "6 min early" in body


def test_an_on_time_departure_repeats_nothing(client, conn, config, monkeypatch):
    from .test_reports import board_says_departed

    _pin_clock(monkeypatch, FRIDAY_AFTERNOON)
    board_says_departed(conn, config, "12:30", hhmm="12:30")

    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text

    assert "Actual departure" not in body
    assert "left on time" in body


def test_a_departed_sailing_the_board_never_timed_still_says_so(
    client, conn, config, monkeypatch
):
    _pin_clock(monkeypatch, FRIDAY_AFTERNOON)
    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text
    assert "Actual departure" not in body
    assert '<span class="in">departed</span>' in body


def test_the_report_form_prefills_the_departure_from_the_board(client, conn, config):
    """Pre-filled, not removed: the board only carries today's sailings, so on any past
    date the field is the only way the time can be supplied at all."""
    from .test_reports import board_says_departed

    board_says_departed(conn, config, "12:42", hhmm="12:30")
    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text

    assert 'name="departed"' in body
    assert 'value="12:42"' in body
    assert "From the departures board" in body


def test_the_departure_field_is_empty_when_the_board_has_nothing(client, conn, config):
    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text
    assert 'name="departed"' in body
    assert "From the departures board" not in body


def test_the_form_asks_how_long_they_waited(client, conn, config):
    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text
    assert 'name="sailings_waited"' in body
    assert "Two or more" in body


def test_each_report_question_is_marked_for_the_answer_it_applies_to(client, conn, config):
    """"How full was the deck" is for somebody who was on it; "how long did you wait" is for
    somebody who missed it. Asking both of everyone invites an answer to a question the
    person could not have observed.

    The toggle itself is JavaScript, so what is pinned here is the markup it keys off — and
    that with JS off both stay visible, which the server's own validation makes safe.
    """
    body = client.get("/?origin=SLT&service_date=2026-07-03&time=12:30").text

    assert 'id="waited-field" data-when-boarded="no"' in body
    assert 'id="fullness-field" data-when-boarded="yes"' in body
    # No `hidden` attribute in the markup: before either radio is touched both apply, and a
    # form that renders half its questions is one somebody assumes they have finished.
    assert 'data-when-boarded="no" hidden' not in body


def test_the_legend_does_not_mix_the_boat_with_the_people(client, conn, config):
    """A person only waits because the vessel ran out of room, so waited_1 is a refinement
    of `filled`, not an alternative to it. Flattened, the panel printed "Filled up before
    departure 0%" directly above "Waited 1 sailing 100%" — the boat had room and somebody
    waited anyway."""
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "How the 1 went" in body
    assert "Ran out of room" in body
    assert "Left vehicles behind" in body
    assert "Of those, how long people waited" in body
    # The old flat row, which is what said 0% while someone waited.
    assert "Filled up before departure" not in body


def test_the_wait_breakdown_is_hidden_when_nothing_ran_out(client, conn, config):
    """Three zeroes answering a question nobody asked."""
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "boarded")
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "Of those, how long people waited" not in body


def test_an_unexplained_fill_says_no_one_has_said(client, conn, config):
    """The third state, drawn as neither success nor failure. "Ran out of room, and nobody
    has said whether that cost anyone their crossing" is also the page's standing invitation:
    one report from that line resolves it."""
    seed_record(conn, config, date(2026, 7, 3), "12:30", "filled")
    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text
    assert "No one has said" in body
    assert "One report from that\n        line settles it" in body
    # Nothing said how long anyone waited, so the wait question is not asked.
    assert "Of those, how long people waited" not in body


def test_a_capacity_notice_alone_puts_a_sailing_in_the_pool(client, conn, config):
    """What the separate capacity card existed to work around. The board's account of the
    deck used to be tallied beside the distribution, over its own sailings, because a notice
    fed `left_full` and never the outcome — so a sailing the board had called full sat in a
    second panel with a second denominator while the hero said it knew of nothing.

    The notice asserts `filled`, so these are simply comparable sailings that ran out of
    room, counted once."""
    from ferrycast.aggregate import aggregate_day

    for day in fridays(4):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    status_text, fetch_status)
               VALUES (?, 'SLT', ?, ?, '12:30',
                       'Peak travel. Loading maximum number of vehicles', 'ok')""",
            (config.route.id, f"{day.isoformat()}T19:22:00Z", day.isoformat()),
        )
        conn.commit()
        aggregate_day(conn, config, day)

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "No history yet" not in body
    assert "4 comparable sailings" in body
    assert "100%" in body
    assert "ran out of room" in body
    # Nobody watched the tarmac, so the page says so rather than claiming a success.
    assert "No one has said" in body
    assert "The board reported on 4 of the 4" in body


def test_the_board_and_the_camera_share_one_denominator(client, conn, config):
    """The reconciliation, end to end: the board saw three sailings fill, the camera saw two
    of those compounds empty, and one sailing nobody watched at all. Three panels with
    denominators of 3, 2 and 1 become one panel of 4 — the board supplies the width and the
    sharper claims sit inside it."""
    from ferrycast.aggregate import aggregate_day

    from .test_aggregate import _band_frames
    from .test_deckspace_history import add_deck_space

    days = fridays(4)
    for day in days[:3]:
        add_deck_space(conn, config, "SLT", day, "12:30", [(90, 50), (60, 25), (30, 0)])
        aggregate_day(conn, config, day)
    # Two of the three also have frames showing the compound emptied.
    for day in days[:2]:
        departure = combine_local(day, parse_hhmm("12:30"), config.tz)
        _band_frames(conn, config, departure, [(-30, "heavy"), (-10, "heavy"), (20, "empty")])
        aggregate_day(conn, config, day)
    # And one sailing the board never mentioned, read from frames alone.
    departure = combine_local(days[3], parse_hhmm("12:30"), config.tz)
    _band_frames(conn, config, departure, [(-30, "light"), (-10, "light"), (20, "empty")])
    aggregate_day(conn, config, days[3])

    body = client.get("/?origin=SLT&service_date=2026-08-14&time=12:30").text

    assert "4 comparable sailings" in body
    assert "75%" in body                              # three of the four ran out of room
    assert "Of the 3 that filled" in body
    assert "2 filled and still took everyone" in body
    assert "for 1 nobody has said either way" in body
    assert "The board reported on 3 of the 4" in body


def test_how_it_works_explains_the_method(client):
    body = client.get("/how-it-works").text

    assert "A record, not a forecast." in body
    # The sources, named as a reader would name them rather than as table names.
    assert "The departures board" in body
    assert "People in the line" in body
    # And what none of them can say, which is the reason the page exists.
    assert "Deck space is not the queue." in body


def test_how_it_works_quotes_this_install_s_own_thresholds(client, config):
    """The ladder is described from the running config, so the page cannot go stale against
    the search it is describing — this install stops at 3, not at the shipped 5."""
    body = client.get("/how-it-works").text

    assert config.query.min_sample == 3
    assert "3 sailings to answer from" in body
    assert f"±{config.query.time_tolerance_minutes} min" in body


def test_how_it_works_says_which_camera_is_lane_read(conn, config):
    """One end calibrated and one not is the normal state of a route, and the page says so
    per terminal rather than describing "the camera" as if there were one."""
    from .test_ondemand_lanes import _write_calibration

    _write_calibration(config, "SLT")
    app = create_app(str(config.source_path))
    with TestClient(app) as calibrated_client:
        body = calibrated_client.get("/how-it-works").text

    assert "Saltery Bay" in body
    assert "Earls Cove" in body
    # Exactly one of the two terminals is uncalibrated.
    assert body.count("not calibrated") == 1


def test_the_index_offers_the_explainer_at_the_bottom(client):
    body = client.get("/").text
    assert '<a href="/how-it-works">How it works</a>' in body


def test_the_public_pages_do_not_offer_the_pipeline_dashboard(client):
    """Capture failures and spend against budget are the operator's business, not a
    traveller's. `/health` still answers at its URL — it is simply not linked from the pages
    someone reaches while looking for a ferry."""
    for path in ["/", "/how-it-works"]:
        assert 'href="/health"' not in client.get(path).text

    assert client.get("/health").status_code == 200


def test_how_it_works_names_a_terminal_s_second_camera(tmp_path):
    """One terminal, two views: the marshalling camera and the highway camera that sees the
    overflow. A page claiming to say what this install can see has to name both."""
    from ferrycast.db import init_db

    from .test_cameras import _config_with_camera

    config = _config_with_camera(
        tmp_path,
        """
  [[route.terminals.cameras]]
  id = "drivebc244"
  name = "Hwy 101 at Egmont Road"
  kind = "queue_extent"
  image_url = "https://example.invalid/244.jpg"
""",
    )
    init_db(config.db_path).close()

    app = create_app(str(config.source_path))
    with TestClient(app) as camera_client:
        body = camera_client.get("/how-it-works").text

    assert "Hwy 101 at Egmont Road" in body
    # Said as what it answers, not as the config's word for it.
    assert "overflow" in body
    assert "queue_extent" not in body


# ---- Add to Home Screen -----------------------------------------------------------------
#
# The install sheet is a different reader from the page: it takes the name, the icon and the
# URL from the manifest, and with no manifest it guesses all three — the document title as the
# app name, and a tile with the first letter of it in place of the roundel. These read what is
# actually served, because the failure that matters is a file the install flow cannot fetch.


def test_manifest_is_served_as_a_manifest(client):
    """A manifest served as application/json is ignored, and the install flow silently falls
    back to guessing — the same outcome as shipping no manifest at all."""
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")


def test_the_page_points_at_the_manifest(client):
    assert '<link rel="manifest" href="/manifest.webmanifest">' in client.get("/").text


def test_installed_app_opens_the_bare_page(client):
    """Share is tapped from whatever sailing the visitor was reading. Without start_url the
    installed icon would pin that query string and open one past Friday for ever."""
    # Fetched from a sailing URL, exactly as the install sheet does.
    manifest = client.get(
        "/manifest.webmanifest", headers={"Referer": "/?origin=SLT&time=12:30"}
    ).json()

    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"


def test_home_screen_name_is_short_enough_to_read(client):
    """The full name carries the route; a home screen truncates at about a dozen characters,
    so the short name has to stand alone."""
    manifest = client.get("/manifest.webmanifest").json()

    assert manifest["short_name"] == "FerryCast"
    assert manifest["name"] == "FerryCast — Saltery Bay - Earls Cove"


def test_every_declared_icon_is_actually_served(client):
    """The container installs the package rather than copying src/, so an icon missing from
    package-data is a 404 in production and a lettered tile on the home screen."""
    manifest = client.get("/manifest.webmanifest").json()

    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])

    for icon in manifest["icons"]:
        served = client.get(icon["src"])
        assert served.status_code == 200, f"{icon['src']} is not served"
        assert served.headers["content-type"] == "image/png"


def test_apple_touch_icon_survives_alongside_the_manifest(client):
    """iOS versions that predate the web app install flow read only this one."""
    html = client.get("/").text

    assert '<link rel="apple-touch-icon" href="/static/brand/apple-touch-icon.png">' in html
    assert client.get("/static/brand/apple-touch-icon.png").status_code == 200
