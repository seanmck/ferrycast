# design-sync notes

Project: **FerryCast — Design System** · `53611e7b-c01f-41cb-8cca-3e4695802675`
https://claude.ai/design/p/53611e7b-c01f-41cb-8cca-3e4695802675

First sync: 2026-08-11.

## What this sync is, and what it deliberately is not

FerryCast is a server-rendered FastAPI + Jinja2 app. **There is no JS component library** —
no `package.json`, no `dist/`, no Storybook, no React anywhere in the repo. The stock
design-sync converter (`package-build.mjs`) cannot run here; it needs a compiled component
package to bundle.

So this is an **off-script, CSS-only sync**, which the base skill explicitly permits ("the
upload format is the contract; the converter is the deterministic path to it, not the only
path"). It ships the design system's *styles* — tokens, fonts, brand, and the class
vocabulary — with an intentionally empty JS namespace. The user chose this scope knowingly
over the alternative of building a React component library first.

`_ds_bundle.js` exports nothing on purpose. Do not treat that as a bug to fix.

## How it is built

Single source of truth: the `<style>` block in `src/ferrycast/web/templates/base.html`.

```sh
node .design-sync/build.mjs  --out ./ds-bundle   # extract + split + emit
node .design-sync/verify.mjs --out ./ds-bundle   # render check (needs playwright)
```

`build.mjs` splits that one stylesheet on its own existing structure — `@font-face` rules to
`fonts/`, the `:root` block plus the dark `@media` override to `tokens/tokens.css`, the
remainder to `_ds_bundle.css`. It brace-matches rather than regexes the nested `@media`, so
adding rules inside it will not truncate the token layer.

**If the split ever goes wrong, look here first:** it keys off `:root {` and the *first*
`@media (prefers-color-scheme: dark)` after it. Reordering base.html so a different dark
block comes first would mis-split. Nothing else in the script is position-dependent.

## Decisions worth not re-litigating

- **Brand artwork ships as data-URI custom properties** (`--brand-mark`, `--brand-logo`) in
  `tokens/brand.css`, not just as files. A rendered design receives only the `styles.css`
  `@import` closure, so it *cannot* link `guidelines/brand/*.png`. The PNGs are uploaded for
  humans browsing the project; the vars are the functional path. Do not "simplify" this by
  dropping the data URIs.
- **The lockup is cream-only.** `--brand-logo`'s wordmark is near-black navy and vanishes on
  hull navy; only the roundel survives both grounds. This is in `guidelines/brand.md` and in
  the conventions header because a design agent will otherwise put it on a dark hero.
- **`main { max-width: 34rem }` is shipped as-is.** It is a real constraint of the app (phone
  first, one narrow column), not an accident of the extraction. The conventions header says
  so explicitly so the agent does not fight it.
- **No `_ds_sync.json` is uploaded.** The anchor's job is letting a re-sync skip re-verifying
  unchanged *components*, and there are none. A hand-made anchor declaring a `shape` the
  converter does not recognise is worse than none. Consequence, which is fine: every re-sync
  re-verifies from scratch — that costs about 30 seconds here.
- Page-level CSS in `index.html`'s `{% block style %}` is **not** synced. That is one page's
  styles, not the design system. If a class in there becomes shared, move it into
  `base.html` and it will flow into the next sync automatically.

## Verification

There are no components to grade, so `.design-sync/verify.mjs` is the gate that stands in
for it. It loads `ds-bundle/.verify.html` — which links **only** `styles.css`, exactly as a
rendered design does — in headless chromium at both `prefers-color-scheme` settings, and
asserts: every `@import` in the closure resolves on disk; tokens resolve to real values;
`.hero`/`.card`/`button` actually paint; `.label` is mono + uppercase; `h1` is Instrument
Serif; the outcome ramp paints; `--control-h` reaches the controls; the brand mark resolves
as a data URI; all three font families load; no failed requests; and light ≠ dark.

Both screenshots were reviewed by eye and match the live app. They land at
`ds-bundle/.verify-{light,dark}.png` (dot-prefixed → never uploaded).

## Re-sync risks

- **playwright is not a repo dependency.** `verify.mjs` imports it, and ESM ignores
  `NODE_PATH`, so it resolves via a `node_modules` **symlink** beside the script. On a fresh
  clone that symlink is dead (it is gitignored). Recreate it:
  `npm i playwright && npx playwright install chromium` somewhere, then
  `ln -sfn <that>/node_modules .design-sync/node_modules`.
- **The font families are subset** to the characters the app renders. If verify ever reports
  a family loaded but text looks wrong, suspect a missing glyph, not a missing file.
  `ibmplexmono-500` ships but the verify page does not currently exercise it.
- `build.mjs` refuses to clear an `--out` directory that lacks its own
  `.ds-build-meta.json`, so pointing it at a non-empty directory fails loudly rather than
  deleting someone's files.
- The upload used the **incremental** path (new, empty project). A future sync will arrive
  pinned via `config.json` and therefore take the **atomic** path — verify everything, then
  upload in one pass.
