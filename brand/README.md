# Brand

`logo-master.png` is the source render of the FerryCast logo: the clock roundel with the
ferry in it, the wordmark, and the route line, on a white ground at 1254 px square.

Nothing in the app reads this file. Everything it serves is cut from it by
`build_assets.py`, which writes into `src/ferrycast/web/static/brand/`:

| File | What it is | Where it is used |
|------|------------|------------------|
| `mark.png` | the roundel alone, 128 px, transparent | the masthead, on every page |
| `logo.png` | the full lockup, 640 px wide, transparent | the project README |
| `og.png` | the lockup on cream, 1200×630 | link previews (`og:image`) |
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

`og.png` is the card a chat client shows when a FerryCast link is pasted, and it is the
lockup and nothing else. The preview is deliberately generic — the same card and the same
sentence whichever sailing the link names — because it is the app introducing itself to
someone who may never have seen it. The answer is on the page.

The tags around it are in `ferrycast/web/preview.py`.
