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
