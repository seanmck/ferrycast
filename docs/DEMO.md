# Seeing the app with a season behind it

FerryCast is correctly blank for weeks after it is installed. The record it answers from is
one it has to watch happen, so every state worth reviewing — a deep bucket, an arrival
curve, the bounds from the line, a pipeline page with something to report — is invisible
until a season has gone by. That is a bad way to review a design decision.

`tools/seed_demo.py` fills a **local** database with a plausible season so those states can
be looked at today.

```bash
ferrycast init                                        # uses the committed config/
python tools/seed_demo.py --reset --start 2026-05-14
ferrycast serve --port 8000
```

`config/ferrycast.toml` and `config/schedule.toml` are committed and are the live ones —
do not overwrite them with the `.example` copies to get a demo running.

Screenshots of the result are in [`screenshots/`](screenshots/).

## What it writes, and what it deliberately does not

It writes **evidence**, not answers. Deck-space rows, camera frames and their lane
readings, ECCC forecasts and first-hand reports go exactly where the real collectors would
have put them, and then the real `aggregate` runs over the lot. Every outcome on screen is
derived by the same classifier that will derive the real ones — if the classifier is
wrong, this is wrong the same way, which is the only version of this script worth having.
Nothing is inserted into `sailing_records` by hand.

| Table | What is seeded |
|---|---|
| `deck_space` | every 15 min from 2 h before each departure to 30 min after, both terminals, every day the timetable covers |
| `frames` + `observations` | one frame per camera every 5 min for the last 32 days, each with its free `geom-v1` lane reading; night and gale frames marked unusable |
| `observations` (`v2`) | the few dozen frames somebody paid the vision model to read — the `ferrycast check` path |
| `sailing_reports` | ~280 first-hand reports, concentrated on the sailings people actually care about, occasionally two on one sailing |
| `marine_forecast` / `shore_forecast` | one ECCC issue per day per feed, plus two days ahead |
| `event_tags`, `job_runs` | manual event tags and a scheduler history |

The demand model is a shape, not a measurement: a summer Friday out of Earls Cove is the
worst sailing of the week, a shoulder-season Tuesday is empty, roughly half of a queue arrives in
the last forty minutes, a busy sailing leaves late, and a gale cancels a run of sailings
rather than a scattering of them. Carryover is tracked across the day, so an overloaded
14:30 makes the 16:30 worse — which is where `waited_2plus` comes from.

Two consequences of doing it this way are worth knowing:

- **A sailing can be known to have filled with no zero ever recorded.** Once the vessel has
  gone the board stops quoting space aboard it and publishes the departure time and its
  capacity notice instead. Those sailings have `filled = 1` and `filled_at = NULL`, and
  they are exactly the ones where the pooled bounds from the line are the only arrival
  guidance there is — the case the "on days like this" block exists for.
- **Nothing here is represented as real.** No network call is made to BC Ferries, ECCC or
  the vision model. The database is a local file you point the app at by hand; delete it
  and the app is a fresh install again. Do not seed a database that is also collecting.

## Options

```
--config PATH        default config/ferrycast.toml
--start / --end      date range (default: the 180 days ending today)
--frame-days N       how many recent days get camera frames (default 32)
--seed N             RNG seed; the same seed gives the same season
--reset              empty the tables first
```

## What one run produces

```
93 of 93 days had a timetable; frames 17736, reports 282, vision spend $0.25
1488 sailing records
  boarded        1140  76.6%
  filled          296  19.9%
  waited_1         33   2.2%
  cancelled        14   0.9%
  waited_2plus      5   0.3%
```

Zero unknowns, which is the claim the deck-space path is supposed to support.

`--start 2026-05-14` matches the deployed schedule, which covers May 14 – Oct 12 2026 and
nothing else. Days the timetable does not cover produce no sailings and are reported as
such rather than looking like a collection failure — with the default 180-day window this
run says `93 of 181 days had a timetable`.

## The screenshots

| File | What it shows |
|---|---|
| `phone-next-sailing` | first paint: the next departure, already answered |
| `phone-peak-friday` / `-light` | the same sailing in both themes |
| `phone-worst-sailing` | a sailing that filled on all five comparable days |
| `phone-quiet-morning` | the other end of the week — space the whole time |
| `phone-bounds-from-the-line` | the pooled "on days like this" bounds from reports |
| `phone-dates-and-curve` | every disclosure open: the dates, the axes table, the arrival curve |
| `phone-departed-report` / `phone-report-form` | a departed sailing with two reports, and the form |
| `desktop-board-dark` / `-light` | the whole-day board with the day's shape and the worst sailing |
| `desktop-long-weekend` | BC Day weekend — every sailing filled |
| `desktop-gale-day` | a gale warning, and the cancellation record for that wind band |
| `desktop-shoulder-season` | the same board when the route is not under pressure |
| `health-dark` / `-light` | the pipeline page: uptime, the 14-day capture strip, spend |
