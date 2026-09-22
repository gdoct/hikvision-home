"""
Storage for gatekeys: SQLite, one file, no ORM.

The database holds the codes themselves in plain text, because the GUI has to
be able to re-print a card months later. That makes the file as sensitive as
a bunch of spare keys: it lives in a docker volume, 0600, and nothing but this
service reads it.

Weekdays are Python's: 0 = Monday ... 6 = Sunday. Times are minutes from
midnight, always a multiple of 15 (the GUI offers nothing finer). A window's
end is exclusive, so 07:15-13:00 means "up to but not including 13:00".
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS keys (
  id               INTEGER PRIMARY KEY,
  token            TEXT    NOT NULL UNIQUE,
  name             TEXT    NOT NULL,
  note             TEXT    NOT NULL DEFAULT '',
  created_at       TEXT    NOT NULL,
  valid_from       TEXT    NOT NULL,          -- YYYY-MM-DD, inclusive
  valid_to         TEXT    NOT NULL,          -- YYYY-MM-DD, inclusive
  max_uses         INTEGER,                   -- NULL: unlimited
  uses             INTEGER NOT NULL DEFAULT 0,
  cooldown_seconds INTEGER NOT NULL DEFAULT 10,
  revoked          INTEGER NOT NULL DEFAULT 0,
  last_used_at     TEXT                       -- ISO 8601, local time
);

CREATE TABLE IF NOT EXISTS windows (
  id        INTEGER PRIMARY KEY,
  key_id    INTEGER NOT NULL REFERENCES keys(id) ON DELETE CASCADE,
  weekday   INTEGER NOT NULL,                 -- 0 = Monday
  start_min INTEGER NOT NULL,
  end_min   INTEGER NOT NULL                  -- exclusive
);
CREATE INDEX IF NOT EXISTS windows_key ON windows(key_id);

-- Every verify call, valid or not. This is the only record of who was let in.
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  key_id  INTEGER,
  name    TEXT,
  token   TEXT,                               -- as presented, for unknown codes
  outcome TEXT NOT NULL,                      -- valid | refused
  reason  TEXT,
  source  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("database not connected")
    return _conn


def query(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    """Run one statement, commit, return lastrowid."""
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid or 0


def transaction():
    """Context manager for several statements under one lock and one commit.

    Verification reads a key and writes its use count; without this, two codes
    presented at once could both slip past a max_uses of 1.
    """
    return _Transaction()


class _Transaction:
    def __enter__(self) -> sqlite3.Connection:
        _lock.acquire()
        return conn()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                conn().commit()
            else:
                conn().rollback()
        finally:
            _lock.release()
