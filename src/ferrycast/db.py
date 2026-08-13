"""SQLite access layer.

Plain sqlite3 — the dataset is one route's worth of 15-minute observations, which stays
small enough that an ORM would be pure overhead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from .timeutil import iso, now_utc

# Bump when schema.sql changes in a way existing databases must be migrated through, and
# add the migration to MIGRATIONS below. Recorded in SQLite's `PRAGMA user_version`.
SCHEMA_VERSION = 7


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _add_filled_at(conn: sqlite3.Connection) -> None:
    """v1 -> v2: record when the deck-space feed showed a sailing had filled."""
    if not _column_exists(conn, "sailing_records", "filled_at"):
        conn.execute("ALTER TABLE sailing_records ADD COLUMN filled_at TEXT")


def _add_departed_hhmm(conn: sqlite3.Connection) -> None:
    """v2 -> v3: record the actual departure time the departures board publishes.

    The board says "9:25 am Departed 9:56 am". That is the only source of a real departure
    time at a terminal whose camera does not face the berth — which, on this route, is both
    of them.
    """
    if not _column_exists(conn, "deck_space", "departed_hhmm"):
        conn.execute("ALTER TABLE deck_space ADD COLUMN departed_hhmm TEXT")


def _add_sailings_waited(conn: sqlite3.Connection) -> None:
    """v3 -> v4: how many sailings someone who missed one ended up waiting.

    Without it a report could only ever say "did not get on", which the record stored as
    `filled` — so the distribution could never contain waited_1 or waited_2plus from the
    one source that actually knows the answer.
    """
    if not _column_exists(conn, "sailing_reports", "sailings_waited"):
        conn.execute("ALTER TABLE sailing_reports ADD COLUMN sailings_waited INTEGER")


def _add_fullness(conn: sqlite3.Connection) -> None:
    """v4 -> v5: how full the compound was, which is what the camera can actually report.

    A vehicle count is both unreliable at this resolution and the wrong unit — an RV and a
    hatchback are not interchangeable. The band is what survived measurement, so it needs a
    column of its own rather than living only inside `raw`.
    """
    if not _column_exists(conn, "observations", "fullness"):
        conn.execute("ALTER TABLE observations ADD COLUMN fullness TEXT")


def _add_record_fullness(conn: sqlite3.Connection) -> None:
    """v5 -> v6: roll the band up to the sailing, alongside the counts it replaces.

    The count columns stay. Observations extracted under prompt v1 only have counts, and
    throwing those rows away to adopt a better unit would be trading real history for tidiness.
    """
    for column in (
        "peak_fullness",
        "fullness_at_departure",
        "residual_fullness",
        "queue_started_at",
        "cleared_at",
    ):
        if not _column_exists(conn, "sailing_records", column):
            conn.execute(f"ALTER TABLE sailing_records ADD COLUMN {column} TEXT")


def _add_left_full(conn: sqlite3.Connection) -> None:
    """v6 -> v7: whether the board said this sailing loaded to capacity.

    Free, published every few minutes, and until now discarded: the parser kept the first of
    the board's two lines per sailing and the note only appears on the second.
    """
    if not _column_exists(conn, "sailing_records", "left_full"):
        conn.execute("ALTER TABLE sailing_records ADD COLUMN left_full INTEGER")


# Maps the version being upgraded *from* to the step that moves it forward one version.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _add_filled_at,
    2: _add_departed_hhmm,
    3: _add_sailings_waited,
    4: _add_fullness,
    5: _add_record_fullness,
    6: _add_left_full,
}


class SchemaTooNewError(RuntimeError):
    """The database was written by a newer FerryCast than the one running."""


def connect(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise FileNotFoundError(f"no database at {path}; run `ferrycast init` first")
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_sql() -> str:
    return resources.files("ferrycast").joinpath("schema.sql").read_text(encoding="utf-8")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent, and migrate it forward if needed.

    Safe to call on an existing database — every statement in schema.sql is IF NOT EXISTS,
    so this is the idempotent entry point that `capture` and friends can lean on.
    """
    conn = connect(db_path)
    # A brand-new database gets the current schema outright, so it must not then be walked
    # through migrations that assume the older shape.
    fresh = (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sailings'"
        ).fetchone()[0]
        == 0
    )
    conn.executescript(schema_sql())

    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"{db_path} is at schema version {current}, but this FerryCast understands "
            f"{SCHEMA_VERSION}. Upgrade FerryCast rather than downgrading the database."
        )

    if fresh:
        current = SCHEMA_VERSION
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    while current < SCHEMA_VERSION:
        migration = MIGRATIONS.get(current)
        if migration:
            migration(conn)
        current += 1
        conn.execute(f"PRAGMA user_version = {current}")

    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


class JobRun:
    """Records a job's outcome so `ferrycast health` can spot silent gaps."""

    def __init__(self, conn: sqlite3.Connection, job: str):
        self.conn = conn
        self.job = job
        self.attempted = 0
        self.succeeded = 0
        self._id: int | None = None

    def __enter__(self) -> JobRun:
        cur = self.conn.execute(
            "INSERT INTO job_runs (job, started_at) VALUES (?, ?)",
            (self.job, iso(now_utc())),
        )
        self._id = cur.lastrowid
        self.conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        detail = f"{exc_type.__name__}: {exc}" if exc else None
        ok = exc is None and (self.attempted == 0 or self.succeeded > 0)
        self.conn.execute(
            """UPDATE job_runs
                  SET finished_at = ?, ok = ?, attempted = ?, succeeded = ?, detail = ?
                WHERE id = ?""",
            (iso(now_utc()), int(ok), self.attempted, self.succeeded, detail, self._id),
        )
        self.conn.commit()
        return False  # never swallow the exception


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params).fetchall())


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None
