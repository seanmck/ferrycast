# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"

pytest                                    # whole suite (pythonpath=src, no install needed)
pytest tests/test_aggregate.py            # one file
pytest tests/test_aggregate.py::test_name # one test
pytest -k "carryover"                     # by name

ruff check src tests                      # line-length 100, rules E/F/I/UP/B
```

CI (`.github/workflows/ci.yml`) runs ruff, pytest, and then a smoke test that the *example*
config still loads: it copies `config/*.example.toml` into a temp dir, sets `FERRYCAST_CONFIG`,
and runs `ferrycast init`, `ferrycast doctor --offline`, `ferrycast next`. Changing config
parsing or the example files means checking that path too.

Running the app locally needs `config/ferrycast.toml` and `config/schedule.toml` (copies of the
`.example.toml` files; both are gitignored so a real timetable and camera URLs stay local).
`ferrycast doctor` validates config, schedule, remote URLs and API key; `ferrycast serve` runs
the web UI; `ferrycast run` runs the UI plus the in-process scheduler.

## Architecture

A single-route (Saltery Bay ⇄ Earls Cove) historical wait tracker. It is a **retrieval system,
not a forecaster**: similarity search over past comparable sailings is the whole model. Read
`README.md` first — it is long and it is the design rationale, not just usage docs.

### Pipeline

Collectors write raw evidence; `aggregate` turns evidence into one record per scheduled sailing;
`query` and the web UI read only records.

```
capture (frames)  scrape (board)  vessels (tracker)  marine/shore (ECCC)
        │                  │                   │                  │
   vision / lanes / extent  │                   │                  │
        └──────────► aggregate.compute_record ──┴──────────────────┘
                              │
                     sailing_records ──► query.py ──► CLI / web
```

Every stage is independent, re-runnable and idempotent. Collectors never raise on a bad remote:
a dead camera writes an `error` frame row, an unparseable page records `unparsed`. A failure has
to be *visible in the data*, never fatal to the job or to the other terminal.

### Modules

| Module | Role |
|---|---|
| `config.py` | Everything uncertain (camera URLs, timetable, page layout) is config, not code. Env overrides `FERRYCAST_*` exist so a container needs no rebuild. |
| `schedule.py` | Dated timetable blocks → `Sailing` objects; day-type and season bucketing (`holidays.py` maps BC stat holidays to Sunday-like). |
| `capture.py` / `deckspace.py` / `vessels.py` / `marine.py` / `shore.py` | The free collectors. |
| `vision.py` | Paid Claude extraction, keyed `(frame, prompt_version)`. |
| `lanes.py` | Free geometric reader for a berth-facing camera (SLT): fitted lane polygons differenced against a per-hour median background. |
| `extent.py` | Free geometric reader for a highway camera pointed down the approach road (ERL): the tail's position past the camera is proof the compound overflowed. Read by the same `lanes.extract_pending` sweep as the lane grids; writes band-only observations (`vehicle_count` deliberately NULL — a highway count is not a compound residual). |
| `selection.py` | Which frames are worth paying to read (essential offsets around each departure). |
| `aggregate.py` | The inference. Biggest and most delicate file. |
| `query.py` | Comparability search and the fallback ladder. |
| `reports.py` | First-hand reports from people in the line. |
| `backfill.py` | On-demand extraction for one slot's comparable sailings. |
| `web/app.py` | FastAPI + Jinja2, server-rendered, three templates. |
| `web/analytics.py` | Server-side PostHog, off unless `FERRYCAST_POSTHOG_KEY` is set. No browser tag: the collectors' remotes are the server's business, and the camera stays the only third party the visitor's browser contacts. Never allowed to raise. |
| `scheduler.py` | In-process cron replacement for container hosts; due-ness read from `job_runs`. |

### The claim model (aggregate.py)

A sailing record carries **two orthogonal, three-valued claims**, not one word:

- `filled` — did the vessel run out of room? Only the departures board can attest it.
- `left_behind` — was anyone provably left on the tarmac? Only a camera residual or a person
  who was in the line can attest it.

**Do not assume deck space is the history source.** Route 7's conditions page has never
published a deck-space percentage (2,368 rows scraped, zero), so `classify_from_deck_space`'s
numeric path never fires here and `filled_at` is never set — no free source gives a fill
*time*. What actually classifies a sailing is `_classify_from_departures_board`: the
operator's "loading maximum number of vehicles" note sets `filled`, and a departed sailing
that stayed noteless for `DEPARTED_NOTE_WAIT` reads as `boarded` at low confidence. Earls
Cove has no board at all, so that direction has only the vessel tracker (departure time
only), reports, and geometry — the compound camera's lanes plus the highway camera's
overflow. ERL's geometry rests on the maintainer's own capacity facts
(`lanes_before_capacity` in `ERL.json`): the fitted lanes sit short of the one-vessel
line, so bare fitted lanes at a *tracked* departure are an affirmative `boarded`, while
occupied lanes or a highway tail cap at `heavy` — a healthy queue, provably a fill only
when it still stands after the vessel has gone. The percentage code stays because other
routes publish one.

`None` means *nobody has said*, which is not `False`. `outcome`
(`boarded`/`filled`/`waited_1`/`waited_2plus`/`cancelled`/`unknown`) is derived from the pair by
`outcome_from_axes` for everything that needs a single word. No source is allowed to speak to an
axis it cannot observe: the tracker sees the ship and never the deck, the board sees the deck and
never the approach road, the ERL camera sees the road and never the compound.

The asymmetry to preserve when touching arrival advice: evidence may only move the "arrive
before" time **earlier**. A report that somebody was turned away at 11:50 is transferable proof
that 11:50 is too late; a report that somebody boarded at 11:05 proves nothing transferable, so
it stays description and never becomes an "arrive by". Reports are explicitly barred from moving
the cutoff — see `reports.outcome_from_reports`.

Generally: `unknown` is surfaced, never hidden or guessed. A dark frame reporting an empty
compound is marked unusable rather than read as "the queue cleared". This honesty about evidence
is the project's core constraint, and the tests concentrate on it.

### Money

**Nothing on a schedule spends money.** `capture` archives frames (free, and irreversible — a
frame not taken at 14:15 is gone), `scrape`/`vessels`/`marine`/`shore` are free, `lanes` and
`extent` are free. Only `vision.py` calls the API, and only via `ferrycast check`, `extract`, or
an explicitly opted-in web endpoint (`web.allow_on_demand_checks` / `allow_on_demand_backfill`,
both off by default, both killable from the environment). Adding anything that spends money on a
schedule or on an anonymous web request contradicts the design.

Vision observations are keyed by `prompt_version`, so bumping it re-extracts stored frames into a
new generation and never rewrites the old one.

### Database

Plain `sqlite3`, one writer (hence UI and scheduler in one process — never two replicas on one
volume). `schema.sql` is all `IF NOT EXISTS`; `init_db` is idempotent and is called at startup.

To change the schema: add the statement to `schema.sql`, write a migration function in `db.py`,
register it in `MIGRATIONS` keyed by the version it upgrades *from*, and bump `SCHEMA_VERSION`.
A fresh database gets `schema.sql` and skips migrations, so both paths must end in the same shape.

Keys include `route` from day one (`sailings` unique on `(route, origin, scheduled_departure)`,
`deck_space` on `(route, terminal, observed_at, sailing_hhmm)`) and every query filters by route
— two routes blending into one distribution would return a confident wrong answer. Frames are
deliberately *not* route-keyed: a camera belongs to a terminal.

### Time

Everything is **stored in UTC and reasoned about in local time** (`timeutil.py`: `iso`, `parse_iso`,
`local`, `combine_local`, `now_utc`). Service date is the local date. Sailing comparisons are done
in minutes-before-departure, not wall clock.

### Web UI and the design system

Server-rendered Jinja2, phone-first, one 34rem column. The **single source of truth for styles is
the `<style>` block in `src/ferrycast/web/templates/base.html`**; `ds-bundle/` is generated output
(gitignored) built by `node .design-sync/build.mjs --out ./ds-bundle`. Read
`.design-sync/conventions.md` before touching styles: never hardcode a colour (light and dark are
two cuts of one six-token palette, not inversions), use the existing class vocabulary rather than
inventing names, and every number a traveller squints at gets `.num`.

Static assets served from the installed package must be listed in `pyproject.toml`'s
`package-data` — the container installs the package rather than copying `src/`, so an unlisted
file is simply missing in production. `brand/build_assets.py` generates the icons and share card;
they are committed, not built at deploy time.

## Conventions

- Comments explain *why*, especially why an alternative was rejected — that style is dense
  throughout this codebase and worth matching. Module docstrings state what a module may and may
  not claim.
- Commit subjects are imperative sentences about behaviour ("Recognise the status word this
  route's tracker actually uses"), and bodies explain the failure that motivated the change. Work
  lands on `main` via PR.
- Tests use the `config` and `conn` fixtures in `tests/conftest.py`, with `add_observation` and
  `build_sailing_frames` to lay down a queue trace around a departure.
