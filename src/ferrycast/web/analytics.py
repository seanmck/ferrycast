"""Server-side product analytics (PostHog).

What this module may claim: that a request arrived, what it asked for, and what the app
answered. Nothing else. No tag runs in the browser, so there is no autocapture, no session
replay, no scroll or click stream, and the property the README states about the page holds:
the terminal camera is still the only third party the *visitor's browser* is asked to
contact. The server talks to plenty of them — the deck-space page, the vessel tracker, the
ECCC feeds — but off the request path, and PostHog joins that list rather than the page's.
Everything counted here is already in the request being served.

The second rule is the collectors' rule one layer up: **a dead remote is not a failure**. A
camera that will not answer writes an `error` frame and the job carries on; PostHog being
unreachable, or misconfigured, or simply not installed, must in the same way cost a
traveller nothing. Every path out of here is wrapped, and the fallback is silence.

What it counts:

| event                | asks |
|---|---|
| `$pageview`          | who arrives, from where, and — for the answer page — whether there was enough history to answer them |
| `report_submitted`   | the contribution loop: someone in the line filed what happened |
| `report_rejected`    | the form was confusing enough to bounce |
| `backfill_requested` | someone paid to fill a slot's history, and what it bought |
| `ondemand_check`     | someone asked the camera to look now |
| `data_exported`      | the datasets get read by someone other than me |
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from ..config import Config

log = logging.getLogger(__name__)

# First-party, and the only cookie this app sets. It holds a random opaque id and nothing
# else — no IP, no fingerprint, nothing derived from the person — so it can say "the same
# browser came back" and cannot say who. 400 days because Chrome caps a cookie's life there
# anyway and a longer expiry would be a fiction.
COOKIE_NAME = "fc_v"
COOKIE_MAX_AGE = 400 * 24 * 60 * 60
_ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

# Traffic, not visits. `/healthz` is the container host's liveness probe: once a minute
# forever, some 44k events a month, which would outnumber the people this is meant to count
# by two orders of magnitude and cost real money to store.
IGNORED_PREFIXES = (
    "/healthz",
    "/static/",
    "/favicon.ico",
    "/manifest.webmanifest",
    "/api/docs",
    "/openapi.json",
)

# Machines, filtered here rather than in PostHog, because an unfiltered crawler inflates the
# one number this is for — how many people use it. Link-preview fetchers are on the list and
# matter more than usual for an app built to be shared into a group chat: every paste of a
# URL summons several of them, and none of them is a traveller.
_BOT_AGENTS = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|scrape|monitor|uptime|preview|fetcher|headless|"
    r"curl/|wget|python-requests|httpx|okhttp|axios|go-http|java/|libwww|lighthouse|"
    r"facebookexternalhit|whatsapp|telegram|discord|slack|twitter|linkedin|pinterest|"
    r"embedly|quora link preview|vercel|render",
    re.IGNORECASE,
)

# Copied through when present, so a link posted to the ferry Facebook group can be told
# apart from one texted between two people. Nothing else is read from the query string.
_CAMPAIGN_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


class Analytics:
    """A PostHog client, or a convincing impression of one that does nothing."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def capture(self, event: str, *, distinct_id: str, properties: dict) -> None:
        if self._client is None:
            return
        try:
            self._client.capture(event, distinct_id=distinct_id, properties=properties)
        except Exception:  # See the module docstring: silence, never a 500 for a traveller.
            log.debug("analytics capture failed for %s", event, exc_info=True)

    def shutdown(self) -> None:
        """Flush what is queued. Called once, from the app's shutdown."""
        if self._client is None:
            return
        with suppress(Exception):
            self._client.shutdown()


def build(config: Config) -> Analytics:
    """The client this config asks for, or a disabled one.

    Three ways to end up disabled, all of them quiet and none of them an error: no key in
    the environment (the default, and what every fork and every test gets), the kill switch
    thrown, or the SDK absent from the image.
    """
    settings = config.analytics
    if not settings.configured:
        return Analytics(None)
    try:
        from posthog import Posthog
    except ImportError:
        log.warning("FERRYCAST_POSTHOG_KEY is set but the `posthog` package is not installed")
        return Analytics(None)

    return Analytics(
        Posthog(
            project_api_key=settings.api_key,
            host=settings.host,
            # The address PostHog would see is this container's, so a location derived from
            # it says Ashburn for every traveller on the Sunshine Coast. No answer beats a
            # confidently wrong one — the same rule the records follow.
            disable_geoip=True,
            # Queue and flush on a background thread. A page must never wait on this.
            sync_mode=False,
            timeout=5,
            on_error=lambda error, batch: log.debug("analytics delivery failed: %s", error),
        )
    )


