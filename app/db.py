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
from datetime import datetime, timezone
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
    role       TEXT    NOT NULL CHECK (role IN ('buyer', 'seller', 'admin',
                                                'carrier', 'driver')),
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

-- ---------------------------------------------------------------------------
-- Sales ("成交") after the auction
--
-- A lot's original auction result is kept immutable on ``lots`` (winner_id /
-- purchase_price / status), so the public view keeps showing the original
-- auction outcome.  Every actual sale is a row here with a stable, citable
-- deal id.  An auction close creates the first ``kind='auction'`` deal; a
-- directed re-sale after a revocation creates a ``kind='resale'`` deal.
--
-- ``uq_deals_active_lot`` guarantees at most one *effective* sale per lot at
-- a time.  Revoking a deal keeps the row (history) and simply flips its status,
-- which releases the buyer's budget and leaves all original records intact.
CREATE TABLE IF NOT EXISTS deals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL REFERENCES events(id),
    lot_id         INTEGER NOT NULL REFERENCES lots(id),
    deal_no        TEXT    NOT NULL DEFAULT '',
    buyer_id       INTEGER NOT NULL REFERENCES users(id),
    price          INTEGER NOT NULL CHECK (price >= 0),
    kind           TEXT    NOT NULL DEFAULT 'auction'
                   CHECK (kind IN ('auction', 'resale')),
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'revoked')),
    offer_id       INTEGER REFERENCES offers(id),
    created_at     TEXT    NOT NULL,
    revoked_at     TEXT,
    revoked_by     INTEGER REFERENCES users(id),
    revoke_reason  TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_active_lot
    ON deals(lot_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_deals_event
    ON deals(event_id, status, id);
CREATE INDEX IF NOT EXISTS idx_deals_buyer
    ON deals(event_id, buyer_id, status, id);
CREATE INDEX IF NOT EXISTS idx_deals_lot
    ON deals(lot_id, id);

-- Directed re-sale offers ("补售要约").  An offer never occupies budget; it
-- only becomes a sale when its designated buyer accepts it.  At most one
-- active offer may exist per lot (partial unique index below); issuing a new
-- offer supersedes the previous active one ("换发").
CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    lot_id      INTEGER NOT NULL REFERENCES lots(id),
    buyer_id    INTEGER NOT NULL REFERENCES users(id),
    price       INTEGER NOT NULL CHECK (price >= 1),
    status      TEXT    NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'accepted', 'withdrawn',
                                  'replaced')),
    deal_id     INTEGER REFERENCES deals(id),
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT    NOT NULL,
    decided_at  TEXT,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_active_lot
    ON offers(lot_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_offers_event
    ON offers(event_id, status, id);
CREATE INDEX IF NOT EXISTS idx_offers_buyer
    ON offers(event_id, buyer_id, status, id);
CREATE INDEX IF NOT EXISTS idx_offers_lot
    ON offers(lot_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER,
    actor_id   INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Post-sale pickup / handover ("交割 / 领货")
--
-- One authorization row per SOLD lot.  The effective person who can physically
-- collect the lot is: driver_id when set, otherwise carrier_id when set,
-- otherwise the winner.  Every authorization change bumps auth_version, which
-- invalidates all credentials derived for earlier authorizations -- even when
-- the new authorization names the same physical picker.
CREATE TABLE IF NOT EXISTS pickup_auth (
    event_id     INTEGER NOT NULL REFERENCES events(id),
    lot_id       INTEGER PRIMARY KEY REFERENCES lots(id),
    deal_id      INTEGER REFERENCES deals(id),
    winner_id    INTEGER NOT NULL REFERENCES users(id),
    carrier_id   INTEGER REFERENCES users(id),
    driver_id    INTEGER REFERENCES users(id),
    auth_version INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pickup_auth_carrier ON pickup_auth(carrier_id);
CREATE INDEX IF NOT EXISTS idx_pickup_auth_driver ON pickup_auth(driver_id);
CREATE INDEX IF NOT EXISTS idx_pickup_auth_winner ON pickup_auth(winner_id);

-- One confirmed handover per seller request id (idempotency boundary).
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id),
    seller_id    INTEGER NOT NULL REFERENCES users(id),
    receipt_no   TEXT    NOT NULL,
    picker_id    INTEGER NOT NULL REFERENCES users(id),
    request_key  TEXT    NOT NULL,
    request_hash TEXT    NOT NULL,
    item_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    UNIQUE(event_id, seller_id, request_key)
);

-- Every delivered lot appears exactly once per effective sale: the
-- UNIQUE(lot_id) is the hard guard against a lot being delivered twice, even
-- across racing processes.  ``deal_id`` records which sale was delivered; a
-- delivery made against a revoked sale is rejected up front, and after a
-- re-sale a new deal may be delivered afresh.
CREATE TABLE IF NOT EXISTS delivery_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id  INTEGER NOT NULL REFERENCES deliveries(id),
    event_id     INTEGER NOT NULL REFERENCES events(id),
    lot_id       INTEGER NOT NULL REFERENCES lots(id),
    deal_id      INTEGER REFERENCES deals(id),
    seller_id    INTEGER NOT NULL REFERENCES users(id),
    winner_id    INTEGER NOT NULL REFERENCES users(id),
    picker_id    INTEGER NOT NULL REFERENCES users(id),
    carrier_id   INTEGER REFERENCES users(id),
    driver_id    INTEGER REFERENCES users(id),
    auth_version INTEGER NOT NULL,
    delivered_at TEXT    NOT NULL,
    UNIQUE(lot_id)
);

