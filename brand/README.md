# Brand

`logo-master.png` is the source render of the FerryCast logo: the clock roundel with the
ferry in it, the wordmark, and the route line, on a white ground at 1254 px square.

Nothing in the app reads this file. Everything it serves is cut from it by
`build_assets.py`, which writes into `src/ferrycast/web/static/brand/`:

| File | What it is | Where it is used |
|------|------------|------------------|
| `mark.png` | the roundel alone, 128 px, transparent | the masthead, and the share card |
| `logo.png` | the full lockup, 640 px wide, transparent | the project README |
| `apple-touch-icon.png` | the roundel on cream, 180 px | iOS home screen |
| `favicon.ico` | the roundel at 16/32/48 | browser tabs |

```bash
pip install pillow      # already a dependency of the app itself
python brand/build_assets.py
```

Two things the script is doing that are not obvious:

**The white is knocked out by a flood fill from the border, not by a white-to-alpha pass.**
The ferry's superstructure and the clock face are the same white as the ground, so a global
pass would hollow out the artwork. Only white reachable from the outside is removed, and it
fades out across the antialiased rim of each stroke rather than being cut at a threshold —
otherwise the mark carries a pale halo everywhere the page is dark.

**The lockup is only ever placed on cream.** `Ferry` is near-black navy and the tagline is
mid-blue, both of which disappear against the hull navy of the app's dark side. The roundel
alone survives either ground — the clock face is opaque white and carries it — which is why
the masthead uses the mark and not the lockup.

## The share card

`og.png` — the 1200×630 image a chat client shows when a FerryCast link is pasted — is the
one asset not cut here. It is typeset: the route, the tagline and the outcome ramp set in the
app's own faces, which Pillow cannot do. `render_card.py` draws `og-card.html` with headless
Chrome and crops the shot to size, writing to the same `static/brand/` directory.

```bash
python brand/build_assets.py     # first: the mark the card places
python brand/render_card.py      # needs Chrome or Chromium; nothing at run time
```

It places the roundel rather than the lockup, for the reason above — the card is on the hull
side, where the wordmark would vanish. What the lockup would have said is set in type
instead, which is what leaves room for the route and the ramp.

Two things worth knowing:

- **Chrome's `--window-size` counts its own furniture**, so asking for a 630 px window gives
  a shorter viewport and silently drops the last line of the card. The script renders with
  slack and crops with Pillow instead.
- **The card names the route it was drawn for**, and nothing checks that it still matches
  `config/ferrycast.toml`. A new route means editing the `<h1>` and re-rendering — the
  `og:image:alt` text *is* built from the config, and will otherwise describe an image that
  says something else.

Everything else about a link preview is per-request and lives in `ferrycast/web/preview.py`:
the title is the sailing, and the description is that sailing's answer.
