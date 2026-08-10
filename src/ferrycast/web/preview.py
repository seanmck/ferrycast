"""Open Graph metadata — what a FerryCast link looks like when someone sends it.

Deliberately generic: the same card and the same sentence whichever sailing the link happens
to name. A preview is the app introducing itself to someone who may never have seen it, and
the logo does that better than a rendering of the answer would. The answer is on the page.

What is per-request is only the part that has to be: og:url and og:image must be absolute,
and the app cannot always work out its own origin. See `absolute`.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request

from ..config import Config

IMAGE_PATH = "/static/brand/og.png"
IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 630

# The lockup is drawn on cream by brand/build_assets.py, never composited at request time.
IMAGE_ALT = (
    "The FerryCast logo: a clock roundel with a ferry sailing across it, above the wordmark "
    "and the route line."
)

# A typographic apostrophe, not a straight one: the straight one is escaped to &#39; in the
# attribute, and a few unfurlers print the entity rather than the character.
DESCRIPTION = (
    "On a day like today, what’s the wait for the ferry? FerryCast answers from comparable "
    "past sailings — whether each one ran out of room, and when."
)


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


def index_preview(request: Request, config: Config) -> Preview:
    return Preview(
        title=f"FerryCast — {config.route.name}",
        description=DESCRIPTION,
        # The bare page, not the sailing the request resolved to: a shared link should keep
        # answering for whoever opens it rather than pinning whatever was next when it was
        # sent, and a preview cached against one sailing's URL would go stale by the hour.
        url=absolute(request, config, "/"),
        image=absolute(request, config, IMAGE_PATH),
        image_alt=IMAGE_ALT,
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
        image_alt=IMAGE_ALT,
    )