CREATE INDEX IF NOT EXISTS idx_delivery_items_delivery
    ON delivery_items(delivery_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_winner
    ON delivery_items(event_id, winner_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_picker
    ON delivery_items(event_id, picker_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_seller
    ON delivery_items(event_id, seller_id);

-- ---------------------------------------------------------------------------
-- Offline settlement ("线下成交款结算")
--
-- The service only keeps the books: an admin records bank receipts
-- (payments) and bounces/refunds them; sellers request payouts against income
-- that is both effectively paid AND delivered.  Nothing here talks to a bank.
--
-- One bank serial number is one payment registration within an event
-- (idempotency boundary for re-reporting the same receipt).
CREATE TABLE IF NOT EXISTS payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id),
    serial_no    TEXT    NOT NULL,
    buyer_id     INTEGER NOT NULL REFERENCES users(id),
    amount       INTEGER NOT NULL CHECK (amount >= 0),
    status       TEXT    NOT NULL DEFAULT 'valid'
                 CHECK (status IN ('valid', 'refunded')),
    request_hash TEXT    NOT NULL DEFAULT '',
    lot_count    INTEGER NOT NULL DEFAULT 0,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT    NOT NULL,
    refunded_at  TEXT,
    refund_id    INTEGER,
    UNIQUE(event_id, serial_no)
);

-- Every lot covered by a payment is preserved as history, even after refund.
-- ``active = 1`` marks the allocation of the currently effective payment;
-- the partial unique index is the hard guarantee that a lot has at most one
-- effective receipt at a time, even across racing processes.
-- ``deal_id`` binds a receipt to the sale it pays for: a receipt registered
-- against the original auction deal cannot be used to settle a re-sale (and
-- vice versa), and revoking a sale is refused while its own receipt is still
-- effective.
CREATE TABLE IF NOT EXISTS payment_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments(id),
    event_id   INTEGER NOT NULL REFERENCES events(id),
    serial_no  TEXT    NOT NULL DEFAULT '',
    lot_id     INTEGER NOT NULL REFERENCES lots(id),
    deal_id    INTEGER REFERENCES deals(id),
    seller_id  INTEGER NOT NULL REFERENCES users(id),
    winner_id  INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_items_active_lot
    ON payment_items(lot_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_payment_items_payment
    ON payment_items(payment_id);
CREATE INDEX IF NOT EXISTS idx_payment_items_event_lot
    ON payment_items(event_id, lot_id);
CREATE INDEX IF NOT EXISTS idx_payment_items_seller
    ON payment_items(event_id, seller_id, active);
CREATE INDEX IF NOT EXISTS idx_payment_items_active
    ON payment_items(event_id, active);

-- One refund/bounce record per bank serial (duplicate refunds replay this).
CREATE TABLE IF NOT EXISTS refunds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    refund_no   TEXT    NOT NULL,
    serial_no   TEXT    NOT NULL,
    payment_id  INTEGER NOT NULL REFERENCES payments(id),
    operator_id INTEGER REFERENCES users(id),
    reason      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE(event_id, serial_no)
);

-- Seller payout applications.  (event, seller, request_key) is the seller's
-- idempotency boundary.  status:
--   pending   - occupies the available quota ("待打款占用")
--   paid      - admin confirmed the bank transfer ("已打款")
--   released  - admin released a still-pending application (hold freed)
--   cancelled - whole-order cancellation by the refund re-balancing, or an
--               administrative cancel (hold freed)
CREATE TABLE IF NOT EXISTS payouts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL REFERENCES events(id),
    seller_id     INTEGER NOT NULL REFERENCES users(id),
    request_key   TEXT    NOT NULL,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'paid', 'released',
                                    'cancelled')),
    cancel_reason TEXT    NOT NULL DEFAULT '',
    created_by    INTEGER REFERENCES users(id),
    operator_id   INTEGER REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    decided_at    TEXT,
    UNIQUE(event_id, seller_id, request_key)
);

CREATE INDEX IF NOT EXISTS idx_payouts_seller_status
    ON payouts(event_id, seller_id, status, id);
