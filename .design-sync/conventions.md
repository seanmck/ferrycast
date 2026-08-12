# FerryCast — "Deep Water"

FerryCast is a server-rendered Jinja2 app, so this design system is **CSS only**. There is
no component library: `window.FerryCast` is deliberately empty and `_ds_bundle.js` exports
nothing. Build with plain HTML elements and the class vocabulary below — do not try to
import components, and do not invent class names, because these are the real ones.

Everything ships through `styles.css`. Load it and you have the tokens, the three webfonts,
and the vocabulary; no provider, wrapper, or theme setup is required.

## Two grounds, one palette

Light is the chart-cream side, dark is the hull-navy side, and they are **not inversions** —
both are cut from the same six-colour palette (`--hull --strait --kelp --cedar --buoy
--chart`). Dark applies automatically via `prefers-color-scheme`. **Never hardcode a colour**;
every colour you need already exists as a token, and hardcoding breaks the dark side.

Surfaces & ink: `--bg --surface --inset --ink --muted --label --line --line-strong`
Accent: `--accent` (cedar) `--accent-ink` `--late`
Inverted panel: `--hero-bg --hero-ink --hero-label --hero-muted`
Outcome ramp: `--o-boarded --o-filled --o-waited_1 --o-waited_2plus --o-cancelled --o-unknown`
Form: `--radius` (2px — corners are near-square) `--shadow` (barely there; `none` in dark)
`--control-h` (one height for every control) `--chevron`

## Type

Three families, each with a job. `h1` and `.serif` are Instrument Serif. Body text is IBM
Plex Sans. `.label`, `.mono`, and `.num` are IBM Plex Mono — and **every number a traveller
squints at is `.num`**, which is mono and tabular so digits stay in column.

`.label` is the uppercase letterspaced mono eyebrow that titles nearly every block; pair it
with `.label-tight` where space is short. `.note` is the small muted explanatory line.

## Layout & surfaces

`main` is opinionated: a **34rem centred column** with its own padding. The app is phone-first
and reads as one narrow column — respect that rather than building wide layouts.

- `.card` — the default surface. `.stack` and `.between` are the flex helpers inside it;
  `.rule` is the hairline divider; `.row` is a two-column grid and `.wide` spans it.
- `.hero` — the **one inverted panel** that carries the headline answer. Use it once per
  screen, for the thing the user came for. Its `.label` and `.note` restyle automatically.
- `.flag` — a cedar left rule for something that needs attention. The system never floods a
  whole surface with accent, so use this instead of a tinted box.
- `.masthead` / `.wordmark` — the header row and its logo-plus-name link.

## Controls

`.segmented` (radio group with `<input>` + `<label>` pairs), `.field` wrapping a labelled
`select`/`input`, `button` or `.btn` for the cedar primary action, `.btn-quiet` for the
outlined secondary. Buttons are full-width by default. `select` and `input[type=date]` are
already restyled to one shared height — just use them plain.

## The outcome ramp

The states a sailing can end in, used as `.seg-*` on a `.bar` segment or a `.dot`:
`.seg-boarded .seg-filled .seg-waited_1 .seg-waited_2plus .seg-cancelled .seg-unknown`.
`.bar` is the stacked proportion bar, `.legend` (with `.count`) the key beneath it, and
`.tally` the row of big mono counts. `.n` right-aligns numeric table cells.

## Brand

Two artwork tokens, because rendered designs cannot link uploaded image files:

- `--brand-mark` — the clock roundel. Safe on **either** ground.
- `--brand-logo` — the full lockup. **Cream grounds only**: its wordmark is near-black navy
  and vanishes against hull navy. On a dark surface, use the mark.

Apply as `background: var(--brand-mark) center/contain no-repeat` on a sized element.

## Read the source

`styles.css` imports `tokens/tokens.css`, `tokens/brand.css`, `fonts/fonts.css`, and
`_ds_bundle.css`. Those files are the app's real stylesheet, comments and all, and they
explain *why* each choice was made — read them before styling anything unusual.

```html
<main>
  <div class="hero">
    <p class="label">Typical wait</p>
    <h1 class="serif">One sailing</h1>
    <p class="note">Based on 34 reports over the last 8 weeks.</p>
    <div class="bar">
      <span class="seg-boarded" style="flex:6"></span>
      <span class="seg-waited_1" style="flex:2"></span>
    </div>
  </div>
  <div class="card">
    <div class="between"><span class="label">Depart</span><span class="num">15:30</span></div>
    <hr class="rule">
    <button>Add this report</button>
  </div>
</main>
```
