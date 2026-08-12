-- FerryCast storage schema.
-- Keyed on (route, terminal, sailing) from day one so a second route can be added later
-- without a migration, per the PRD's P2 note.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS frames (
    id            INTEGER PRIMARY KEY,
    route         TEXT    NOT NULL,
    terminal      TEXT    NOT NULL,
    captured_at   TEXT    NOT NULL,          -- UTC ISO8601, second resolution
    service_date  TEXT    NOT NULL,          -- local YYYY-MM-DD
    path          TEXT,                      -- relative to data_dir; NULL once pruned
    sha256        TEXT,
    bytes         INTEGER,
    width         INTEGER,
    height        INTEGER,
    status        TEXT    NOT NULL,          -- ok | error
    error         TEXT,
    -- Deliberately NOT keyed by route. A camera belongs to a terminal, not to a route, so
    -- a terminal serving two routes (Horseshoe Bay, if this ever grows) should yield one
    -- shared frame per capture rather than a duplicate image per route. `route` below
    -- records which route's capture run fetched it.
    UNIQUE (terminal, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_frames_terminal_time ON frames (terminal, captured_at);
CREATE INDEX IF NOT EXISTS idx_frames_service_date  ON frames (service_date);
CREATE INDEX IF NOT EXISTS idx_frames_route         ON frames (route, captured_at);

CREATE TABLE IF NOT EXISTS deck_space (
    id                INTEGER PRIMARY KEY,
    route             TEXT    NOT NULL,
    terminal          TEXT    NOT NULL,
    observed_at       TEXT    NOT NULL,      -- UTC ISO8601
    service_date      TEXT    NOT NULL,
    sailing_hhmm      TEXT,                  -- local HH:MM the row refers to, when parseable
    departed_hhmm     TEXT,                  -- local HH:MM it ACTUALLY left, per the board
    percent_available INTEGER,
    vessel            TEXT,
    status_text       TEXT,
    fetch_status      TEXT    NOT NULL,      -- ok | error | unparsed
    error             TEXT,
    raw               TEXT,
    -- Route is part of the key: the conditions page is published per route-direction, so
    -- one terminal can report a 15:25 sailing on two different routes.
    UNIQUE (route, terminal, observed_at, sailing_hhmm)
);
CREATE INDEX IF NOT EXISTS idx_deck_space_lookup
    ON deck_space (route, terminal, service_date, sailing_hhmm);

-- One row per (frame, prompt version). Re-running extraction with an improved prompt
-- adds rows rather than destroying the old ones, which keeps R3 idempotent.
CREATE TABLE IF NOT EXISTS observations (
    id                 INTEGER PRIMARY KEY,
    frame_id           INTEGER NOT NULL REFERENCES frames (id) ON DELETE CASCADE,
    prompt_version     TEXT    NOT NULL,
    model              TEXT    NOT NULL,
    vehicle_count      INTEGER,
    lanes_occupied     INTEGER,
    queue_beyond_frame INTEGER,              -- 0/1
    ferry_at_dock      INTEGER,              -- 0/1
    visibility         TEXT,                 -- clear | dim | obscured | dark
    confidence         REAL,
    usable             INTEGER NOT NULL DEFAULT 1,
    notes              TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cost_usd           REAL,
    created_at         TEXT    NOT NULL,
    raw                TEXT,
    UNIQUE (frame_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_observations_frame ON observations (frame_id);

CREATE TABLE IF NOT EXISTS sailings (
    id                  INTEGER PRIMARY KEY,
    route               TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    service_date        TEXT NOT NULL,       -- local YYYY-MM-DD
    scheduled_departure TEXT NOT NULL,       -- local ISO8601 with offset
    depart_hhmm         TEXT NOT NULL,
    day_type            TEXT NOT NULL,
    season              TEXT NOT NULL,
    -- Route is part of the key. Without it, a terminal serving two routes could not hold
    -- two sailings departing at the same minute (Horseshoe Bay routinely does), and the
    -- second would be silently dropped.
    UNIQUE (route, origin, scheduled_departure)
);
CREATE INDEX IF NOT EXISTS idx_sailings_bucket
    ON sailings (route, origin, depart_hhmm, day_type, season);

CREATE TABLE IF NOT EXISTS sailing_records (
    sailing_id         INTEGER PRIMARY KEY REFERENCES sailings (id) ON DELETE CASCADE,
    peak_queue         INTEGER,
    queue_at_departure INTEGER,
    residual_queue     INTEGER,
    carryover          INTEGER,
    overload           INTEGER,              -- 0/1
    cancelled          INTEGER,              -- 0/1
    outcome            TEXT NOT NULL,        -- boarded | waited_1 | waited_2plus | cancelled | unknown
    n_frames           INTEGER NOT NULL DEFAULT 0,
    confidence         REAL,
    queue_truncated    INTEGER NOT NULL DEFAULT 0,
    deck_space_min     INTEGER,
    -- When the deck-space feed first reported no space left for this sailing (UTC).
    -- This is the "how late could I have arrived" signal, and it costs nothing to collect.
    filled_at          TEXT,
    method             TEXT,                 -- frames:<prompt_version> | deck_space | import:<source>
    computed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_outcome ON sailing_records (outcome);

-- First-hand accounts from people who were actually in the line. This is the one signal
-- neither the camera nor the deck-space feed can produce: whether a particular traveller
-- got on. Keyed by the *scheduled* sailing rather than by a sailing id so a report can be
-- filed for a day the aggregator has not materialised yet.
CREATE TABLE IF NOT EXISTS sailing_reports (
    id              INTEGER PRIMARY KEY,
    route           TEXT    NOT NULL,
    origin          TEXT    NOT NULL,
    service_date    TEXT    NOT NULL,      -- local YYYY-MM-DD
    depart_hhmm     TEXT    NOT NULL,      -- the scheduled departure being reported on
    joined_queue_at TEXT,                  -- UTC ISO8601; optional, half a memory still helps
    departed_at     TEXT,                  -- UTC ISO8601, when the vessel actually left
    boarded         INTEGER NOT NULL,      -- 0/1 — the only required fact
    sailings_waited INTEGER,               -- if they missed it: 1, or 2 for "two or more"
    deck_fullness   TEXT,                  -- room | half | nearly_full | full
    source          TEXT    NOT NULL DEFAULT 'web',
    submitted_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_sailing
    ON sailing_reports (route, origin, service_date, depart_hhmm);

-- What ECCC forecast for the water this route crosses, expanded to one row per local date
-- the forecast covers. Wind is the one condition that cancels a sailing outright and the
-- only thing on the page FerryCast does not observe for itself, so the text is stored as
-- issued and quoted rather than paraphrased.
--
-- Keyed by issue as well as by date: a forecast is amended through the day, and the claim
-- the page makes about past cancellations has to be checkable against what was actually
-- being said at the time rather than against the last word on it.
CREATE TABLE IF NOT EXISTS marine_forecast (
    id           INTEGER PRIMARY KEY,
    route        TEXT    NOT NULL,
    site         TEXT    NOT NULL,       -- ECCC marine area code, e.g. m0000028
    service_date TEXT    NOT NULL,       -- local YYYY-MM-DD this forecast covers
    issued_at    TEXT    NOT NULL,       -- UTC ISO8601, ECCC's own stamp
    fetched_at   TEXT    NOT NULL,       -- UTC ISO8601
    area         TEXT,                   -- "Strait of Georgia"
    location     TEXT,                   -- "Strait of Georgia - north of Nanaimo"
    kind         TEXT    NOT NULL,       -- regular | extended
    period       TEXT,                   -- ECCC's own period label
    wind         TEXT,                   -- the forecast sentence, verbatim
    visibility   TEXT,
    warning      TEXT,                   -- NULL only when the feed warned about nothing
    wind_max_kt  INTEGER,                -- strongest wind named in `wind`
    band         TEXT,                   -- light | strong | gale | storm | unknown
    fetch_status TEXT    NOT NULL,       -- ok | error
    error        TEXT,
    raw          TEXT,
    UNIQUE (route, site, service_date, issued_at, kind)
);
CREATE INDEX IF NOT EXISTS idx_marine_lookup
    ON marine_forecast (route, service_date, issued_at);
CREATE INDEX IF NOT EXISTS idx_marine_band ON marine_forecast (route, band, service_date);

CREATE TABLE IF NOT EXISTS event_tags (
    id           INTEGER PRIMARY KEY,
    service_date TEXT NOT NULL,
    tag          TEXT NOT NULL,
    note         TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL,
    UNIQUE (service_date, tag)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    attempted   INTEGER NOT NULL DEFAULT 0,
    succeeded   INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job_time ON job_runs (job, started_at);
