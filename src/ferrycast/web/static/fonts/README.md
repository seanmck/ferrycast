# Vendored fonts

The "Deep Water" theme's three typefaces, self-hosted rather than pulled from a CDN: the
query page has to paint on a phone with one bar of signal, and a third-party round trip is
the slowest part of that. Only the weights the theme actually uses are here.

| File | Family | Weight |
|---|---|---|
| `instrumentserif-400.woff2` | Instrument Serif | 400 — headings and times |
| `ibmplexsans-400.woff2` | IBM Plex Sans | 400 — prose |
| `ibmplexsans-500.woff2` | IBM Plex Sans | 500 — buttons and the selected segment |
| `ibmplexmono-400.woff2` | IBM Plex Mono | 400 — every number and tracked label |
| `ibmplexmono-500.woff2` | IBM Plex Mono | 500 — emphasis in mono |

Each is subset to printable ASCII plus the punctuation the app renders (`– — · ° ′ ″ … “ ”
‘ ’ → ← ↓ ↑ ⇄ ± ≥ ≤ − × ÷ •`), with `kern`, `liga`, `calt` and `tnum` retained — `tnum`
matters, because the outcome counts and queue lengths are read as columns. That takes the
set from 139 KB to 59 KB.

Glyphs outside the subset fall back to the system font rather than failing to render. If
the UI ever needs a character beyond this range — another language, a new symbol — re-subset
rather than relying on the fallback.

## Licence

Both families are licensed under the **SIL Open Font License 1.1**, which permits
redistribution alongside this application:

- **IBM Plex Sans / IBM Plex Mono** — © 2017 IBM Corp. <https://github.com/IBM/plex>
- **Instrument Serif** — © 2022 The Instrument Serif Project Authors.
  <https://github.com/Instrument/instrument-serif>

Full licence text: <https://openfontlicense.org/>

## Regenerating

Fetch the `latin` subset from the Google Fonts CSS API for each family and weight above,
then subset with `fonttools`:

```bash
pyftsubset FONT.woff2 --output-file=FONT.woff2 --flavor=woff2 \
  --unicodes="U+0020-007E,U+00B0,U+00B7,U+2013,U+2014,U+2018,U+2019,U+201C,U+201D,U+2026,U+2032,U+2033,U+2190,U+2191,U+2192,U+2193,U+21C4,U+00B1,U+2264,U+2265,U+2212,U+00D7,U+00F7,U+2022" \
  --layout-features=kern,liga,calt,tnum --no-hinting --desubroutinize
```
