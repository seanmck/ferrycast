"""The "fill in this slot's history" button.

This is the one control on the page that spends money, so the tests are mostly about what
it refuses to do: appear when disabled, appear when there is nothing to buy, appear when the
answer is already current, or spend past the day's cap.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ferrycast.web.app import create_app

from .test_backfill import add_frames, add_sailing
from .test_query import PLAIN_FRIDAY, fridays, seed_record


@pytest.fixture
def enabled(conn, config):
    """A client with on-demand backfill turned on."""
    app = create_app(str(config.source_path))
    app.state.config = replace(
        config, web=replace(config.web, allow_on_demand_backfill=True)
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(conn, config):
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        yield test_client


def _fridays_with_frames(conn, config, count=3):
    days = [d for d in fridays(count + 3) if d < PLAIN_FRIDAY][:count]
    for day in days:
        add_sailing(conn, config, day, "12:30")
        add_frames(conn, config, day, "12:30")
    return days


SLOT = f"/?origin=SLT&service_date={PLAIN_FRIDAY.isoformat()}&time=12:30"


def test_the_button_does_not_appear_when_the_feature_is_off(client, conn, config):
    """A disabled deployment must never show a control that would 403."""
    _fridays_with_frames(conn, config)
    assert "/fill" not in client.get(SLOT).text


def test_the_button_appears_when_frames_are_waiting(enabled, conn, config):
    _fridays_with_frames(conn, config)
    body = enabled.get(SLOT).text
    assert 'action="/fill"' in body
    assert "Deepen this answer" in body


def test_the_button_states_the_work_before_it_is_tapped(enabled, conn, config):
    """How much it will read, so the tap is not blind."""
    _fridays_with_frames(conn, config, count=2)
    body = enabled.get(SLOT).text
    # 2 sailings x 4 essential offsets = 8 frames.
    assert "Read 8 frames" in body


def test_the_page_never_talks_about_money(enabled, conn, config, monkeypatch):
    """Spend is the operator's business and belongs on /health, not on the page someone
    reads at the terminal. A price on a trivial action made it read as a purchase."""
    _fridays_with_frames(conn, config, count=2)

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        stats = ExtractionStats()
        stats.extracted = len(frames)
        stats.cost_usd = 0.008
        return stats

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)

    offered = enabled.get(SLOT).text
    filled = enabled.post(
        "/fill",
        data={"origin": "SLT", "service_date": PLAIN_FRIDAY.isoformat(), "time": "12:30"},
    ).text

    import re

    for body in (offered, filled):
        # Strip <script> and <style>: the chart is full of JS `${...}` literals and the
        # inline CSS has a comment about repaint *cost* — neither is money. ("cent" is out
        # of the word list for the same reason: it is inside "recent".)
        visible = re.sub(r"(?s)<script.*?</script>|<style.*?</style>", "", body)
        for word in ("$", "cost", "budget", "spend", "¢", "usd"):
            assert word not in visible.lower(), f"{word!r} on the page"


def test_no_button_when_there_is_nothing_to_buy(enabled, conn, config):
    """Sailings exist and are comparable, but no frames were ever archived for them."""
    for day in fridays(3):
        add_sailing(conn, config, day, "12:30")   # no frames
    assert 'action="/fill"' not in enabled.get(SLOT).text


def test_no_button_once_every_comparable_sailing_has_a_record(enabled, conn, config):
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "boarded")
    assert 'action="/fill"' not in enabled.get(SLOT).text


def test_a_sufficient_answer_is_still_offered_newer_sailings(enabled, conn, config):
    """Without this the distribution freezes at whatever the first fill produced and ages
    silently — still five sailings, still "sufficient", quietly describing a past season."""
    for day in fridays(5):
        seed_record(conn, config, day, "12:30", "boarded")
    newer = max(fridays(5)) + timedelta(days=7)
    add_sailing(conn, config, newer, "12:30")
    add_frames(conn, config, newer, "12:30")

    body = enabled.get(
        f"/?origin=SLT&service_date={(newer + timedelta(days=7)).isoformat()}&time=12:30"
    ).text
    assert "Newer sailings available" in body


def test_posting_when_disabled_is_refused(client, conn, config):
    response = client.post(
        "/fill",
        data={"origin": "SLT", "service_date": PLAIN_FRIDAY.isoformat(), "time": "12:30"},
    )
    assert response.status_code == 403
    assert "allow_on_demand_backfill" in response.text


def test_a_fill_reports_what_it_bought(enabled, conn, config, monkeypatch):
    """The result is rendered, not redirected away: the point of the tap is to see what it
    bought, and a redirect would drop the line the person just paid for."""
    _fridays_with_frames(conn, config, count=2)

    def fake_extract(conn_, config_, frames, **kw):
        from ferrycast.vision import ExtractionStats

        stats = ExtractionStats()
        stats.extracted = len(frames)
        stats.cost_usd = 0.001 * len(frames)
        return stats

    monkeypatch.setattr("ferrycast.vision.extract_frames", fake_extract)
    body = enabled.post(
        "/fill",
        data={"origin": "SLT", "service_date": PLAIN_FRIDAY.isoformat(), "time": "12:30"},
    ).text

    assert "Filled" in body
    assert "8 frames" in body


def test_the_daily_cap_stops_the_button_spending(enabled, conn, config, monkeypatch):
    """One slot must not be able to exhaust the day, and the message has to say so."""
    _fridays_with_frames(conn, config)
    capped = replace(
        enabled.app.state.config,
        web=replace(enabled.app.state.config.web, backfill_daily_frame_cap=0),
    )
    enabled.app.state.config = capped

    called = []
    monkeypatch.setattr(
        "ferrycast.vision.extract_frames", lambda *a, **k: called.append(1)
    )
    body = enabled.post(
        "/fill",
        data={"origin": "SLT", "service_date": PLAIN_FRIDAY.isoformat(), "time": "12:30"},
    ).text

    assert called == [], "the cap must be checked before anything is read"
    assert "daily limit" in body
