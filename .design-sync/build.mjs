// Build the claude.ai/design upload bundle from FerryCast's own stylesheet.
//
// FerryCast is a server-rendered FastAPI app: there is no JS component library to
// compile, so this bundle carries the design system's *styles* — tokens, fonts, and
// the class vocabulary those tokens drive — and an intentionally empty JS namespace.
//
// The single source of truth is the <style> block in web/templates/base.html. This
// script extracts it and splits it on its own existing structure, so a change to the
// app's stylesheet flows into the next sync without anyone re-copying CSS by hand.
//
//   node .design-sync/build.mjs [--out ./ds-bundle]

import { createHash } from 'node:crypto';
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';

const REPO = resolve(import.meta.dirname, '..');
const BASE_HTML = join(REPO, 'src/ferrycast/web/templates/base.html');
const STATIC = join(REPO, 'src/ferrycast/web/static');
const NAMESPACE = 'FerryCast';

const outArg = process.argv.indexOf('--out');
const OUT = resolve(outArg > -1 ? process.argv[outArg + 1] : './ds-bundle');

// --- guard: only ever clear a directory we made ourselves -------------------
try {
  const existing = readFileSync(join(OUT, '.ds-build-meta.json'), 'utf8');
  if (!JSON.parse(existing).builtBy) throw new Error('foreign');
  rmSync(OUT, { recursive: true, force: true });
} catch (e) {
  if (e.code !== 'ENOENT') {
    console.error(`[OUT_UNSAFE] refusing to clear ${OUT} — not a prior build of this script.`);
    process.exit(1);
  }
}
mkdirSync(join(OUT, 'tokens'), { recursive: true });
mkdirSync(join(OUT, 'fonts'), { recursive: true });
mkdirSync(join(OUT, 'guidelines/brand'), { recursive: true });

// --- extract the stylesheet -------------------------------------------------
const html = readFileSync(BASE_HTML, 'utf8');
const styleMatch = html.match(/<style>([\s\S]*?)<\/style>/);
if (!styleMatch) {
  console.error('[NO_STYLE] no <style> block in base.html — the stylesheet moved.');
  process.exit(1);
}
// Drop the Jinja hook for page-local CSS; page-level styles are not the design system.
const css = styleMatch[1].replace(/\{%\s*block style\s*%\}\{%\s*endblock\s*%\}/g, '').trim();

// --- split on the sheet's own structure ------------------------------------
// @font-face rules → fonts/, :root + dark override → tokens/, remainder → vocabulary.
const fontFaces = [...css.matchAll(/@font-face\s*\{[^}]*\}/g)].map((m) => m[0]);
if (fontFaces.length === 0) {
  console.error('[FONT_MISSING] no @font-face rules found in base.html.');
  process.exit(1);
}

// The token layer is the :root block plus the dark-scheme :root override that follows
// it. Brace-match rather than regex the nested @media, so a future edit can't truncate.
function blockAt(src, startIdx) {
  let depth = 0;
  for (let i = startIdx; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(startIdx, i + 1);
  }
  throw new Error('unbalanced braces in base.html <style>');
}
const rootIdx = css.indexOf(':root {');
const rootBlock = blockAt(css, rootIdx);
// The first dark @media after :root carries the dark token overrides.
const darkIdx = css.indexOf('@media (prefers-color-scheme: dark)', rootIdx);
const darkBlock = blockAt(css, darkIdx);

let vocabulary = css;
for (const chunk of [...fontFaces, rootBlock, darkBlock]) vocabulary = vocabulary.replace(chunk, '');
vocabulary = vocabulary.replace(/\n{3,}/g, '\n\n').trim();

// --- fonts ------------------------------------------------------------------
// Rewrite the app's absolute /static/fonts/… urls to the flat copies beside fonts.css.
const fontFiles = new Set();
const fontCss = fontFaces
  .join('\n\n')
  .replace(/url\("\/static\/fonts\/([^"]+)"\)/g, (_, f) => {
    fontFiles.add(f);
    return `url("./${f}")`;
  });
for (const f of fontFiles) cpSync(join(STATIC, 'fonts', f), join(OUT, 'fonts', f));
writeFileSync(
  join(OUT, 'fonts/fonts.css'),
  `/* FerryCast self-hosted webfonts — subset to the characters the app renders.\n` +
    `   Subset on purpose: the page has to paint on a phone with one bar of signal. */\n\n${fontCss}\n`,
);

