"""Server-side analytics.

Two properties matter more than the events themselves, and both are here: that analytics
can never cost a traveller their answer, and that it cannot count things that are not
people. The third is honesty — a pageview has to carry whether the page actually answered,
because "how often does this thing say nobody knows" is the question the whole project is
built around and it is invisible in raw traffic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from ferrycast.config import ConfigError, load_config
from ferrycast.web import analytics
from ferrycast.web.app import create_app

from .test_query import fridays, seed_record

# A Friday that has been and gone, with a 12:30 from Saltery Bay in the fixture timetable.
SAILED = date(2026, 7, 3)
NOW = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)


class FakeClient:
    """Stands in for `posthog.Posthog` — the same three calls, none of the network."""

    def __init__(self, explode: bool = False):
        self.events: list[tuple[str, str, dict]] = []
        self.explode = explode
        self.closed = False

    def capture(self, event, *, distinct_id, properties):
        if self.explode:
            raise RuntimeError("posthog is having a day")
        self.events.append((event, distinct_id, properties))

    def shutdown(self):
        self.closed = True

    # Test-side conveniences.
    def named(self, event: str) -> list[dict]:
        return [properties for name, _, properties in self.events if name == event]

    @property
    def names(self) -> list[str]:
        return [name for name, _, _ in self.events]

    @property
    def visitors(self) -> set[str]:
        return {distinct_id for _, distinct_id, _ in self.events}


@pytest.fixture
def posthog(monkeypatch):
    """Wire a fake client in, as though a key were present in the environment."""
    client = FakeClient()
    monkeypatch.setattr(analytics, "build", lambda config: analytics.Analytics(client))
    return client


@pytest.fixture
def client(conn, config, monkeypatch, posthog):
    monkeypatch.setattr("ferrycast.reports.now_utc", lambda: NOW)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: NOW)
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: NOW)
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        yield test_client


def _form(**overrides) -> dict:
    body = {
        "origin": "SLT",
        "service_date": SAILED.isoformat(),
        "time": "12:30",
        "boarded": "yes",
        "joined": "11:55",
        "deck_fullness": "nearly_full",
    }
    body.update(overrides)
    return body


# --- off by default ----------------------------------------------------------------


def test_an_install_without_a_key_captures_nothing(conn, config):
    """The default, and what every fork and every deploy that never sets a key gets.

    No middleware, no cookie, no import of the SDK — and the page is exactly the page it
    was before this module existed.
    """
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        response = test_client.get("/")

    assert response.status_code == 200
    assert app.state.analytics.enabled is False
    assert "fc_v" not in response.cookies


def test_the_key_may_not_be_written_into_the_config_file(tmp_path, config):
    """It is committed into the image, so a key there would ship to every fork of it."""
    path = tmp_path / "with-key.toml"
    path.write_text(
        config.source_path.read_text() + '\n[analytics]\napi_key = "phc_secret"\n'
    )

    with pytest.raises(ConfigError, match="FERRYCAST_POSTHOG_KEY"):
        load_config(path)


def test_the_kill_switch_works_without_removing_the_key(monkeypatch, config):
    """A deploy has to be able to stop reporting without a commit, a build and a release."""
    monkeypatch.setenv("FERRYCAST_POSTHOG_KEY", "phc_live")
    assert load_config(config.source_path).analytics.configured is True

    monkeypatch.setenv("FERRYCAST_ANALYTICS", "off")
    assert load_config(config.source_path).analytics.configured is False


# --- never at the traveller's expense ----------------------------------------------


def test_a_broken_analytics_backend_still_serves_the_page(conn, config, monkeypatch):
    """The collectors' rule, one layer up: a dead remote is data, never a failure."""
    exploding = FakeClient(explode=True)
    monkeypatch.setattr(analytics, "build", lambda cfg: analytics.Analytics(exploding))

    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        response = test_client.get("/")

    assert response.status_code == 200
    assert "FerryCast" in response.text


def test_shutdown_flushes_what_is_queued(client, posthog):
    """The queue lives on a background thread, so an unflushed exit loses it."""
    client.get("/")
    assert posthog.closed is False
    client.__exit__(None, None, None)  # the lifespan's shutdown half
    assert posthog.closed is True


# --- counting people, not traffic --------------------------------------------------


def test_the_liveness_probe_is_not_a_visit(client, posthog):
    """Once a minute forever would outnumber the people this is meant to count."""
    assert client.get("/healthz").status_code == 200
    assert posthog.names == []


def test_crawlers_and_link_previewers_are_not_visits(client, posthog):
    """An app built to be pasted into a group chat summons these by the handful."""
    for agent in (
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "facebookexternalhit/1.1",
        "WhatsApp/2.23",
        "python-requests/2.31.0",
    ):
        client.get("/", headers={"user-agent": agent})

    assert posthog.names == []


