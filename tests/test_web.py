from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from ferrycast.web.app import create_app

from .test_query import fridays, seed_record


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

    response = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30")

    assert response.status_code == 200
    # The answer is in the first response, not fetched afterwards by script.
    assert "Waited 1 sailing" in response.text
    assert "4 comparable sailings" in response.text


def test_index_warns_when_the_sample_is_thin(client, conn, config):
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")
    response = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30")
    assert "Small sample" in response.text


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


def test_api_query_returns_the_distribution(client, conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1")

    response = client.get("/api/query", params={"origin": "SLT", "service_date": "2026-07-31", "time": "12:30"})

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
    response = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30")
    assert "75%" in response.text
    assert "waited at least one sailing" in response.text


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
    sailing = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30").text

    assert _meta(sailing, "og:title") == _meta(bare, "og:title")
    assert _meta(sailing, "og:description") == _meta(bare, "og:description")
    # The page still answers; only the preview is generic.
    assert "75%" in sailing
    assert "75%" not in _meta(sailing, "og:description")


def test_preview_url_is_the_bare_page(client):
    html = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30").text
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
    from .test_collection_status import add_reading

    add_reading(conn, config, minutes_ago=4)
    seed_record(conn, config, date(2026, 8, 3), "09:25", "boarded")

    body = client.get("/?origin=SLT&service_date=2026-08-10&time=09:25").text
    assert "No history yet" in body        # still honest about the answer
    assert "Last reading 4 min ago" in body
    assert "2026-08-03" in body            # the sailing it does know about


def test_a_silent_feed_is_flagged_on_the_page(client, conn, config):
    from .test_collection_status import add_reading

    add_reading(conn, config, minutes_ago=config.capture.interval_minutes * 5)
    body = client.get("/?origin=SLT&service_date=2026-08-10&time=09:25").text
    assert "older than expected" in body


def test_collection_evidence_is_hidden_once_there_is_a_real_answer(client, conn, config):
    """The proof-of-life panel is scaffolding for a new install, not a permanent fixture —
    and it must never sit next to a distribution looking like a second, contradictory one."""
    from .test_collection_status import add_reading

    add_reading(conn, config, minutes_ago=2)
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "waited_1")

    body = client.get("/?origin=SLT&service_date=2026-07-31&time=12:30").text
    assert "comparable sailing" in body
    assert "Last reading" not in body
