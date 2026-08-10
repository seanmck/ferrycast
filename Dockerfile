# FerryCast — single container running the web UI and the scheduler together.
#
# One container rather than several because the data lives in one SQLite file on one
# volume: splitting capture, scraping and the UI across services would mean several
# processes writing that file over a shared mount.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FERRYCAST_DATA_DIR=/data

WORKDIR /app

# Dependencies first, so a code change does not re-install them.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Config is copied last: it changes most often, and on a container host it is supplied by
# committing config/ferrycast.toml and config/schedule.toml (they hold no secrets — the API
# key is an environment variable).
COPY config ./config

# Those two files are gitignored by default, so a build straight from a fresh clone would
# produce an image that crash-loops at startup. Fail here instead, where the message is
# visible in the build log.
RUN if [ ! -f config/ferrycast.toml ] || [ ! -f config/schedule.toml ]; then \
        echo "" >&2; \
        echo "ERROR: config/ferrycast.toml and config/schedule.toml must be in the image." >&2; \
        echo "They are gitignored by default. They contain no secrets, so commit them:" >&2; \
        echo "" >&2; \
        echo "    cp config/ferrycast.example.toml config/ferrycast.toml" >&2; \
        echo "    cp config/schedule.example.toml  config/schedule.toml" >&2; \
        echo "    # edit the schedule to the real timetable, then:" >&2; \
        echo "    git add -f config/ferrycast.toml config/schedule.toml" >&2; \
        echo "" >&2; \
        exit 1; \
    fi

# The mount point for the persistent volume. Deliberately a plain mkdir and not a Docker
# `VOLUME` instruction: Railway rejects the build outright ("docker VOLUME is not supported,
# use Railway Volumes"), because it manages the mount itself. The directory still has to
# exist so the app can start before a volume is attached — but note that without one,
# everything written here vanishes on every redeploy. See deploy/RAILWAY.md step 3.
RUN mkdir -p /data

EXPOSE 8000

# Fails fast if the config or schedule is missing, rather than looping on a broken image.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; \
urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz').read()" || exit 1

CMD ["ferrycast", "run"]