def test_a_browser_is_counted_once_and_then_recognised(client, posthog):
    """Two pageviews from one phone are one person — that is the whole job of the cookie."""
    first = client.get("/", headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1"})
    assert first.cookies["fc_v"]

    # TestClient carries the cookie back, as a browser would.
    second = client.get("/?time=12:30", headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1"})

    assert posthog.names == ["$pageview", "$pageview"]
    assert len(posthog.visitors) == 1
    # Only minted once: re-setting it on every response would be noise on every document.
    assert "set-cookie" not in second.headers


def test_the_visitor_cookie_holds_nothing_but_an_opaque_id(client):
    response = client.get("/", headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1"})

    header = response.headers["set-cookie"]
    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    assert len(response.cookies["fc_v"]) == 32


def test_a_forged_cookie_cannot_choose_its_own_id(client, posthog):
    """It is client-supplied, so an unchecked value is a string of someone else's choosing."""
    client.get(
        "/",
        headers={"user-agent": "Mozilla/5.0 (iPhone) Safari/604.1", "cookie": "fc_v=' OR 1=1--"},
    )

    (distinct_id,) = posthog.visitors
    assert distinct_id != "' OR 1=1--"
    assert len(distinct_id) == 32


# --- what the events say -----------------------------------------------------------


def test_a_pageview_says_whether_the_page_could_answer(client, posthog, conn, config):
    """This app's failure is not a crash — it is arriving and being told nobody knows."""
    for day, outcome in zip(
        fridays(4), ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True
    ):
        seed_record(conn, config, day, "12:30", outcome)

    client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")

    (view,) = posthog.named("$pageview")
    assert view["answered"] is True
    assert view["sample_size"] == 4
    assert view["origin"] == "SLT"
    assert view["depart_hhmm"] == "12:30"
    assert view["day_type"] == "friday"
    assert view["route"] == "route7"


def test_a_pageview_with_no_history_behind_it_says_so(client, posthog):
    client.get("/?origin=SLT&service_date=2026-08-14&time=12:30")

    (view,) = posthog.named("$pageview")
    assert view["answered"] is False
    assert view["sample_size"] == 0


def test_a_pageview_carries_the_campaign_a_link_was_shared_with(client, posthog):
    """So a paste into the ferry Facebook group can be told from one texted to a friend."""
    client.get("/?utm_source=facebook&utm_campaign=coast-group")

    (view,) = posthog.named("$pageview")
    assert view["utm_source"] == "facebook"
    assert view["utm_campaign"] == "coast-group"


def test_a_filed_report_is_counted_as_what_it_said(client, posthog):
    response = client.post("/report", data=_form(boarded="no"), follow_redirects=False)
    assert response.status_code == 303

    (report,) = posthog.named("report_submitted")
    assert report["boarded"] is False
    assert report["deck_fullness"] == "nearly_full"
    assert report["origin"] == "SLT"
    # The redirect that follows is not a second visit — the report already said this.
    assert posthog.names == ["report_submitted"]


def test_a_bounced_report_records_why_it_bounced(client, posthog):
    """A form that quietly turns people away is the cheapest way to lose evidence."""
    response = client.post("/report", data=_form(boarded=""))

    assert response.status_code == 400
    (rejected,) = posthog.named("report_rejected")
    assert "whether you got on" in rejected["reason"]


def test_an_export_is_counted(client, posthog):
    client.get("/export/sailings.csv")

    (export,) = posthog.named("data_exported")
    assert export["dataset"] == "sailings"
    assert export["format"] == "csv"


# --- whose machine is this ---------------------------------------------------------

# Real strings, because a hand-written user agent proves only that the regex matches the
# regex. Every family here impersonates an older one — Edge claims to be Chrome, which
# claims to be Safari, which claims to be Mozilla — and that is the whole difficulty.
AGENTS = [
    (
        "iphone-safari",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        ("Safari", "iOS", "Mobile"),
    ),
    (
        "android-chrome",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36",
        ("Chrome", "Android", "Mobile"),
    ),
    (
        "ipad-safari",
        "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        ("Safari", "iOS", "Tablet"),
    ),
    (
        "android-tablet",
        "Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36",
        ("Chrome", "Android", "Tablet"),
    ),
    (
        # The one actually seen in production, and the one that read as `Linux` before this.
        "windows-edge",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0",
        ("Microsoft Edge", "Windows", "Desktop"),
    ),
    (
        "mac-firefox",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
        ("Firefox", "Mac OS X", "Desktop"),
    ),
    (
        "samsung-internet",
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
        ("Samsung Internet", "Android", "Mobile"),
    ),
]


@pytest.mark.parametrize(
    ("agent", "expected"),
    [pytest.param(agent, expected, id=name) for name, agent, expected in AGENTS],
)
def test_the_user_agent_is_read_into_browser_os_and_device(agent, expected):
    read = analytics._visitor_agent(agent)

    assert (read["$browser"], read["visitor_os"], read["$device_type"]) == expected


def test_an_unreadable_user_agent_says_unknown_rather_than_guessing():
    """The house rule, applied to a header: not knowing is an answer and gets said.

    Left off instead, it would land in a breakdown as an empty bucket, which reads like a
    reporting fault rather than like the honest gap it is.
    """
    read = analytics._visitor_agent("Mozilla/5.0 (compatible)")

    assert read == {
        "$browser": analytics.UNKNOWN,
        "visitor_os": analytics.UNKNOWN,
        "$device_type": analytics.UNKNOWN,
    }


def test_the_visitors_os_never_travels_as_dollar_os(client, posthog):
    """`$os` belongs to the SDK, which fills it from `platform` — the container's.

    It merges its own system context *over* whatever is passed here, so sending `$os` would
    be silently dropped and reading it back would report the server's OS for every traveller.
    The honest value therefore travels under a name the SDK does not claim.
    """
    client.get("/", headers={"user-agent": AGENTS[0][1]})

    (view,) = posthog.named("$pageview")
    assert view["visitor_os"] == "iOS"
    assert "$os" not in view


def test_a_pageview_says_it_was_read_on_a_phone(client, posthog):
    """The question the module was built for, and the one it could not answer before."""
    client.get("/", headers={"user-agent": AGENTS[0][1]})

    (view,) = posthog.named("$pageview")
    assert view["$device_type"] == "Mobile"
    assert view["$browser"] == "Safari"
    # The evidence travels with the reading, so a count that looks wrong can be argued with.
    assert view["$raw_user_agent"] == AGENTS[0][1]
