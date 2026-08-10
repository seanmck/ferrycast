"""SQLite access layer.

Plain sqlite3 — the dataset is one route's worth of 15-minute observations, which stays
small enough that an ORM would be pure overhead.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from .timeutil import iso, now_utc

# Bump when schema.sql changes in a way existing databases must be migrated through, and
# add the migration to MIGRATIONS below. Recorded in SQLite's `PRAGMA user_version`.
SCHEMA_VERSION = 1

# Maps the version being upgraded *from* to the SQL that moves it forward one step.
MIGRATIONS: dict[int, str] = {}


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
    conn.executescript(schema_sql())

    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"{db_path} is at schema version {current}, but this FerryCast understands "
            f"{SCHEMA_VERSION}. Upgrade FerryCast rather than downgrading the database."
        )
    while current < SCHEMA_VERSION:
        migration = MIGRATIONS.get(current)
        if migration:
            conn.executescript(migration)
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
