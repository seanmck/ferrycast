"""Open Graph metadata — what a FerryCast link looks like when someone sends it.

A link to this app is almost always sent about *one sailing*: "we're aiming for the 12:30,
here". So the preview answers for that sailing rather than describing the app — the title
carries the departure, the description carries the number, and both are built from the same
distribution the page itself renders. Someone reading the message in a car gets the answer
without opening anything.

The card image is static and served from /static: rasterising type is not something this
deployment can do at request time, and a scraper that has to wait would show nothing at all.
It is drawn offline from assets/og-card.html — see assets/README.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from starlette.requests import Request

from ..config import Config

IMAGE_PATH = "/static/og.png"
IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 630

SITE_NAME = "FerryCast"

# What the app is, in one clause, for the previews that have no sailing to report on.
TAGLINE = (
    "FerryCast records whether each sailing ran out of room, and when — the part BC Ferries' "
    "deck space leaves out."
)

# Chat clients truncate somewhere around here, and a sentence cut mid-word looks broken.
MAX_DESCRIPTION = 300


@dataclass(frozen=True)
class Preview:
    """Everything the head block needs. One object so a template cannot half-set it."""

    title: str
    description: str
    url: str
    image: str
    image_alt: str


def absolute(request: Request, config: Config, path: str) -> str:
    """Make `path` absolute. Scrapers reject relative og:url and og:image outright.

    A platform proxy terminates TLS, so the scheme this process sees is http even though
    every URL anyone can share is https — and a card whose image is http on an https page
    is dropped as mixed content. `[web] base_url` (or FERRYCAST_BASE_URL) settles it
    outright; failing that, the forwarded scheme is trusted over the local one.
    """
    base = config.web.base_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        if forwarded in ("http", "https"):
            _, _, host_and_path = base.partition("://")
            base = f"{forwarded}://{host_and_path}"
    return f"{base}{path}"


def _pretty_date(day: date) -> str:
    """"Fri 31 Jul" — no %-d, which is not portable."""
    return f"{day:%a} {day.day} {day:%b}"


def _clamp(sentences: list[str]) -> str:
    """Drop whole trailing sentences rather than let a client cut one in half."""
    while len(" ".join(sentences)) > MAX_DESCRIPTION and len(sentences) > 1:
        sentences.pop()
    return " ".join(sentences)


def _describe(distribution: dict | None, *, origin_name: str, when: str, has_sailing: bool) -> str:
    if not has_sailing:
        return f"No departures from {origin_name} on {when}. {TAGLINE}"
    if not distribution or not distribution["n"]:
        return f"No comparable sailings recorded for this one yet. {TAGLINE}"

    n = distribution["n"]
    sailings = "sailing" if n == 1 else "sailings"
    shares = distribution["shares"]
    waited = shares.get("waited_1", 0.0) + shares.get("waited_2plus", 0.0)
    percent = round(waited * 100)

    # Rounding is the enemy of trust here: "0% waited" next to a sailing that did wait, or
    # "100%" next to one that didn't, is the kind of thing that gets an app deleted. Anchor
    # the two absolute claims on the count, not on the rounded share.
    waited_count = distribution["counts"].get("waited_1", 0) + distribution["counts"].get(
        "waited_2plus", 0
    )
    if waited_count == 0:
        opening = f"None of the {n} comparable {sailings} had to wait."
    elif waited_count == n:
        opening = f"All {n} comparable {sailings} waited at least one sailing."
    else:
        opening = f"{percent}% of {n} comparable {sailings} waited at least one sailing."

    sentences = [opening]

    minutes = distribution.get("typical_fill_minutes_before")
    if minutes is not None and distribution.get("typical_fill_local"):
        sentences.append(
            f"They typically ran out of room {minutes} min before departure — "
            f"be in the lineup by {distribution['typical_fill_local']}."
        )
    if not distribution.get("sufficient", True):
        sentences.append("Small sample, so treat it as a hint rather than a forecast.")

    return _clamp(sentences)


def _image_alt(config: Config) -> str:
    """Describe the card, not the sailing — the image is the same on every page."""
    return (
        f"The FerryCast card: {config.route.name} in cream on navy, above the colour ramp "
        "the app uses to show how comparable past sailings went."
    )


def index_preview(
    request: Request,
    config: Config,
    *,
    origin: str,
    service_date: str,
    selected_time: str | None,
    distribution: dict | None,
) -> Preview:
    route = config.route
    origin_name = route.terminal(origin).name
    destination_name = route.terminal(route.destinations[origin]).name
    when = _pretty_date(date.fromisoformat(service_date))

    query = {"origin": origin, "service_date": service_date}
    if selected_time:
        query["time"] = selected_time
        title = f"{selected_time} {origin_name} → {destination_name} · {when}"
    else:
        title = f"{origin_name} → {destination_name} · {when}"

    return Preview(
        title=title,
        description=_describe(
            distribution,
            origin_name=origin_name,
            when=when,
            has_sailing=bool(selected_time),
        ),
        # Not request.url: the shared link is often bare "/", and pinning the preview to the
        # sailing it actually describes means resharing from the card keeps that sailing.
        # `safe=":"` leaves 12:30 legible in the clients that print the URL under the card.
        url=absolute(request, config, "/?" + urlencode(query, safe=":")),
        image=absolute(request, config, IMAGE_PATH),
        image_alt=_image_alt(config),
    )


def health_preview(request: Request, config: Config) -> Preview:
    return Preview(
        title="FerryCast — pipeline health",
        description=(
            "Capture, extraction and season coverage for the FerryCast dataset. "
            "How much of the record is actually there."
        ),
        url=absolute(request, config, "/health"),
        image=absolute(request, config, IMAGE_PATH),
        image_alt=_image_alt(config),
    )