CREATE INDEX IF NOT EXISTS idx_payouts_event_status
    ON payouts(event_id, status, id);
"""

# Schema helpers for columns added after the first release.  ``CREATE TABLE IF
# NOT EXISTS`` does not add columns to an existing table, so an old database
# gets them via an additive ``ALTER TABLE`` on startup.  Everything added is
# nullable or defaulted, so existing rows stay valid and old normal sales keep
# working without being re-entered.
_COLUMN_MIGRATIONS = (
    ("pickup_auth", "deal_id", "INTEGER REFERENCES deals(id)"),
    ("delivery_items", "deal_id", "INTEGER REFERENCES deals(id)"),
    ("payment_items", "deal_id", "INTEGER REFERENCES deals(id)"),
)

# Roles accepted by newer builds.  The users table may already exist with the
# original CHECK constraint, so init() rebuilds it when an old constraint is
# detected (SQLite cannot ALTER a CHECK in place).
USER_ROLES = ("buyer", "seller", "admin", "carrier", "driver")


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self._lock = threading.RLock()
        self._alias_secret: str | None = None
        self._ready = False

    def ensure_ready(self) -> None:
        """Idempotent schema initialisation, performed lazily on first use.

        This keeps plain ``TestClient(app)`` instances (which do not run the
        lifespan context) and direct service usage working without an explicit
        ``init()`` call.
        """
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                self.init()
                self._ready = True

    def init(self) -> None:
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate_users_role(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_deals(conn)
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
        self._ready = True

    @staticmethod
    def _migrate_deals(conn: sqlite3.Connection) -> None:
        """Add deal linkage columns and backfill one deal per legacy sale.

        Every already-closed lot with a winner becomes a single
        ``kind='auction'`` deal at its original price, so legacy sales are
        immediately citable and revocable without re-entry.  Pickup
        authorisations, deliveries and effective receipts of those lots are
        pointed at the deal.  ``deal_no`` is the citable reference
        ``DL-<id>``.  Runs inside ``BEGIN IMMEDIATE``.
        """
        for table, column, decl in _COLUMN_MIGRATIONS:
            existing = {
                r["name"] for r in conn.execute(
                    f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = conn.execute(
            "SELECT id, event_id, winner_id, purchase_price, closed_at, "
            "created_at FROM lots WHERE winner_id IS NOT NULL "
            "AND purchase_price IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM deals d WHERE d.lot_id = lots.id)"
        ).fetchall()
        for r in rows:
            cur = conn.execute(
                "INSERT INTO deals(event_id, lot_id, buyer_id, price, kind, "
                "status, created_at) VALUES (?,?,?,?,'auction','active',?)",
                (r["event_id"], r["id"], r["winner_id"],
                 int(r["purchase_price"]), r["closed_at"] or now),
            )
            deal_id = cur.lastrowid
            conn.execute(
                "UPDATE deals SET deal_no=? WHERE id=?",
                (f"DL-{deal_id:08d}", deal_id),
            )
            conn.execute(
                "UPDATE pickup_auth SET deal_id=? WHERE lot_id=? "
                "AND deal_id IS NULL", (deal_id, r["id"]))
            conn.execute(
                "UPDATE delivery_items SET deal_id=? WHERE lot_id=? "
                "AND deal_id IS NULL", (deal_id, r["id"]))
            conn.execute(
                "UPDATE payment_items SET deal_id=? WHERE lot_id=? "
                "AND deal_id IS NULL", (deal_id, r["id"]))

    @staticmethod
    def _migrate_users_role(conn: sqlite3.Connection) -> None:
        """Rebuild ``users`` if its CHECK predates the carrier/driver roles."""
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if sql is None or "carrier" in (sql[0] or ""):
            return
        # SQLite's table-rebuild procedure requires FK enforcement off for the
        # rebuild connection; this short-lived init connection closes right away.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE users_new (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id   INTEGER NOT NULL REFERENCES events(id),
                    name       TEXT    NOT NULL,
                    role       TEXT    NOT NULL CHECK (role IN
                               ('buyer','seller','admin','carrier','driver')),
                    token_hash TEXT    NOT NULL UNIQUE,
                    budget     INTEGER CHECK (budget IS NULL OR budget >= 0),
                    created_at TEXT    NOT NULL,
                    UNIQUE (event_id, name)
                )
                """
            )
            conn.execute(
                "INSERT INTO users_new(id, event_id, name, role, token_hash, "
                "budget, created_at) SELECT id, event_id, name, role, "
                "token_hash, budget, created_at FROM users"
            )
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_new RENAME TO users")
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:  # pragma: no cover - defensive
                raise sqlite3.IntegrityError(
                    f"foreign key violations after migration: {violations[:5]}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

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
        self.ensure_ready()
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
        self.ensure_ready()
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
