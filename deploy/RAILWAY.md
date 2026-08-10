# Deploying FerryCast on Railway

One service, one volume. The web UI and the scheduler run in the same container because the
data is a single SQLite file: splitting capture, scraping and the UI across services would
mean several processes writing that file over a shared mount, which is how SQLite databases
get corrupted. It also sidesteps any per-service volume limits.

Nothing that runs on the schedule spends money — it archives frames and scrapes deck space.
The vision model runs only when you ask (`ferrycast check`).

---

## 0. Connect Railway to GitHub

If you sign in to [railway.com](https://railway.com) **with GitHub**, the accounts are
linked already — skip to step 1. Otherwise: **Account Settings → Connected Accounts →
connect GitHub**.

Either way you then have to grant the Railway GitHub App access to this specific repo,
because `seanmck/ferrycast` is private and the app defaults to no repositories:

**Railway dashboard → New Project → Deploy from GitHub repo → Configure GitHub App** →
choose **Only select repositories** → add `ferrycast` → Install.

If `seanmck/ferrycast` does not appear in Railway's repo list, that grant is the thing
that is missing, not the connection itself.

> ⚠️ **Set the deploy branch.** Railway tracks the repo's **default branch** (`main`), and
> this work is on `claude/prd-implementation-3b4uv7`. Either merge to `main` first, or after
> creating the service go to **Settings → Source → Branch** and point it at
> `claude/prd-implementation-3b4uv7`. Otherwise Railway will deploy a `main` that has none
> of this code and the build will fail on the missing Dockerfile.

Once connected, every push to the tracked branch redeploys automatically.

**CLI alternative.** If you would rather not link GitHub at all:

```bash
npm i -g @railway/cli
railway login          # opens a browser
railway init           # create the project
railway up             # build and deploy from the local directory
```

This deploys the working directory directly. You lose deploy-on-push, so most people
connect GitHub and keep the CLI for `railway ssh` (step 6).

## 1. Commit your config

`config/ferrycast.toml` and `config/schedule.toml` are gitignored for local work, but the
image needs them. They hold **no secrets** — the API key is an environment variable — so on
a private repo committing them is fine:

```bash
cp config/ferrycast.example.toml config/ferrycast.toml
cp config/schedule.example.toml  config/schedule.toml
```

Edit `config/schedule.toml` to the **real** BC Ferries timetable — the shipped times are
placeholders, and a wrong departure time mis-windows every observation around it.

Leave `data_dir` alone. The Dockerfile sets `FERRYCAST_DATA_DIR=/data`, which takes
precedence over the config file, so the same committed config works locally and on Railway.
Hardcoding `/data` here only breaks local runs.

Leave `webcam_url` empty if you would rather supply it as an environment variable (step 4).

```bash
git add -f config/ferrycast.toml config/schedule.toml
git commit -m "Add deployment config"
git push
```

The Docker build fails with an explicit message if these are missing, so you will find out
in the build log rather than from a crash-looping container.

## 2. Create the service

In Railway: **New Project → Deploy from GitHub repo → seanmck/ferrycast**, then set the
branch as described in step 0.

`railway.toml` in the repo root already sets the builder to the Dockerfile, the start
command to `ferrycast run`, and the healthcheck to `/healthz`. Railway should pick all of
that up without any dashboard configuration.

## 3. Add the volume — do this before the first real deploy

Volumes are **not** under the service's Settings tab. Use the CLI, which is the one route
that is unambiguous:

```bash
npm i -g @railway/cli
railway login
railway link                                          # pick the project + service
railway volume add --service ferrycast --mount-path /data
railway volume list                                   # confirm the mount path
```

`railway volume add` prompts for the service if you omit `--service`. Railway redeploys the
service to attach the volume — wait for that deploy to go green.

The dashboard route is the **`+ Create` button** on the project canvas (the same one used to
add a service), which offers **Volume** alongside Database / GitHub Repo / Empty Service;
`Cmd/Ctrl + K` → "volume" reaches it too. Railway's dashboard changes often enough that the
CLI above is the version worth trusting — `railway volume --help` prints the exact syntax
your CLI supports.

**The mount path must be `/data`.** That is what `FERRYCAST_DATA_DIR=/data` in the
Dockerfile points at. If you mount somewhere else, either change that variable to match or
the app will write to a path that is still ephemeral.

### Why before the first real deploy

Without a volume the container filesystem is ephemeral: the database and every archived
frame are destroyed on each redeploy, and the historical record silently restarts from
nothing — no error, just a permanently empty distribution.

Mounting a volume also **replaces the contents of that directory** in the container, so
anything already written to `/data` by an earlier deploy is gone at the moment you attach
it. That is harmless on day one and annoying after a week of collection, which is the whole
reason to do this first.

### Size

Deck space alone is a few MB a year. With frame archiving on (the default) budget about
**3 GB/year** — `prune` thins unread frames weekly to hold it near that. Start small;
Railway lets you grow a volume later, and the per-plan ceiling is the real constraint, so
check what your plan allows if you intend to archive frames for multiple seasons.

### Check it worked

After the deploy goes green:

```bash
railway ssh ls -la /data
```

You want `ferrycast.db` and a `frames/` directory. If `/data` is empty, give the scheduler
a minute — it creates the database on first tick — then check the deploy logs for
`[scheduler] started`.

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

Use `railway ssh`, which opens a shell **inside the running container** — that is the only
CLI route that sees the volume:

```bash
railway ssh                                   # then, at the container prompt:
ferrycast health
ferrycast schedule
ferrycast check --origin ERL --time 15:25
ferrycast query --origin ERL --date 2026-08-14 --time 15:25
```

Or as one-shots: `railway ssh ferrycast health`.

> ⚠️ **`railway run` is not the same thing.** It runs the command **on your machine** with
> the service's environment variables injected — including `FERRYCAST_DATA_DIR=/data`,
> which on your laptop is either missing or a different, empty directory. So
> `railway run ferrycast check` reads no deployed data, writes its result nowhere useful,
> and still spends the API call. Use it for things that genuinely should run locally
> against remote config; use `railway ssh` for anything touching the database.

The web endpoints in step 5 are the other way in, and need no CLI at all.

---

## Notes and gotchas

- **Do not put `VOLUME ["/data"]` back in the Dockerfile.** Railway fails the build with
  *"docker VOLUME at Line N is not supported, use Railway Volumes"* — it manages the mount
  itself. The Dockerfile just `mkdir -p /data` so the app can start before a volume exists.
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
  put auth in front of it. The CLI path (`railway ssh ferrycast check`) needs no such
  exposure and is the safer default.
- **Cost.** The container is the only ongoing charge; FerryCast itself adds vision spend
  only when you run `check` (~$0.004 each).