class Visit:
    """One request's worth of analytics: who is asking, and what they were shown.

    Handlers reach for this rather than the client, so the visitor id and the request
    properties are worked out once and every event from the request agrees about them.
    """

    def __init__(self, analytics: Analytics, request: Request) -> None:
        self._analytics = analytics
        self._request = request
        self.distinct_id, self.is_new = _visitor_id(request)
        self.extra: dict = {}

    def annotate(self, **properties) -> None:
        """Add what only the handler knows to this request's pageview.

        The middleware captures the pageview *after* the handler returns, so a route can
        say here what it could only find out by answering — above all whether there was
        enough comparable history to give a real answer, which is the question this whole
        project exists to keep honest.
        """
        self.extra.update(properties)

    def capture(self, event: str, **properties) -> None:
        self._analytics.capture(
            event,
            distinct_id=self.distinct_id,
            properties={**_request_properties(self._request), **properties},
        )


class _NoVisit:
    """What handlers get when analytics is off, so they need no branch of their own."""

    distinct_id = ""
    is_new = False

    def annotate(self, **properties) -> None:
        pass

    def capture(self, event: str, **properties) -> None:
        pass


NO_VISIT = _NoVisit()


def visit(request: Request) -> Visit | _NoVisit:
    """This request's analytics handle. Never None, never raises."""
    return getattr(request.state, "analytics_visit", NO_VISIT)


def _visitor_id(request: Request) -> tuple[str, bool]:
    """(id, whether it is new). A returning browser brings its own; anyone else gets one."""
    existing = request.cookies.get(COOKIE_NAME, "")
    if _ID_PATTERN.match(existing):
        return existing, False
    # Validated rather than trusted: the cookie is client-supplied, and an unchecked value
    # is an arbitrary string of someone else's choosing landing in the distinct_id column.
    return uuid4().hex, True


def _request_properties(request: Request) -> dict:
    referrer = request.headers.get("referer", "")
    properties = {
        "$current_url": str(request.url),
        "$pathname": request.url.path,
        "$host": request.url.hostname or "",
        # PostHog derives browser, OS and device type from this, which is the whole of what
        # a server-side install can know about the phone — and enough to answer "is this
        # thing being read on a phone in a queue", which is what it was built for.
        "$raw_user_agent": request.headers.get("user-agent", ""),
        "$referrer": referrer or "$direct",
        "$referring_domain": urlsplit(referrer).netloc or "$direct",
        "route": _route_id(request),
    }
    for param in _CAMPAIGN_PARAMS:
        value = request.query_params.get(param)
        if value:
            properties[param] = value[:200]
    return properties


def _route_id(request: Request) -> str:
    config = getattr(request.app.state, "config", None)
    return config.active_route_id if config else ""


def _is_bot(request: Request) -> bool:
    agent = request.headers.get("user-agent", "")
    # A request with no user agent at all is a script or a probe, never a browser.
    return not agent or bool(_BOT_AGENTS.search(agent))


class AnalyticsMiddleware(BaseHTTPMiddleware):
    """Attaches a `Visit` to every request, and captures the pageview once it is answered.

    After rather than before, for two reasons: the status code is only known then, and the
    handler has had its chance to say what it found (see `Visit.annotate`).
    """

    def __init__(self, app, analytics: Analytics) -> None:
        super().__init__(app)
        self._analytics = analytics

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(IGNORED_PREFIXES) or _is_bot(request):
            return await call_next(request)

        current = Visit(self._analytics, request)
        request.state.analytics_visit = current
        response = await call_next(request)

        # A pageview is a person looking at a page: a GET that returned a document. A POST
        # renders one too, but what happened there was the report or the fill, and those
        # say so themselves — counting a second pageview for them would double the traffic
        # of exactly the people who did the most.
        is_page = request.method == "GET" and response.headers.get("content-type", "").startswith(
            "text/html"
        )
        if is_page:
            current.capture(
                "$pageview", status_code=response.status_code, **current.extra
            )
            if current.is_new:
                response.set_cookie(
                    COOKIE_NAME,
                    current.distinct_id,
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                    # The app is served over TLS in production and over plain HTTP on a
                    # laptop; marking it secure unconditionally would mean the cookie is
                    # never stored locally and every local pageview looks like a new person.
                    secure=request.url.scheme == "https",
                )
        return response


def install(app, config: Config) -> Analytics:
    """Wire analytics into an app, returning the client so it can be shut down.

    Nothing is installed when it is not configured — no middleware, no per-request object,
    no cookie. An install without a key behaves exactly as it did before this module.
    """
    analytics = build(config)
    if analytics.enabled:
        app.add_middleware(AnalyticsMiddleware, analytics=analytics)
    return analytics
