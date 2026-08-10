"""Cut the FerryCast brand assets out of the master logo render.

The master is a 1254px square drawn on a white ground. Everything the app ships is derived
from it here rather than trimmed by hand, so the crops stay reproducible if the master is
ever re-rendered.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

HERE = Path(__file__).parent
MASTER = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "logo-master.png"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "src/ferrycast/web/static/brand"
OUT.mkdir(parents=True, exist_ok=True)

CHART = (244, 239, 228)  # --chart, the app's cream ground

# Bands measured off the master by horizontal projection.
MARK = (249, 86, 999, 893)          # clock roundel + ferry + waves
LOCKUP = (212, 86, 1044, 1161)      # everything, down to the route line

GROUND = 250   # at or above this the master is bare paper
INK = 205      # at or below it is solid ink; between the two is an antialiased rim


def knock_out_background(im: Image.Image) -> Image.Image:
    """Make the *outside* transparent, leaving white ink alone.

    A plain white-to-alpha pass would eat the ferry's superstructure and the clock face,
    which are the same white as the ground. So white is only removed where it is reachable
    from the border: a flood fill over near-white pixels marks the outside, and alpha is
    applied to that region alone. Within it the alpha ramps rather than snapping, so the
    antialiased rim of every stroke survives instead of leaving a hard, haloed edge.
    """
    im = im.convert("RGB")
    w, h = im.size
    lum = im.convert("L")
    lum_px = lum.load()
    ramp = lum.point(
        lambda v: 0 if v >= GROUND else (255 if v <= INK else round(255 * (GROUND - v) / (GROUND - INK)))
    ).load()

    outside = bytearray(w * h)
    q = deque()
    border = [(x, y) for x in range(w) for y in (0, h - 1)]
    border += [(x, y) for y in range(h) for x in (0, w - 1)]
    for x, y in border:
        if lum_px[x, y] > INK and not outside[y * w + x]:
            outside[y * w + x] = 1
            q.append((x, y))

    while q:
        x, y = q.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < w and 0 <= ny < h and not outside[ny * w + nx] and lum_px[nx, ny] > INK:
                outside[ny * w + nx] = 1
                q.append((nx, ny))

    out = im.convert("RGBA")
    px = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if outside[row + x]:
                r, g, b, _ = px[x, y]
                px[x, y] = (r, g, b, ramp[x, y])
    return out


def square(im: Image.Image, pad: float = 0.06) -> Image.Image:
    """Centre a mark in a square canvas with a little air around it."""
    im = im.crop(im.getbbox())
    side = round(max(im.size) * (1 + 2 * pad))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    return canvas


def on_cream(im: Image.Image) -> Image.Image:
    plate = Image.new("RGBA", im.size, (*CHART, 255))
    plate.alpha_composite(im)
    return plate.convert("RGB")


def save(im: Image.Image, name: str, colors: int | None = None) -> None:
    """Write a PNG, palettised where the artwork is flat enough to take it.

    The master is a render, so its flat fields carry a little noise; left as truecolour it
    compresses badly for what it is. Quantising costs nothing visible on this artwork and
    takes the served files to a fraction of the size — which is the whole point on a page
    that has to paint on one bar of signal.
    """
    if colors:
        im = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    path = OUT / name
    im.save(path, optimize=True)
    print(f"{name:22} {im.size[0]:>4}x{im.size[1]:<5} {path.stat().st_size / 1024:6.1f} KB")


master = Image.open(MASTER).convert("RGB")
cut = knock_out_background(master)

mark = square(cut.crop(MARK))
lockup = cut.crop(LOCKUP)
lockup = lockup.crop(lockup.getbbox())


def scaled(im: Image.Image, width: int) -> Image.Image:
    return im.resize((width, round(width * im.height / im.width)), Image.LANCZOS)


# 128px covers the masthead mark at 3x on a phone, so it ships as a single file.
save(mark.resize((128, 128), Image.LANCZOS), "mark.png", colors=128)
save(scaled(lockup, 640), "logo.png", colors=128)

# iOS composites its own ground behind a transparent icon, so hand it ours.
save(on_cream(square(cut.crop(MARK), pad=0.10).resize((180, 180), Image.LANCZOS)),
     "apple-touch-icon.png", colors=128)

fav = OUT / "favicon.ico"
mark.resize((48, 48), Image.LANCZOS).save(fav, sizes=[(16, 16), (32, 32), (48, 48)])
print(f"{'favicon.ico':22} {'16/32/48':10} {fav.stat().st_size / 1024:6.1f} KB")

# og.png is not cut here. The share card is typeset — it carries the route and the outcome
# ramp in the app's own faces, which Pillow cannot set — so it is drawn by render_card.py
# from og-card.html, placing the mark.png written just above. Run that after this.
