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
    UNIQUE (terminal, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_frames_terminal_time ON frames (terminal, captured_at);
CREATE INDEX IF NOT EXISTS idx_frames_service_date  ON frames (service_date);

CREATE TABLE IF NOT EXISTS deck_space (
    id                INTEGER PRIMARY KEY,
    route             TEXT    NOT NULL,
    terminal          TEXT    NOT NULL,
    observed_at       TEXT    NOT NULL,      -- UTC ISO8601
    service_date      TEXT    NOT NULL,
    sailing_hhmm      TEXT,                  -- local HH:MM the row refers to, when parseable
    percent_available INTEGER,
    vessel            TEXT,
    status_text       TEXT,
    fetch_status      TEXT    NOT NULL,      -- ok | error | unparsed
    error             TEXT,
    raw               TEXT,
    UNIQUE (terminal, observed_at, sailing_hhmm)
);
CREATE INDEX IF NOT EXISTS idx_deck_space_lookup ON deck_space (terminal, service_date, sailing_hhmm);

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
    UNIQUE (origin, scheduled_departure)
);
CREATE INDEX IF NOT EXISTS idx_sailings_bucket
    ON sailings (origin, depart_hhmm, day_type, season);

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
    method             TEXT,
    computed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_outcome ON sailing_records (outcome);

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
