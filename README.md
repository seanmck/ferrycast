# FerryCast

Historical wait tracker for the **Saltery Bay ⇄ Earls Cove** ferry route.

The route is first-come-first-served with ~2-hour headways, so missing a sailing costs 2+
hours. BC Ferries publishes current deck space, but that number explicitly excludes vehicles
still queued *outside* the terminal — the exact number a traveller needs. FerryCast builds
the missing record: it watches both terminal webcams, extracts how many vehicles were
actually waiting, and answers *"on a day like today, what's the wait?"* from comparable
historical sailings.

It is a retrieval system, not a forecaster. Similarity search is the model.

---

## Status

| Req | What it does | State |
|-----|--------------|-------|
| R1 | Frame capture from both terminal webcams, every 15 min | ✅ Built — **needs webcam URLs** |
| R2 | Deck-space scrape, both directions, same cadence | ✅ Built |
| R3 | Vision extraction to structured JSON, batchable and idempotent | ✅ Built |
| R4 | Sailing-level aggregation: peak queue, carryover, overload | ✅ Built |
| R5 | "Day like today" query UI, mobile-friendly | ✅ Built |
| P1 | Arrival-curve view | ✅ Built |
| P1 | Event calendar tags (auto long weekends + manual) | ✅ Built |
| P1 | Anomaly digest (`ferrycast health`) | ✅ Built |
| P1 | Backfill from service notices | ⚠️ CSV importer only — see [Not built](#not-built) |

**Two things must be filled in before this collects anything:** the webcam frame URLs and
the real sailing timetable. Both are configuration, not code. See [Setup](#setup).

---

## Setup

```bash
pip install -e .

cp config/ferrycast.example.toml config/ferrycast.toml
cp config/schedule.example.toml  config/schedule.toml
```

Then edit both files:

1. **`config/ferrycast.toml` → `webcam_url` for each terminal.**
   This is the PRD's one blocking open question and nothing here guesses at it. You need a
   URL that returns the camera *image* at a fixed address, not the page that embeds it.
   Also satisfy yourself that low-rate archival is acceptable use; the default poll interval
   is 15 minutes and the user agent identifies the project.

2. **`config/schedule.toml` → the real departure times.**
   The shipped times are plausible placeholders, **not** a published timetable. A wrong
   departure time silently mis-windows every observation around it, so check each one.

Then:

```bash
ferrycast init      # create the database and data directories
ferrycast doctor    # verify config, schedule, URLs, and API key
```

`doctor` tells you whether each webcam URL actually returns an image, which is the fastest
way to resolve the open question above.

For extraction, set an API key (capture and scraping work without one):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## How it works

```
  webcams ──► capture ──► frames on disk ──┐
                                           ├──► extract ──► observations ──┐
  BC Ferries ─► scrape ──► deck space ─────┘   (vision, per frame)         │
                                    │                                      │
                                    └──────────────► aggregate ◄───────────┘
                                                         │
                                                  sailing records
                                                         │
                                                    query / web UI
```

Each stage is independent and re-runnable:

- **capture** never raises. A dead camera writes an `error` row, so a gap is visible in the
  data instead of killing the cron job — and one dead camera never stops the other.
- **extract** is keyed on `(frame, prompt_version)`. Bump `prompt_version` in the config and
  re-run to re-extract the whole backlog with a better prompt; the old generation is kept.
- **aggregate** is idempotent and can be re-run over any date range as extraction catches up.

### How an outcome is decided

Per the PRD: given frames before and after a scheduled departure, if the queue doesn't drop
near zero afterwards, the sailing was overloaded and what remains is the carryover.

| Outcome | Meaning |
|---|---|
| `boarded` | The queue cleared — someone in it made this sailing |
| `waited_1` | Overloaded, but the carryover fits the next sailing |
| `waited_2plus` | Carryover exceeds one vessel's capacity |
| `cancelled` | A queue persisted and no vessel ever appeared |
| `unknown` | Not enough usable frames to say (night, fog, capture gap) |

`unknown` is deliberately common and never hidden. A dark frame that reports an empty
compound is marked unusable rather than read as "the queue cleared" — the system says
*"no usable record"* instead of guessing, and the UI reports how many it excluded.

---

## Daily use

```bash
# What's the next sailing likely to do?
ferrycast query

# A specific sailing, with the underlying dates
ferrycast query --origin ERL --date 2026-08-14 --time 15:25 --verbose

# Serve the mobile UI
ferrycast serve --host 0.0.0.0 --port 8000
```

```
Earls Cove -> Saltery Bay
2026-08-14 at 15:25 (friday, peak_summer)
n = 7 comparable sailing(s), match: exact
  Made it on             0     0%
  Waited 1 sailing       7   100% ##############################
  Waited 2+ sailings     0     0%
  Cancelled              0     0%
```

Comparable means **same sailing time × day-type × season bucket**, with BC stat holidays
mapped to Sunday-like. When that bucket is too thin, the search widens in defined steps
(all seasons → ±60 min → similar day types) and **says which step it used** — a distribution
over three sailings is never presented as though it were over thirty. Sample size and the
underlying dates always travel with the answer.

### All commands

| Command | Purpose |
|---|---|
| `ferrycast init` | Create the database and data directories |
| `ferrycast doctor` | Check config, schedule, webcam/deck-space URLs, API key |
| `ferrycast capture` | Capture one frame per terminal (R1) |
| `ferrycast scrape` | Scrape current deck space (R2) |
| `ferrycast extract` | Vision-extract pending frames (R3) |
| `ferrycast aggregate` | Roll frames up into sailing records (R4) |
| `ferrycast query` | The day-like-today distribution (R5) |
| `ferrycast next` | Upcoming scheduled sailings |
| `ferrycast serve` | Run the web UI |
| `ferrycast health` | Capture uptime, coverage and spend |
| `ferrycast prune` | Apply the frame retention policy |
| `ferrycast tag` | Manual event tags (festivals, closures) |
| `ferrycast export` | CSV/JSON export of raw observations |
| `ferrycast import-records` | Seed history from a CSV |

---

## Deployment

A small VPS with cron is the recommended host — see `deploy/crontab.example`:

```cron
*/15 * * * *  cd /srv/ferrycast && ferrycast capture && ferrycast scrape
7    * * * *  cd /srv/ferrycast && ferrycast extract --limit 60
23   3 * * *  cd /srv/ferrycast && ferrycast aggregate --date yesterday
41   4 * * 0  cd /srv/ferrycast && ferrycast prune && ferrycast health --window 7
```

Phase 1 of the PRD is just the first line — get it running and data starts accruing while
everything else is built.

`deploy/ferrycast.service` / `.timer` cover the same thing under systemd, and
`.github/workflows/capture.yml` is a GitHub Actions alternative. Read the caveats at the top
of that workflow before relying on it: Actions cron fires late under load, and the raw frames
have nowhere durable to live, which costs you the ability to re-extract the backlog later
(R3). It is a reasonable way to start collecting today, not the long-term home.

---

## Cost

Comfortably inside the PRD's ~$5/month target, using Claude Haiku 4.5 at $1/$5 per Mtok:

| Lever | Effect |
|---|---|
| Frames downscaled to 896px before upload | Image tokens scale with area — the single biggest saver |
| Dark frames flagged without a model call | Night sailings cost nothing to mark unusable |
| `monthly_budget_usd` | Extraction stops when the month's spend hits the cap |

Roughly ~1k input tokens per frame: about **$2–4/month** at 15-minute cadence across two
cameras, with dark hours skipped. Every call's token usage and cost is recorded per
observation, so `ferrycast health` reports real spend rather than an estimate:

```
vision spend     $1.09 of $5.00 this month
```

If the budget is hit, extraction stops cleanly and says so; capture keeps running, so nothing
is lost — you just catch up later.

---

## Adding a route later

**v1 tracks Saltery Bay ⇄ Earls Cove and nothing else** — one route, two cameras, as the
PRD scopes it. But the parts that are expensive to change once real data exists are already
route-aware, so adding Langdale or Texada later is a config edit rather than a migration.

What's already done:

- **Database keys include the route.** `sailings` is unique on `(route, origin,
  scheduled_departure)` and `deck_space` on `(route, terminal, observed_at, sailing_hhmm)`.
  This matters because a terminal can serve several routes at the same minute — Horseshoe
  Bay routinely does. Keyed only on origin and time, the second sailing would be silently
  dropped, and fixing it later means migrating a live table.
- **Every query filters by route.** Aggregation and the comparability search are scoped, so
  two routes can't blend into one distribution. That failure mode is worse than an error:
  it returns a confident, wrong answer.
- **Frames are deliberately *not* route-keyed.** A camera belongs to a terminal, so a shared
  terminal yields one frame per capture rather than a duplicate image per route.
- **The config format already accepts several routes** (`[[route]]`), and schedule blocks
  accept an optional `route`.
- **Schema versioning** (`PRAGMA user_version`) gives future migrations a defined home.

To add one:

1. Switch `[route]` to the `[[route]]` array form, append the new route, and set
   `[app] active_route`.
2. Add its schedule blocks with `route = "..."` set on each.
3. Run `ferrycast init` (idempotent), then `doctor`, `capture`, `aggregate` as usual.

What is **not** solved, and would need thought:

- **Collecting two routes at once.** `active_route` selects one, so a second route means a
  second install (its own config and database) or teaching the CLI to loop over routes.
  The latter is a small change to `capture`/`scrape`/`aggregate`; it isn't written because
  nothing exercises it yet.
- **A terminal whose one camera overlooks two routes' queues.** At Horseshoe Bay the
  compound serves several destinations, so a vehicle count from one frame can't be
  attributed to a route. That is a modelling problem, not a schema one, and guessing at it
  now would be speculative. Saltery Bay and Earls Cove each serve a single route, so v1
  doesn't encounter it.

## Data

SQLite, keyed on `(route, terminal, sailing)` from day one so a second route can be added
without a migration. Tables: `frames`, `deck_space`, `observations`, `sailings`,
`sailing_records`, `event_tags`, `job_runs`.

Everything is exportable:

```bash
ferrycast export frames --format csv --out frames.csv
ferrycast export sailings --format json --since 2026-06-01
```

Retention follows R1: frame images are kept ~400 days, thinned to one per hour after 120
days, then deleted. **Only the images are pruned** — the extracted observations are the
actual dataset and cost almost nothing to keep forever.

---

## Development

```bash
pip install -e ".[dev]"
pytest        # 99 tests
ruff check src tests
```

The tests concentrate on the parts most likely to be silently wrong: the overload/carryover
inference, the comparability fallback ladder, holiday and season bucketing, the deck-space
parser against several page phrasings, and the failure paths (dead camera, HTML served where
an image was expected, unparseable page, low-confidence frame, exhausted budget).

---

## Not built

- **Backfill by scraping BC Ferries service notices / X posts.** Deliberately skipped: it
  depends on page and post formats that aren't stable enough to write against blind, and a
  wrong parse would seed the dataset with fiction. `ferrycast import-records` takes a CSV
  (`service_date,origin,depart_hhmm,outcome[,peak_queue,carryover,source]`) instead, which
  is the same seeding path with a human in the loop. Worth revisiting once the live
  pipeline has established what a real overload signature looks like.
- Everything in the PRD's Non-Goals and P2 lists: no push alerts, no other routes, no
  accounts or crowdsourced reporting, no trained forecaster, no native app.

## Open questions still owned by you

- **Webcam access** *(blocking)* — confirm frame URL stability and acceptable use.
  `ferrycast doctor` verifies the URL you supply.
- **Night sailings** — currently flagged `unknown` rather than guessed. Once you have real
  night frames, check whether they are usable at all; if not, those sailings will rely on
  deck-space data alone.
- **Departure detection** — implemented as *either* ferry-at-dock from frames *or* a
  deck-space row for that sailing, so the feed anchors sailings when the camera can't.
- **Comparable-day definition** — validate the buckets against the first season. The
  fallback ladder is instrumented, so `match_level` in the API tells you how often the exact
  bucket was too thin.