// --- brand ------------------------------------------------------------------
// Rendered designs receive only the styles.css @import closure, so a design cannot
// link an uploaded .png. The mark therefore ships as a data URI custom property —
// that resolves anywhere the tokens do. The .png copies under guidelines/ are for
// humans browsing the project, not for designs to reference.
const dataUri = (rel, mime) =>
  `url("data:${mime};base64,${readFileSync(join(STATIC, rel)).toString('base64')}")`;
const brandCss = `/* FerryCast brand assets, inlined so they resolve inside rendered designs.
   Usage rule (see guidelines/brand.md): --brand-mark survives either ground; the full
   lockup --brand-logo is cream-only — its wordmark is near-black navy and disappears
   against hull navy. */

:root {
  /* The clock roundel with the ferry in it. 128px, transparent. Safe on any ground. */
  --brand-mark: ${dataUri('brand/mark.png', 'image/png')};
  /* The full lockup: roundel + wordmark + route line. 640px wide, transparent.
     LIGHT GROUNDS ONLY. */
  --brand-logo: ${dataUri('brand/logo.png', 'image/png')};
}
`;
writeFileSync(join(OUT, 'tokens/brand.css'), brandCss);
for (const f of ['mark.png', 'logo.png', 'og.png', 'apple-touch-icon.png', 'favicon.ico'])
  cpSync(join(STATIC, 'brand', f), join(OUT, 'guidelines/brand', f));

writeFileSync(
  join(OUT, 'guidelines/brand.md'),
  `# Brand usage

Everything here is cut from one master render by \`brand/build_assets.py\` in the app repo.

| Asset | What it is | Where it belongs |
|---|---|---|
| \`brand/mark.png\` | the clock roundel with the ferry in it, 128px, transparent | the masthead, on every page — **safe on either ground** |
| \`brand/logo.png\` | the full lockup: roundel + wordmark + route line, 640px | **cream grounds only** |
| \`brand/og.png\` | the lockup on cream, 1200x630 | link previews (\`og:image\`) |
| \`brand/apple-touch-icon.png\` | the roundel on cream, 180px | iOS home screen |
| \`brand/favicon.ico\` | the roundel at 16/32/48 | browser tabs |

## The one rule that matters

**The lockup is only ever placed on cream.** In it, "Ferry" is near-black navy and the
tagline is mid-blue — both disappear against the hull navy of the app's dark side. The
roundel alone survives either ground, because its clock face is opaque white and carries
it. That is why the masthead uses the mark and not the lockup.

## Using brand artwork in a design

A rendered design receives only the \`styles.css\` import closure, so it **cannot link the
\`.png\` files above** — they are here for humans. Use the data-URI custom properties, which
resolve anywhere the tokens do:

\`\`\`css
/* safe anywhere */   background: var(--brand-mark) center/contain no-repeat;
/* cream only */      background: var(--brand-logo) center/contain no-repeat;
\`\`\`

## Knocking out the white

The master render sits on a white ground, and the ferry's superstructure and clock face are
that same white — so a global white-to-alpha pass would hollow out the artwork. The build
flood-fills from the border instead, removing only white reachable from outside, and fades
across each antialiased rim rather than cutting at a threshold. Without that, the mark
carries a pale halo everywhere the page is dark.
`,
);

// --- tokens -----------------------------------------------------------------
writeFileSync(
  join(OUT, 'tokens/tokens.css'),
  `/* FerryCast "Deep Water" tokens — chart paper and painted steel.\n` +
    `   Light is the cream side, dark is the hull side; both are drawn from the same\n` +
    `   six-colour palette rather than being inversions of each other. */\n\n` +
    `${rootBlock}\n\n${darkBlock}\n`,
);

// --- component/vocabulary CSS ----------------------------------------------
writeFileSync(
  join(OUT, '_ds_bundle.css'),
  `/* FerryCast class vocabulary — the styles the tokens drive.\n` +
    `   Lifted verbatim from web/templates/base.html: this is the app's real stylesheet,\n` +
    `   not a restatement of it. Style with these classes rather than inventing new ones. */\n\n` +
    `${vocabulary}\n`,
);

