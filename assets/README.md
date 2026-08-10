# Share card

`og-card.html` is the source for `src/ferrycast/web/static/og.png` — the 1200×630 image a
chat client shows when a FerryCast link is pasted. The PNG is committed rather than
generated at request time: the running app has no rasteriser, and a scraper that has to wait
for one shows nothing at all.

```bash
python assets/render_og.py
```

That needs Chrome or Chromium on the machine doing the rendering, and nothing at run time.
The script drives it headless, then crops the shot to exactly 1200×630 with Pillow — Chrome's
`--window-size` counts its own furniture, so asking for 630 gives a viewport shorter than
that and silently drops the last line of the card.

**Re-render whenever the card text, the palette, or the route changes.** The card names the
route it was drawn for. Nothing checks that it still matches `config/ferrycast.toml`, so a
new route means editing the `<h1>` here and running the script again — the `og:image:alt`
text, which *is* built from the config, will otherwise describe an image that says something
else.

Everything else about a link preview is per-request and lives in `ferrycast/web/preview.py`:
the title is the sailing, and the description is that sailing's answer.
