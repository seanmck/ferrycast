// Render check for the FerryCast CSS bundle.
//
// There are no components to grade here, so this is the gate that stands in for it: load
// the bundle the way a rendered design does (styles.css and nothing else) and assert that
// the closure actually delivers tokens, webfonts, and the class vocabulary. A silent
// failure here would mean every design built with this system renders unstyled.
//
//   node .design-sync/verify.mjs [--out ./ds-bundle]

import { createServer } from 'node:http';
import { cpSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { extname, join, resolve } from 'node:path';
import { chromium } from 'playwright';

const outArg = process.argv.indexOf('--out');
const OUT = resolve(outArg > -1 ? process.argv[outArg + 1] : './ds-bundle');
const REPO = resolve(import.meta.dirname, '..');

cpSync(join(REPO, '.design-sync/verify.html'), join(OUT, '.verify.html'));

// --- static check: every @import in the closure must resolve on disk ---------
const problems = [];
const seen = new Set();
(function walk(rel) {
  if (seen.has(rel)) return;
  seen.add(rel);
  const abs = join(OUT, rel);
  if (!existsSync(abs)) return problems.push(`[CSS_IMPORT_MISSING] ${rel} does not exist`);
  const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
  for (const m of readFileSync(abs, 'utf8').matchAll(/@import\s+(?:url\()?["']([^"']+)["']/g)) {
    if (/^https?:/.test(m[1])) continue;
    walk(join(dir, m[1]).replace(/\\/g, '/'));
  }
})('styles.css');

const MIME = { '.css': 'text/css', '.js': 'text/javascript', '.html': 'text/html',
  '.woff2': 'font/woff2', '.png': 'image/png', '.ico': 'image/x-icon' };
const server = createServer((req, res) => {
  const p = join(OUT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(OUT) || !existsSync(p)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'content-type': MIME[extname(p)] ?? 'application/octet-stream' });
  res.end(readFileSync(p));
});
await new Promise((r) => server.listen(0, r));
const base = `http://127.0.0.1:${server.address().port}`;

const browser = await chromium.launch();
const results = {};
for (const scheme of ['light', 'dark']) {
  const ctx = await browser.newContext({ colorScheme: scheme, viewport: { width: 480, height: 1400 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const failed = [];
  page.on('requestfailed', (r) => failed.push(r.url()));
  page.on('response', (r) => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });
  await page.goto(`${base}/.verify.html`, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);

  results[scheme] = await page.evaluate(() => {
    const cs = (sel, prop) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el).getPropertyValue(prop).trim() : null;
    };
    const root = getComputedStyle(document.documentElement);
    return {
      // Tokens resolved (not empty, not the literal var() fallback).
      tokenBg: root.getPropertyValue('--bg').trim(),
      tokenAccent: root.getPropertyValue('--accent').trim(),
      tokenHeroBg: root.getPropertyValue('--hero-bg').trim(),
      brandMarkLen: root.getPropertyValue('--brand-mark').trim().length,
      // Vocabulary applied.
      bodyBg: cs('body', 'background-color'),
      bodyFont: cs('body', 'font-family'),
      heroBg: cs('.hero', 'background-color'),
      cardBg: cs('.card', 'background-color'),
      btnBg: cs('button', 'background-color'),
      labelFont: cs('.label', 'font-family'),
      labelTransform: cs('.label', 'text-transform'),
      h1Font: cs('h1', 'font-family'),
      segBoarded: cs('.seg-boarded', 'background-color'),
      controlH: cs('select', 'height'),
      // Webfonts actually loaded, by family.
      fontsLoaded: [...document.fonts].filter((f) => f.status === 'loaded').map((f) => `${f.family} ${f.weight}`),
      markPainted: getComputedStyle(document.querySelector('.wordmark span')).backgroundImage.startsWith('url("data:image/png'),
    };
  });
  results[scheme].netFailures = failed;
  await page.screenshot({ path: join(OUT, `.verify-${scheme}.png`), fullPage: true });
  await ctx.close();
}
await browser.close();
server.close();

// --- assertions -------------------------------------------------------------
const px = (v) => v && v !== 'rgba(0, 0, 0, 0)' && v !== 'transparent';
for (const [scheme, r] of Object.entries(results)) {
  const need = {
    'tokens resolve': px(r.tokenBg) && px(r.tokenAccent) && px(r.tokenHeroBg),
    'body vocabulary applied': px(r.bodyBg) && /IBM Plex Sans/.test(r.bodyFont ?? ''),
    'hero/card/button painted': px(r.heroBg) && px(r.cardBg) && px(r.btnBg),
    'label is mono + uppercase': /IBM Plex Mono/.test(r.labelFont ?? '') && r.labelTransform === 'uppercase',
    'h1 is Instrument Serif': /Instrument Serif/.test(r.h1Font ?? ''),
    'outcome ramp painted': px(r.segBoarded),
    'control height token applied': r.controlH === '46px',
    'brand mark resolves as data URI': r.brandMarkLen > 100 && r.markPainted,
    'all three families loaded': ['Instrument Serif', 'IBM Plex Sans', 'IBM Plex Mono']
      .every((f) => r.fontsLoaded.some((l) => l.startsWith(f))),
    'no failed requests': r.netFailures.length === 0,
  };
  for (const [name, ok] of Object.entries(need)) if (!ok) problems.push(`[RENDER] ${scheme}: ${name}`);
}
// Light and dark must not be identical — the sheet defines a genuine dark side.
if (results.light.bodyBg === results.dark.bodyBg) problems.push('[RENDER] dark scheme did not change the ground');

writeFileSync(join(OUT, '.render-check.json'), JSON.stringify({ results, problems }, null, 2) + '\n');
console.error(JSON.stringify(results, null, 2));
if (problems.length) { console.error('\nFAIL:\n' + problems.map((p) => '  ' + p).join('\n')); process.exit(1); }
console.error('\nrender check OK — tokens, fonts, vocabulary, and brand all resolve from styles.css alone');
