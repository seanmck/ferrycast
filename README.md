<p align="center">
  <img src="src/ferrycast/web/static/brand/logo.png" alt="FerryCast" width="320">
</p>

# FerryCast

Historical wait tracker for the **Saltery Bay ⇄ Earls Cove** ferry route.

The route is first-come-first-served with ~2-hour headways, so missing a sailing costs 2+
hours. BC Ferries publishes current deck space, but that number explicitly excludes vehicles
still queued *outside* the terminal — the exact number a traveller needs. FerryCast builds
the missing record: it logs, for every sailing, whether the vessel ran out of room and when,
and answers *"on a day like today, what's the wait?"* from comparable historical sailings.
On the morning you travel, it can also read the terminal camera on demand to count the
vehicles actually queued.

It is a retrieval system, not a forecaster. Similarity search is the model.

---

## Status

| Req | What it does | State |
|-----|--------------|-------|
| R1 | Frame capture from both terminal webcams | ✅ Built — archived by default, analysed on demand |
| R2 | Deck-space scrape, both directions, every 15 min | ✅ Built — **the default history source** |
| R3 | Vision extraction to structured JSON, batchable and idempotent | ✅ Built — runs when you ask |
| R4 | Sailing-level aggregation: peak queue, carryover, overload | ✅ Built |
| R5 | "Day like today" query UI, mobile-friendly | ✅ Built |
| P1 | Arrival-curve view | ✅ Built |
| P1 | Event calendar tags (auto long weekends + manual) | ✅ Built |
| P1 | Anomaly digest (`ferrycast health`) | ✅ Built |
| P1 | Backfill from service notices | ⚠️ CSV importer only — see [Not built](#not-built) |
| — | First-hand reports from the line | ✅ Built — see [Reporting a sailing](#reporting-a-sailing-you-were-on) |

**Before this collects anything you must fill in the real sailing timetable**, and the
webcam URLs too if you want on-demand camera checks. Both are configuration, not code.
See [Setup](#setup).

---

## Setup

```bash
pip install -e .

cp config/ferrycast.example.toml config/ferrycast.toml
cp config/schedule.example.toml  config/schedule.toml
```

Then edit both files:

1. **`config/ferrycast.toml` → `webcam_url` for each terminal.**
   Needed only for `ferrycast check` — the historical record is built from deck space, so
   everything else works without it. You need a URL that returns the camera *image* at a
   fixed address, not the page that embeds it, and you should satisfy yourself that
   low-rate access is acceptable use.

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

For `ferrycast check`, set an API key. Everything else — scraping, aggregation and the
historical query — works without one:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## How it works

```
  BC Ferries ─► scrape ──► deck space ─────► aggregate ──► sailing records ──► query / UI
   (every 15 min, free)                          ▲                              (free)
                                                 │
  webcam ──► capture ──► frame ──► extract ──────┘
   (only when you run `check`, or opt into the cron)
```

The free path across the top runs on a schedule and needs no camera. The vision path
underneath runs when you ask, and its readings take precedence when present because they
measure the queue outside the terminal rather than space aboard the vessel.

Each stage is independent and re-runnable:

- **scrape** degrades gracefully. A page whose layout changed records `unparsed` rather than
  throwing, and never disturbs anything else.
- **capture** never raises. A dead camera writes an `error` row, so a gap is visible in the
  data instead of killing the job — and one dead camera never stops the other.
- **extract** is keyed on `(frame, prompt_version)`. Bump `prompt_version` in the config and
  re-run to re-extract stored frames with a better prompt; the old generation is kept.
- **aggregate** is idempotent and can be re-run over any date range as evidence arrives.

### How an outcome is decided

From deck space, a sailing whose available space reaches zero before departure `filled`,
and the first zero reading is when it stopped being possible to get on.

From camera frames, the PRD's rule applies: given frames before and after a departure, if
the queue doesn't drop near zero afterwards the sailing was overloaded, and what remains is
the carryover.

From a report, whoever filed it says outright whether they got on, which no camera or feed
can. See [Reporting a sailing](#reporting-a-sailing-you-were-on).

| Outcome | Meaning | Evidence needed |
|---|---|---|
| `boarded` | Space available right up to departure | any |
| `filled` | Ran out of room before departure; how many were left behind is unknown | deck space, or a report |
| `waited_1` | Overloaded, and the carryover fits the next sailing | frames |
| `waited_2plus` | Carryover exceeds one vessel's capacity | frames |
| `cancelled` | No vessel ever appeared / feed said cancelled | frames or deck space |
| `unknown` | Not enough evidence to say | — |

Every answer carries the provenance of its evidence, because `filled` and `waited_1` are
different claims and it would be dishonest to blur them. `unknown` is never hidden either:
a dark frame reporting an empty compound is marked unusable rather than read as "the queue
cleared", and the UI reports how many records it excluded.

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
  Space the whole time         0     0%
  Filled up before departure   7   100% ##############################
  Waited 1 sailing             0     0%
  Waited 2+ sailings           0     0%
  Cancelled                    0     0%

  typically ran out of room 30 min before departure (about 14:55)
  From published deck space: it shows when the vessel ran out of room, not how many
  vehicles were still queued outside the terminal.
```

### The web UI

`ferrycast serve` puts two pages on the configured port:

| Path | What it is |
|---|---|
| `/` | The day-like-today answer: pick a direction, date and sailing; get the distribution, when to arrive, and the dates behind it |
| `/health` | Pipeline health — uptime, season coverage, a 14-day capture strip, and spend against budget |

Both are styled with the **Deep Water** theme (hull navy and chart cream, cedar and buoy
accents, Instrument Serif for times and IBM Plex Mono for every number), and follow the
device's light/dark setting. The three typefaces are self-hosted from
`web/static/fonts/`, subset to the characters the app can render — 58 KB in total, and no
third-party requests, because the page has to paint at the side of Highway 101.

The masthead carries the clock-and-ferry mark, and the same artwork supplies the favicon and
the iOS home-screen icon. All of it is cut from one master render by
`brand/build_assets.py` — see [`brand/README.md`](brand/README.md) for how, and for why the
masthead uses the mark alone rather than the full lockup.

#### Reporting a sailing you were on

Deck space knows how much room was left aboard; the camera counts the vehicles waiting
outside. Neither knows whether *you* got on. So under every sailing that has already
departed there is a short form:

| Field | Required | Why it is asked |
|---|---|---|
| Did you get on? | yes | The outcome. Nothing else in the pipeline observes it directly |
| Joined the line | no | Bounds when the cutoff was — see below |
| Ferry left | no | Actual against scheduled departure |
| How full was the deck? | no | Four steps, not a percentage: nobody on a car deck can tell 60% from 70% |

Only the first answer is needed, because a half-remembered trip is still worth more than no
record. Nothing identifies the person filing it, and submitting **re-derives that sailing
immediately** rather than waiting for the nightly aggregation — otherwise the page would go
on contradicting what you just told it.

A report outranks both automatic sources when the outcome is decided, since it is the only
direct observation of the thing the app exists to answer. One person left behind sets the
sailing to `filled` however many others got on: several people boarding is not evidence that
nobody was turned away after them. It is `filled` rather than `waited_1` because how long
that person actually waited is a different question, and one report cannot answer it.

**A report is not allowed to move the "arrive before" time.** Somebody who joined at 11:50
and did not get on proves the cutoff was *earlier* than 11:50; recording their arrival as
the moment it filled would tell the next traveller they can turn up later than they really
can, which is the one direction this app must not be wrong in. Deck space keeps that job,
and the bound the report does establish is stated on the page instead:

> Someone joined the line at **11:50** and did not get on, so the cutoff was earlier than that.
> There was still room for someone who joined at **11:05**.

The same thing over the API, for scripting or a bulk backfill:

```bash
curl -X POST "$HOST/api/report?origin=SLT&service_date=2026-07-03&time=12:30&boarded=false&joined=11:50&deck_fullness=full"
curl "$HOST/api/reports?origin=SLT&service_date=2026-07-03&time=12:30"
ferrycast export reports --format csv --out reports.csv
```

Reports live in their own table, so **an install created before this feature needs one
`ferrycast init`** to add it (idempotent, and `ferrycast run` does it at startup anyway).

#### Sending someone a link

A FerryCast link is almost always sent about one sailing — "we're aiming for the 12:30,
here" — so the Open Graph preview answers for *that* sailing rather than describing the app.
Pasted into a message, `/?origin=SLT&service_date=2026-08-01&time=12:30` shows:

> **12:30 Saltery Bay → Earls Cove · Sat 1 Aug**
> 75% of 12 comparable sailings waited at least one sailing. They typically ran out of room
> 40 min before departure — be in the lineup by 11:50.

The person reading it in a car gets the answer without opening anything, and a thin sample
says so in the preview rather than only on the page. The title and description are built per
request from the same distribution the page renders; the card beneath them is the mark over
the outcome ramp, static and rendered offline (`brand/README.md` covers re-rendering it).

Both `og:url` and `og:image` have to be absolute, and behind a TLS-terminating proxy the app
cannot always tell that it is being served over https — a card whose image is http on an
https page is dropped as mixed content. The forwarded scheme is trusted when there is one;
set **`FERRYCAST_BASE_URL`** (or `[web] base_url`) to your public origin to settle it
outright.

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
| `ferrycast capture` | Archive one frame per terminal (R1) |
| `ferrycast scrape` | Scrape current deck space (R2) |
| `ferrycast extract` | Vision-extract frames (R3); `--essential` reads only what matters |
| `ferrycast check` | On-demand: the queue right now, plus days like this one |
| `ferrycast aggregate` | Roll evidence up into sailing records (R4) |
| `ferrycast query` | The day-like-today distribution (R5) |
| `ferrycast next` | Upcoming scheduled sailings |
| `ferrycast serve` | Run the web UI |
| `ferrycast run` | Web UI **and** scheduler in one process (containers) |
| `ferrycast schedule` | Show the job plan, or `--once` to run what is due |
| `ferrycast health` | Feed uptime, sailing coverage and spend |
| `ferrycast prune` | Apply the frame retention policy |
| `ferrycast tag` | Manual event tags (festivals, closures) |
| `ferrycast export` | CSV/JSON export of raw observations |
| `ferrycast import-records` | Seed history from a CSV |

---

## Deployment

### Container host (Railway, Fly, Render, Docker)

One service runs the web UI and an in-process scheduler together, so the SQLite database
has a single writer and only one volume is needed:

```bash
docker build -t ferrycast .
docker run -p 8000:8000 -v ferrycast-data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... ferrycast
```

**Railway: see [deploy/RAILWAY.md](deploy/RAILWAY.md)** for the full walkthrough — the repo
already carries a `Dockerfile` and `railway.toml`. The one step that matters is attaching a
volume at `/data` before the first real deploy; without it every redeploy destroys the
collected history.

`ferrycast schedule` shows what is scheduled and when each job last ran;
`ferrycast schedule --once` runs whatever is due and exits.

### VPS with cron

See `deploy/crontab.example`:

```cron
*/15 * * * *  cd /srv/ferrycast && ferrycast capture && ferrycast scrape
23   3 * * *  cd /srv/ferrycast && ferrycast aggregate --date yesterday
41   4 * * 0  cd /srv/ferrycast && ferrycast prune
47   4 * * 0  cd /srv/ferrycast && ferrycast health --window 7 --strict
```

That is the entire collection pipeline, and none of it spends money — it archives frames
and scrapes deck space, but analyses nothing. Vision runs only when you type
`ferrycast check` or ask for a specific day. See
[Cost](#cost-and-when-the-vision-model-runs).

Phase 1 of the PRD is just the first line — get it running and data starts accruing while
everything else is built.

`deploy/ferrycast.service` / `.timer` cover the same thing under systemd, and
`.github/workflows/capture.yml` is a GitHub Actions alternative. Read the caveats at the top
of that workflow before relying on it: Actions cron fires late under load, and the raw frames
have nowhere durable to live, which costs you the ability to re-extract the backlog later
(R3). It is a reasonable way to start collecting today, not the long-term home.

---

## Cost, and when the vision model runs

**The vision model runs only when you ask it to.** Nothing on a schedule spends money.

The historical record is built from **deck space**, which is scraped every 15 minutes and
costs nothing. The camera is read only by `ferrycast check`, on the mornings you actually
travel — about **$0.004** a look. A household running the default setup and checking before
a dozen trips a year spends **under five cents a year** on vision.

| What runs | When | Cost |
|---|---|---|
| `capture` — archive a frame from each camera | every 15 min | free (disk only) |
| `scrape` — deck space | every 15 min | free |
| `aggregate` — build sailing records | nightly | free |
| `check` — analyse the camera now | when you ask | ~$0.004 |

**Frames are archived but not analysed.** Capture runs by default because it is the one
irreversible step: extraction can be run at any point in the future, but a frame not taken
at 14:15 is gone for good. So the images accumulate against the day you want them, and the
model reads them only when you ask — either `check` for right now, or
`extract --essential --for-date 2026-07-04` to analyse a day that has already passed.

The archive stays bounded. After `thin_unextracted_after_days` (default 45), frames still
unread are thinned to the ones an extraction would actually use — the handful around each
departure — which halves disk to about **2.8 GB/year** while leaving every sailing fully
analysable. Frames past their retention date that are still unread are held rather than
deleted, and `prune` tells you how many.

### What deck space can and can't tell you

This is the honest trade, and it's the same limitation the PRD identifies. Deck space
describes space aboard the vessel — it excludes vehicles still queued on the approach road.

| Question | Deck space | Camera frames |
|---|---|---|
| Did the sailing fill up? | ✅ | ✅ |
| **When** did it fill — how late could I arrive? | ✅ | ✅ |
| Was it a 1-sailing or a 3-sailing wait? | ❌ | ✅ |
| How many vehicles were left behind? | ❌ | ✅ |
| Does it work at night and in fog? | ✅ | ❌ |

So a sailing that runs out of room is recorded as **`filled`**, not `waited_1` — claiming
someone waited exactly one sailing would assert something this evidence can't support. In
exchange, coverage is total: deck space doesn't care that it's dark, which resolves the
PRD's open question about night sailings. Over a simulated 10-week season it produced a
record for **1,190 of 1,190 sailings with zero unknowns**, where the camera path left 303
night sailings unresolved.

### Optional: queue-level accuracy

If the 1-vs-3-sailing distinction matters, archive frames and read the few per sailing that
decide the outcome. Set `capture.scheduled = true`, add the two commented cron lines in
`deploy/crontab.example`, and it costs about **$2.65/month**:

| Policy | Frames read/week | ~$/month | Gets you |
|---|---:|---:|---|
| Archive only, analyse on demand *(default)* | 0 | **$0.00** | filled / not filled, and when |
| `extract --essential` nightly | 476 | $2.65 | queue counts, carryover, 1 vs 2+ |
| `extract` everything | 966 | $5.38 | the above, plus finer arrival curves |

The default is deliberately the first row *with the frames kept*: you get the free record
now and can buy the finer one for any past day later, for about **9 cents a day**
(`ferrycast extract --essential --for-date 2026-07-04`). That option expires only if you
turn capture off.

### Checking on the morning of departure

```bash
ferrycast check --origin ERL --time 15:25
```

```
Earls Cove -> Saltery Bay
now:  47 vehicles waiting (seen 09:12, 3 min ago)
trend: 31 -> 39 -> 47 (oldest to newest)

history for the 15:25 on days like this (n=7):
  Space the whole time         0     0%
  Filled up before departure   7   100%
  typically ran out of room 30 min before departure (about 14:55)

cost: $0.0041 (1 frame(s) read)
```

It captures a fresh frame itself, so it works with no capture cron running at all, and
reads only the newest few frames. Frames already read cost nothing to re-report, and the
historical half is free — stored records, no model call.

The plain historical query needs no camera at all and costs nothing:

```bash
ferrycast query --origin ERL --date 2026-08-14 --time 15:25
```

### Other levers

| Lever | Effect |
|---|---|
| Frames downscaled to 896px before upload | Image tokens scale with area — the biggest per-frame saver |
| Dark frames flagged without a model call | Night sailings cost nothing to mark unusable |
| `monthly_budget_usd` | Extraction stops when the month's spend hits the cap |

Every call's tokens and cost are recorded per observation, so `ferrycast health` reports
real spend rather than an estimate:

```
vision spend     $1.09 of $5.00 this month
```

If the budget is hit, extraction stops cleanly and says so; capture keeps running, so
nothing is lost.

> **Retention interacts with this.** Because a deferred frame may be the only record of its
> moment, `ferrycast prune` skips frames that have never been extracted and tells you how
> many it held back. Otherwise a cheap extraction policy would quietly destroy the history
> it was deferring. Set `retention.keep_unextracted = false` to reclaim the disk anyway.

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
`sailing_records`, `sailing_reports`, `event_tags`, `job_runs`.

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
pytest        # 230 tests
ruff check src tests
```

The tests concentrate on the parts most likely to be silently wrong: the overload/carryover
inference, the comparability fallback ladder, holiday and season bucketing, the deck-space
parser against several page phrasings, and the failure paths (dead camera, HTML served where
an image was expected, unparseable page, low-confidence frame, exhausted budget).

The served artwork is generated rather than written, and committed: `brand/build_assets.py`
cuts the icons out of the logo master with Pillow, and `brand/render_card.py` draws the
share card with headless Chrome. Neither runs at deploy time — see
[brand/README.md](brand/README.md).

---

## Not built

- **Backfill by scraping BC Ferries service notices / X posts.** Deliberately skipped: it
  depends on page and post formats that aren't stable enough to write against blind, and a
  wrong parse would seed the dataset with fiction. `ferrycast import-records` takes a CSV
  (`service_date,origin,depart_hhmm,outcome[,peak_queue,carryover,source]`) instead, which
  is the same seeding path with a human in the loop. Worth revisiting once the live
  pipeline has established what a real overload signature looks like.
- Everything else in the PRD's Non-Goals and P2 lists: no push alerts, no other routes, no
  accounts, no trained forecaster, no native app. Sailings *can* now be reported by hand
  ([above](#reporting-a-sailing-you-were-on)), but there is nobody to attribute a report to
  and no reputation attached to one — it is a household filling in its own record, not a
  crowdsourcing platform.

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