// --- styles.css: the entry point (an @import list, never inlined CSS) -------
// Order is load-bearing: tokens and fonts first, vocabulary last.
writeFileSync(
  join(OUT, 'styles.css'),
  ['@import "./tokens/tokens.css";', '@import "./tokens/brand.css";', '@import "./fonts/fonts.css";', '@import "./_ds_bundle.css";', ''].join('\n'),
);

// --- _ds_bundle.js: a deliberately empty namespace -------------------------
// FerryCast ships no importable React components. The header is still the format the
// app's self-check parses; an empty components list is the honest statement of that.
const header = JSON.stringify({
  namespace: NAMESPACE,
  components: [],
  sourceHashes: {},
  inlinedExternals: [],
  builtBy: 'cc-design-sync',
}).replace(/\*\//g, '*\\/');
writeFileSync(
  join(OUT, '_ds_bundle.js'),
  `/* @ds-bundle: ${header} */\n` +
    `/* FerryCast is a server-rendered Jinja2 app: it has no compiled component library.\n` +
    `   This namespace exists so the bundle is well-formed and is intentionally empty —\n` +
    `   build with the CSS vocabulary in styles.css, not with window.FerryCast.*. */\n` +
    `(function(){window.${NAMESPACE}=window.${NAMESPACE}||{};})();\n`,
);

writeFileSync(join(OUT, '_ds_needs_recompile'), JSON.stringify({ by: 'design-sync-cli' }) + '\n');

// --- README -----------------------------------------------------------------
const conventions = (() => {
  try { return readFileSync(join(REPO, '.design-sync/conventions.md'), 'utf8').trim() + '\n\n'; }
  catch { return ''; }
})();
// Several properties share a line in base.html, and the dark block redefines a subset —
// so count unique names across both blocks, not line-anchored matches in :root alone.
// The underscore is load-bearing: --o-waited_1 / --o-waited_2plus carry it.
const tokenNames = [
  ...new Set([...(rootBlock + darkBlock).matchAll(/(--[a-z0-9_-]+)\s*:/g)].map((m) => m[1])),
].sort();
// Strip comments before harvesting selectors, or ordinary prose inside them ("base.html",
// "...unless the legend") is counted as a class the design system does not have.
const decls = vocabulary.replace(/\/\*[\s\S]*?\*\//g, '');
const classNames = [...new Set([...decls.matchAll(/\.([a-z][a-z0-9_-]*)/gi)].map((m) => m[1]))].sort();
writeFileSync(
  join(OUT, 'README.md'),
  conventions +
    `## What is in this project\n\n` +
    `| File | What it holds |\n|---|---|\n` +
    `| \`styles.css\` | the entry point — imports everything below, in order |\n` +
    `| \`tokens/tokens.css\` | ${tokenNames.length} custom properties, light and dark |\n` +
    `| \`tokens/brand.css\` | brand artwork as data-URI custom properties |\n` +
    `| \`fonts/fonts.css\` | ${fontFaces.length} \`@font-face\` rules over ${fontFiles.size} self-hosted woff2 subsets |\n` +
    `| \`_ds_bundle.css\` | the class vocabulary (${classNames.length} classes) |\n` +
    `| \`_ds_bundle.js\` | intentionally empty — no importable components |\n` +
    `| \`guidelines/\` | brand usage rules and the source artwork |\n\n` +
    `Generated from \`src/ferrycast/web/templates/base.html\` by \`.design-sync/build.mjs\`.\n`,
);

writeFileSync(
  join(OUT, '.ds-build-meta.json'),
  JSON.stringify(
    {
      builtBy: 'ferrycast-design-sync',
      namespace: NAMESPACE,
      tokens: tokenNames.length,
      classes: classNames.length,
      fontFaces: fontFaces.length,
      fontFiles: fontFiles.size,
      styleSha: createHash('sha256').update(css).digest('hex').slice(0, 12),
    },
    null,
    2,
  ) + '\n',
);

console.error(
  `built ${OUT}\n` +
    `  tokens:     ${tokenNames.length} custom properties\n` +
    `  vocabulary: ${classNames.length} classes\n` +
    `  fonts:      ${fontFaces.length} @font-face over ${fontFiles.size} files\n` +
    `  components: 0 (CSS-only design system)\n` +
    `  readmeHeader: ${conventions ? 'present' : 'ABSENT — author .design-sync/conventions.md'}`,
);
