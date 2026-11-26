"""SQLite storage.

Money is stored as whole integers (no floats anywhere).

Writes are serialised twice over: an in-process ``RLock`` (fair ordering of
threads) on top of SQLite ``BEGIN IMMEDIATE`` (which also protects against a
second process).  A bid's budget check and its insert therefore live in one
atomic step, and a close sees exactly the bids that committed before it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get(
    "AUCTION_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auction.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'closed')),
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id),
    name       TEXT    NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('buyer', 'seller', 'admin')),
    token_hash TEXT    NOT NULL UNIQUE,
    budget     INTEGER CHECK (budget IS NULL OR budget >= 0),
    created_at TEXT    NOT NULL,
    UNIQUE (event_id, name)
);

CREATE TABLE IF NOT EXISTS lots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL REFERENCES events(id),
    seller_id      INTEGER NOT NULL REFERENCES users(id),
    title          TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    reserve        INTEGER NOT NULL CHECK (reserve >= 1),
    status         TEXT    NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'closed')),
    winner_id      INTEGER REFERENCES users(id),
    purchase_price INTEGER,
    closed_at      TEXT,
    created_at     TEXT    NOT NULL,
    version        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lots_event ON lots(event_id, status);

-- One row per (lot, buyer).  Buyers may only raise their own maximum and may
-- not withdraw, so the row is created on their FIRST valid bid; row id order
-- is therefore the documented tie-breaker (equal max -> earlier first valid
-- bid leads).  Rows are kept after close as price-setting history.
CREATE TABLE IF NOT EXISTS bids (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id),
    lot_id     INTEGER NOT NULL REFERENCES lots(id),
    buyer_id   INTEGER NOT NULL REFERENCES users(id),
    max_bid    INTEGER NOT NULL CHECK (max_bid >= 1),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    UNIQUE (lot_id, buyer_id)
);

CREATE INDEX IF NOT EXISTS idx_bids_lot ON bids(lot_id, max_bid DESC, id ASC);
CREATE INDEX IF NOT EXISTS idx_bids_buyer ON bids(event_id, buyer_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self._lock = threading.RLock()
        self._alias_secret: str | None = None

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='alias_secret'"
                ).fetchone()
                if row is None:
                    import secrets as _secrets

                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES ('alias_secret', ?)",
                        (_secrets.token_hex(32),),
                    )
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key='alias_secret'"
                    ).fetchone()
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            self._alias_secret = row["value"]
        finally:
            conn.close()

    @property
    def alias_secret(self) -> str:
        if self._alias_secret is None:
            raise RuntimeError("Database.init() has not been called")
        return self._alias_secret

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A deferred transaction gives multi-statement reads one consistent
        snapshot under WAL, so a view never mixes pre- and post-bid state."""
        conn = self.connect()
        try:
            conn.execute("BEGIN DEFERRED")
            try:
                yield conn
            finally:
                conn.execute("ROLLBACK")  # read-only; nothing to commit
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()
