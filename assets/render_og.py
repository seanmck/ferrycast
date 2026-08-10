#!/usr/bin/env python3
"""Rasterise assets/og-card.html into the share card the web app serves.

    python assets/render_og.py

Run this only when the card design or the route it names changes; the PNG it writes is
committed, because the running app has no rasteriser and a link preview cannot wait on one.

Headless Chrome does the drawing rather than an image library: the card uses the same
self-hosted woff2 faces as the app, and getting Instrument Serif to look like itself matters
more here than avoiding a build-time dependency that nothing else needs.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "og-card.html"
OUTPUT = ROOT / "src" / "ferrycast" / "web" / "static" / "og.png"
WIDTH, HEIGHT = 1200, 630

# --window-size counts the browser's own furniture, so the live viewport is shorter than
# the number given and the last hundred pixels of the shot come back unpainted. How much
# shorter depends on the build, so the card is rendered with slack and cropped to size
# here — the one way to get exactly 1200x630 out of any Chrome.
SLACK = 270

# Playwright's bundled build is checked first: a container that already has it (the
# development one does) needs nothing installed.
CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser() -> str:
    for candidate in CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    for chrome in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(chrome)
    raise SystemExit(
        "no Chrome or Chromium found. Install one, or point CANDIDATES at your binary."
    )


def main() -> int:
    browser = find_browser()
    with tempfile.TemporaryDirectory() as work:
        shot = Path(work) / "shot.png"
        subprocess.run(
            [
                browser,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                # Each file:// document is its own origin in Chromium, so the @font-face
                # requests are cross-origin without this and the card renders in Times.
                "--allow-file-access-from-files",
                f"--user-data-dir={work}/profile",
                f"--window-size={WIDTH},{HEIGHT + SLACK}",
                f"--screenshot={shot}",
                SOURCE.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        with Image.open(shot) as image:
            card = image.convert("RGB").crop((0, 0, WIDTH, HEIGHT))
            card.save(OUTPUT, format="PNG", optimize=True)

    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({WIDTH}x{HEIGHT}, {size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
