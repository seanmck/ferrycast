"""The web app manifest — what a phone installs when someone adds FerryCast to a home screen.

Separate from `preview.py` because the audiences are different. A link preview introduces the
app to someone who has never seen it; the install sheet is read by someone who has already
decided, and what it needs is a name short enough to sit under an icon, an icon set the
platform can mask to its own shape, and a start URL that is the bare page.

The start URL matters more than it looks. Share is tapped from whatever the visitor was
reading — `/?origin=SLT&service_date=2026-08-14&time=12:30` — and without a manifest the
installed app pins that query string forever, so the icon opens one dead sailing from one past
Friday instead of the app. `start_url` is what makes the installed copy keep answering.

Served from a route rather than shipped as a static file because the route's name is config,
like everything else uncertain here. The icons beside it are static: they are drawings.
"""

from __future__ import annotations

from ..config import Config
from .preview import DESCRIPTION

# What sits under the icon. The document title carries the route as well — "FerryCast —
# Saltery Bay – Earls Cove" — which a home screen truncates to about "FerryCast — Salt…", so
# the short name drops it. The full name is still on the sheet the person is reading.
SHORT_NAME = "FerryCast"

# Cream, matching the light `theme-color` in base.html. The platform paints `background_color`
# as the splash before the app's first frame, so a value that disagrees with the page shows up
# as a flash of the wrong colour on every single launch. A manifest takes one colour and not a
# light/dark pair, and the app's paper is the honest half of the palette to give it.
BACKGROUND_COLOR = "#F4EFE4"

# 192 and 512 are the sizes the install flows actually look for. The maskable copy is the same
# drawing with more air around it, for launchers that crop the icon to a circle — declared
# separately because a launcher told it may crop an `any` icon would clip the roundel's rim.
ICONS = (
    ("/static/brand/icon-192.png", "192x192", "any"),
    ("/static/brand/icon-512.png", "512x512", "any"),
    ("/static/brand/icon-512-maskable.png", "512x512", "maskable"),
)


def manifest(config: Config) -> dict:
    """The manifest document. Pure — everything in it is config or a constant."""
    return {
        # Pinned rather than derived from start_url, so that changing where the app opens
        # updates the installed copy instead of orphaning it as a second app.
        "id": "/",
        "name": f"FerryCast — {config.route.name}",
        "short_name": SHORT_NAME,
        "description": DESCRIPTION,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": BACKGROUND_COLOR,
        "theme_color": BACKGROUND_COLOR,
        "lang": "en-CA",
        "icons": [
            {"src": src, "sizes": sizes, "type": "image/png", "purpose": purpose}
            for src, sizes, purpose in ICONS
        ],
    }
