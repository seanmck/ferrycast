from datetime import date

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
