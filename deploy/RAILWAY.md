# Deploying FerryCast on Railway

One service, one volume. The web UI and the scheduler run in the same container because the
data is a single SQLite file: splitting capture, scraping and the UI across services would
mean several processes writing that file over a shared mount, which is how SQLite databases
get corrupted. It also sidesteps any per-service volume limits.

Nothing that runs on the schedule spends money — it archives frames and scrapes deck space.
The vision model runs only when you ask (`ferrycast check`).

---

## 1. Commit your config

`config/ferrycast.toml` and `config/schedule.toml` are gitignored for local work, but the
image needs them. They hold **no secrets** — the API key is an environment variable — so on
a private repo committing them is fine:

```bash
cp config/ferrycast.example.toml config/ferrycast.toml
cp config/schedule.example.toml  config/schedule.toml
```

Edit `config/schedule.toml` to the **real** BC Ferries timetable — the shipped times are
placeholders, and a wrong departure time mis-windows every observation around it. In
`config/ferrycast.toml` set:

```toml
[app]
data_dir = "/data"        # the volume mount path, see step 3
```

Leave `webcam_url` empty if you would rather supply it as an environment variable (step 4).

```bash
git add -f config/ferrycast.toml config/schedule.toml
git commit -m "Add deployment config"
git push
```

The Docker build fails with an explicit message if these are missing, so you will find out
in the build log rather than from a crash-looping container.

## 2. Create the service

In Railway: **New Project → Deploy from GitHub repo → seanmck/ferrycast**, and pick the
branch you want to run.

`railway.toml` in the repo root already sets the builder to the Dockerfile, the start
command to `ferrycast run`, and the healthcheck to `/healthz`. Railway should pick all of
that up without any dashboard configuration.

## 3. Add the volume — do this before the first real deploy

**Settings → Volumes → Add volume**, mount path:

```
/data
```

Without it the container filesystem is ephemeral: the database and every archived frame are
destroyed on each redeploy, and the historical record silently restarts from nothing. The
`FERRYCAST_DATA_DIR=/data` default in the Dockerfile points the app at this mount.

Size: deck space alone is a few MB a year. With frame archiving on (the default) budget
about **3 GB/year** — `prune` thins unread frames weekly to keep it near that.

## 4. Environment variables

| Variable | Needed | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | for `check` | Vision calls. Everything else works without it. |
| `FERRYCAST_DATA_DIR` | no | Defaults to `/data` in the Dockerfile. |
| `FERRYCAST_WEBCAM_SLT` | optional | Camera URL, overrides the committed config. |
| `FERRYCAST_WEBCAM_ERL` | optional | As above. |
| `FERRYCAST_DECKSPACE_SLT` / `_ERL` | optional | Deck-space page URLs. |
| `PORT` | injected | Railway sets it; `ferrycast run` binds it automatically. |

The `FERRYCAST_WEBCAM_*` overrides exist so you can keep camera URLs out of git entirely if
you prefer — the committed config can leave them blank.

## 5. Verify

Once deployed, against your `*.up.railway.app` domain:

```bash
curl -s https://YOUR-APP.up.railway.app/healthz          # -> ok
curl -s https://YOUR-APP.up.railway.app/api/schedule     # what ran, what is next
curl -s https://YOUR-APP.up.railway.app/api/health       # coverage, spend, problems
```

`/api/schedule` is the one to watch on a first deploy. Within a minute or two `deckspace`
should show a recent `last_run`; if it never does, the scrape is failing — check the deploy
logs for `[scheduler] deckspace`.

The historical record needs time. After a day you will have a few sailings recorded; the
"day like today" distribution only becomes meaningful over weeks, which is why the PRD
treats getting collection running as the thing to ship first.

## 6. Running commands against the deployed data

```bash
railway run ferrycast health
railway run ferrycast check --origin ERL --time 15:25
railway run ferrycast query --origin ERL --date 2026-08-14 --time 15:25
```

`railway run` executes in the deployed environment, so these see the real volume.

---

## Notes and gotchas

- **Keep it at one replica.** SQLite has a single writer; `railway.toml` sets
  `numReplicas = 1`. Scaling out will corrupt the database rather than speed anything up.
- **Redeploys are safe for the schedule.** Due-ness is read from the `job_runs` table on the
  volume, not from memory, so a restart neither re-runs everything nor skips a cycle.
- **The scheduler is in-process, not Railway cron.** That keeps one writer on one volume and
  avoids any minimum-interval restriction on platform cron. `ferrycast schedule` shows the
  plan; `ferrycast schedule --once` runs whatever is due and exits, if you ever do want to
  drive it externally.
- **On-demand checks from the browser are disabled by default.** A public URL that spends
  money is a bad idea. To enable the button, set `[web] allow_on_demand_checks = true` and
  keep `on_demand_daily_cap` sane — and consider that a Railway domain is public unless you
  put auth in front of it. The CLI path (`railway run ferrycast check`) needs no such
  exposure and is the safer default.
- **Cost.** The container is the only ongoing charge; FerryCast itself adds vision spend
  only when you run `check` (~$0.004 each).
