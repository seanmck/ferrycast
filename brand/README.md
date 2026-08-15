# Brand

`logo-master.png` is the source render of the FerryCast logo: the clock roundel with the
ferry in it, the wordmark, and the route line, on a white ground at 1254 px square.

Nothing in the app reads this file. Everything it serves is cut from it by
`build_assets.py`, which writes into `src/ferrycast/web/static/brand/`:

| File | What it is | Where it is used |
|------|------------|------------------|
| `mark.png` | the roundel alone, 128 px, transparent | the masthead, on every page |
| `logo.png` | the full lockup, 640 px wide, transparent | anywhere the ground is light — including the README on GitHub's light theme |
| `og.png` | the lockup on cream, 1200×630 | link previews (`og:image`) |
| `apple-touch-icon.png` | the roundel on cream, 180 px | iOS home screen |
| `favicon.ico` | the roundel at 16/32/48 | browser tabs |

One more is written beside the master rather than into `web/static/`, because the app never
serves it and it has no business in the wheel:

| File | What it is | Where it is used |
|------|------------|------------------|
| `logo-readme.png` | the lockup on cream, 756 px wide | the top of the project README, **on dark themes only** |

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

That rule is why `logo-readme.png` exists, and why the README serves **two** files rather
than one:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/logo-readme.png">
  <img src="src/ferrycast/web/static/brand/logo.png" alt="FerryCast" width="320">
</picture>
```

GitHub renders a README on whichever ground the reader has chosen, and strips `style`
attributes out of the HTML in it — so a transparent lockup cannot be given a ground in
markup, only in the file. But a cream plate is only *right* on the dark side. On GitHub's
light canvas, which is pure white, it reads as an unintentional box, and badly: the clock
face is near-white, so the plate lands as white inside cream inside white. The transparent
lockup is what belongs there, and `<picture>` is what lets each ground have the file it
wants.

The plain `<img>` is the light one deliberately. It is the fallback anywhere `<picture>`
or `prefers-color-scheme` is not honoured — npm, editors, plain markdown renderers — and
those default to a light ground, so the transparent lockup is the safe thing to land on.

`og.png` is the card a chat client shows when a FerryCast link is pasted, and it is the
lockup and nothing else. The preview is deliberately generic — the same card and the same
sentence whichever sailing the link names — because it is the app introducing itself to
someone who may never have seen it. The answer is on the page.

The tags around it are in `ferrycast/web/preview.py`.
