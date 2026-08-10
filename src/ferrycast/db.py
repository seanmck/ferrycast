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


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent. Safe to call on an existing database."""
    conn = connect(db_path)
    conn.executescript(schema_sql())
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
