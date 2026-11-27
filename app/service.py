"""Service layer: events, auth, lots, bids, budgets, closing.

Every mutation runs inside ``Database.tx()`` (in-process lock + SQLite
``BEGIN IMMEDIATE``) so a bid's budget check and its write are one atomic
step, and a close observes whatever bids committed before it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from .db import Database
from .domain import (
    Bid,
    Commitment,
    Lot,
    Outcome,
    budget_violations,
    compute_commitments,
    evaluate_lot,
    evaluate_open_lots,
)
from .errors import ApiError
from .pickups import PickupMixin
from .settlements import SettlementMixin

TOKEN_PREFIX = "auk_"
ADMIN_KEY = os.environ.get("AUCTION_ADMIN_KEY", "dev-admin-key")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def alias_for(event_id: int, user_id: int, secret: str) -> str:
    """Stable, event-scoped, non-reversible public alias for a bidder."""
    digest = hmac.new(
        secret.encode("utf-8"), f"{event_id}:{user_id}".encode("utf-8"), hashlib.sha256
    ).digest()
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    n = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(6):
        n, r = divmod(n, len(alphabet))
        chars.append(alphabet[r])
    return "bidder-" + "".join(reversed(chars))


# --- row loaders ------------------------------------------------------------
def _event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ApiError(404, "event_not_found", "Event not found")
    return row


def _lot(conn: sqlite3.Connection, event_id: int, lot_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM lots WHERE event_id=? AND id=?", (event_id, lot_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "lot_not_found", "Lot not found")
    return row


def _user(conn: sqlite3.Connection, event_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM users WHERE event_id=? AND id=?", (event_id, user_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "user_not_found", "User not found")
    return row


def _lots(conn: sqlite3.Connection, event_id: int) -> list[Lot]:
    rows = conn.execute(
        "SELECT id, reserve, status, winner_id, purchase_price FROM lots "
        "WHERE event_id=? ORDER BY id",
        (event_id,),
    ).fetchall()
    return [
        Lot(r["id"], r["reserve"], r["status"], r["winner_id"], r["purchase_price"])
        for r in rows
    ]


def _bids_by_lot(conn: sqlite3.Connection, event_id: int) -> dict[int, list[Bid]]:
    rows = conn.execute(
        "SELECT id, lot_id, buyer_id, max_bid FROM bids WHERE event_id=? ORDER BY id",
        (event_id,),
    ).fetchall()
    by_lot: dict[int, list[Bid]] = {}
    for r in rows:
        by_lot.setdefault(r["lot_id"], []).append(
            Bid(r["id"], r["buyer_id"], r["max_bid"])
        )
    return by_lot


def _budgets(conn: sqlite3.Connection, event_id: int) -> dict[int, int]:
    rows = conn.execute(
        "SELECT id, budget FROM users WHERE event_id=? AND role='buyer' "
        "AND budget IS NOT NULL",
        (event_id,),
    ).fetchall()
    return {r["id"]: r["budget"] for r in rows}


def _lot_bids(conn: sqlite3.Connection, lot_id: int) -> list[Bid]:
    rows = conn.execute(
        "SELECT id, buyer_id, max_bid FROM bids WHERE lot_id=? ORDER BY id", (lot_id,)
    ).fetchall()
    return [Bid(r["id"], r["buyer_id"], r["max_bid"]) for r in rows]


def _next_bid_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM bids").fetchone()["n"]


def _audit(conn: sqlite3.Connection, event_id: int | None, actor_id: int | None,
           action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(event_id, actor_id, action, detail, created_at) "
        "VALUES (?,?,?,?,?)",
        (event_id, actor_id, action, detail, _now()),
    )


class Service(PickupMixin, SettlementMixin):
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- events
    def create_event(self, name: str) -> sqlite3.Row:
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO events(name, status, created_at) VALUES (?, 'open', ?)",
                (name, _now()),
            )
            event_id = cur.lastrowid
            _audit(conn, event_id, None, "event_created", name)
            return _event(conn, event_id)

    def get_event(self, event_id: int) -> sqlite3.Row:
        with self.db.read() as conn:
            return _event(conn, event_id)

    def close_event(self, event_id: int, actor_id: int) -> tuple[sqlite3.Row, list[dict]]:
        with self.db.tx() as conn:
            event = _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if actor["role"] != "admin":
                raise ApiError(403, "forbidden", "Only an admin can close an event")
            if event["status"] != "open":
                raise ApiError(409, "event_closed", "Event is already closed")

            settled = [
                self._close_lot_locked(conn, event_id, row["id"])
                for row in conn.execute(
                    "SELECT id FROM lots WHERE event_id=? AND status='open' ORDER BY id",
                    (event_id,),
                ).fetchall()
            ]
            conn.execute("UPDATE events SET status='closed' WHERE id=?", (event_id,))
            _audit(conn, event_id, actor_id, "event_closed")
            return _event(conn, event_id), settled

    # ----------------------------------------------------------------- users
    def register(self, event_id: int, name: str, role: str) -> dict:
        if role == "admin":
            raise ApiError(403, "forbidden", "Admins are created through the admin key")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.db.tx() as conn:
            _event(conn, event_id)
            dup = conn.execute(
                "SELECT 1 FROM users WHERE event_id=? AND name=?", (event_id, name)
            ).fetchone()
            if dup:
                raise ApiError(409, "name_taken", "Name already taken in this event")
            cur = conn.execute(
                "INSERT INTO users(event_id, name, role, token_hash, budget, created_at) "
                "VALUES (?,?,?,?,NULL,?)",
                (event_id, name, role, _hash_token(token), _now()),
            )
            user_id = cur.lastrowid
            _audit(conn, event_id, user_id, "user_registered", role)
            return {"user_id": user_id, "event_id": event_id, "name": name,
                    "role": role, "token": token}

    def create_admin(self, event_id: int, name: str, admin_key: str) -> dict:
        if not hmac.compare_digest(admin_key, ADMIN_KEY):
            raise ApiError(403, "forbidden", "Invalid admin key")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.db.tx() as conn:
            _event(conn, event_id)
            dup = conn.execute(
                "SELECT 1 FROM users WHERE event_id=? AND name=?", (event_id, name)
            ).fetchone()
            if dup:
                raise ApiError(409, "name_taken", "Name already taken in this event")
            cur = conn.execute(
                "INSERT INTO users(event_id, name, role, token_hash, budget, created_at) "
                "VALUES (?,?,?,?,NULL,?)",
                (event_id, name, "admin", _hash_token(token), _now()),
            )
            _audit(conn, event_id, cur.lastrowid, "admin_created")
            return {"user_id": cur.lastrowid, "event_id": event_id, "name": name,
                    "role": "admin", "token": token}

    def authenticate(self, token: str) -> sqlite3.Row:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE token_hash=?", (_hash_token(token),)
            ).fetchone()
        if row is None:
            raise ApiError(401, "invalid_token", "Missing or invalid bearer token")
        return row

    def me(self, user: sqlite3.Row) -> dict:
        return {"user_id": user["id"], "event_id": user["event_id"],
                "name": user["name"], "role": user["role"]}

    # --------------------------------------------------------------- budgets
    def set_budget(self, event_id: int, buyer_id: int, budget: int) -> Commitment:
        """Set/raise/change a budget; refused if it would leave the buyer short."""
        with self.db.tx() as conn:
            buyer = _user(conn, event_id, buyer_id)
            if buyer["role"] != "buyer":
                raise ApiError(403, "forbidden", "Only buyers set a budget")
            if _event(conn, event_id)["status"] != "open":
                raise ApiError(409, "event_closed",
                               "The event is closed; the budget can no longer change")
            lots = _lots(conn, event_id)
            bids = _bids_by_lot(conn, event_id)
            budgets = _budgets(conn, event_id)
            budgets[buyer_id] = budget
            bad = budget_violations(lots, bids, budgets)
            if bad:
                _, _, commitment = bad[0]
                raise ApiError(
                    409, "budget_below_commitment",
                    f"Budget {budget} is below current commitments ({commitment.total})",
                )
            conn.execute("UPDATE users SET budget=? WHERE id=?", (budget, buyer_id))
            _audit(conn, event_id, buyer_id, "budget_set", str(budget))
            return self._commitment(lots, bids, buyer_id)

    def budget_view(self, event_id: int, buyer_id: int) -> dict:
        with self.db.read() as conn:
            buyer = _user(conn, event_id, buyer_id)
            if buyer["role"] != "buyer":
                raise ApiError(403, "forbidden", "Only buyers have a budget")
            lots = _lots(conn, event_id)
            bids = _bids_by_lot(conn, event_id)
            commitment = self._commitment(lots, bids, buyer_id)
            budget = buyer["budget"]
            return {
                "budget": budget,
                "used": commitment.used,
                "reserved": commitment.reserved,
                "total_committed": commitment.total,
                "remaining": None if budget is None else budget - commitment.total,
            }

    @staticmethod
    def _commitment(lots: Sequence[Lot], bids: dict[int, list[Bid]],
                    buyer_id: int) -> Commitment:
        outcomes = evaluate_open_lots(lots, bids)
        return compute_commitments(lots, bids, outcomes).get(buyer_id) or Commitment()

    # ------------------------------------------------------------------ lots
    def create_lot(self, event_id: int, seller_id: int, title: str,
                   description: str, reserve: int) -> sqlite3.Row:
        with self.db.tx() as conn:
            seller = _user(conn, event_id, seller_id)
            if seller["role"] != "seller":
                raise ApiError(403, "forbidden", "Only sellers create lots")
            if _event(conn, event_id)["status"] != "open":
                raise ApiError(409, "event_closed", "Event is closed")
            cur = conn.execute(
                "INSERT INTO lots(event_id, seller_id, title, description, reserve, "
                "status, created_at) VALUES (?,?,?,?,?, 'open', ?)",
                (event_id, seller_id, title, description, reserve, _now()),
            )
            _audit(conn, event_id, seller_id, "lot_created", str(cur.lastrowid))
            return _lot(conn, event_id, cur.lastrowid)

    def update_lot(self, event_id: int, actor_id: int, lot_id: int, *,
                   title: str | None, description: str | None,
                   reserve: int | None) -> sqlite3.Row:
        with self.db.tx() as conn:
            actor = _user(conn, event_id, actor_id)
            lot = _lot(conn, event_id, lot_id)
            if actor["role"] != "seller" or lot["seller_id"] != actor_id:
                raise ApiError(403, "forbidden", "Only the lot's seller can edit it")
            if lot["status"] != "open":
                raise ApiError(409, "lot_closed", "Lot is already closed")
            has_bids = conn.execute(
                "SELECT COUNT(*) AS n FROM bids WHERE lot_id=?", (lot_id,)
            ).fetchone()["n"] > 0
            if reserve is not None and has_bids and reserve != lot["reserve"]:
                raise ApiError(409, "reserve_locked",
                               "Reserve cannot change once bidding has started")
            updates: dict[str, object] = {}
            if title is not None:
                updates["title"] = title
            if description is not None:
                updates["description"] = description
            if reserve is not None:
                updates["reserve"] = reserve
            if updates:
                assignments = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE lots SET {assignments}, version=version+1 WHERE id=?",
                    (*updates.values(), lot_id),
                )
            _audit(conn, event_id, actor_id, "lot_updated", str(lot_id))
            return _lot(conn, event_id, lot_id)

    # ------------------------------------------------------------- public view
    def _public_lot(self, conn: sqlite3.Connection, lot: sqlite3.Row) -> dict:
        secret = self.db.alias_secret
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM bids WHERE lot_id=?", (lot["id"],)
        ).fetchone()["n"]
        if lot["status"] == "closed":
            price = lot["purchase_price"]
            leader_alias = (
                alias_for(lot["event_id"], lot["winner_id"], secret)
                if lot["winner_id"] is not None else None
            )
        else:
            outcome = evaluate_lot(lot["reserve"], _lot_bids(conn, lot["id"]))
            price = outcome.price
            leader_alias = (
                alias_for(lot["event_id"], outcome.leader_id, secret)
                if outcome.has_leader else None
            )
        return {
            "id": lot["id"],
            "event_id": lot["event_id"],
            "title": lot["title"],
            "description": lot["description"],
            "reserve": lot["reserve"],
            "status": lot["status"],
            "current_price": price,
            "leader_alias": leader_alias,
            "bidder_count": count,
            "created_at": lot["created_at"],
        }

    def list_lots(self, event_id: int) -> dict:
        with self.db.read() as conn:
            event = _event(conn, event_id)
            lots = conn.execute(
                "SELECT * FROM lots WHERE event_id=? ORDER BY id", (event_id,)
            ).fetchall()
            return {
                "event": dict(event),
                "lots": [self._public_lot(conn, lot) for lot in lots],
            }

    def get_lot(self, event_id: int, lot_id: int) -> dict:
        with self.db.read() as conn:
            return self._public_lot(conn, _lot(conn, event_id, lot_id))

    # ---------------------------------------------------------------- bidding
    def place_bid(self, event_id: int, buyer_id: int, lot_id: int, max_bid: int) -> dict:
        with self.db.tx() as conn:
            buyer = _user(conn, event_id, buyer_id)
            if buyer["role"] != "buyer":
                raise ApiError(403, "forbidden", "Only buyers can bid")
            if buyer["budget"] is None:
                raise ApiError(409, "budget_required",
                               "Set your event budget before bidding")
            if _event(conn, event_id)["status"] != "open":
                raise ApiError(409, "event_closed", "Event is closed")
            lot = _lot(conn, event_id, lot_id)
            if lot["status"] != "open":
                raise ApiError(409, "lot_closed", "Lot is already closed")

            lots = _lots(conn, event_id)
            bids = _bids_by_lot(conn, event_id)
            lot_bids = bids.get(lot_id, [])
            mine = next((b for b in lot_bids if b.buyer_id == buyer_id), None)

            if max_bid < lot["reserve"]:
                raise ApiError(422, "below_reserve",
                               f"Max bid must be at least the reserve ({lot['reserve']})")
            if mine is not None and max_bid <= mine.max_bid:
                raise ApiError(422, "must_raise",
                               f"New max must exceed your current max ({mine.max_bid}); "
                               "bids can only be raised")

            # Preview applying this single bid; a returning buyer keeps their
            # original row id, which is the first-valid-bid tie-breaker.
            preview = [b for b in lot_bids if b.buyer_id != buyer_id]
            preview.append(Bid(mine.id if mine else _next_bid_id(conn), buyer_id, max_bid))
            budgets = _budgets(conn, event_id)
            bad = budget_violations(lots, bids, budgets,
                                    preview_lot=lot_id, preview_bids=preview)
            if bad:
                offender_id, offender_budget, commitment = bad[0]
                if offender_id == buyer_id:
                    raise ApiError(
                        409, "budget_exceeded",
                        f"This bid would leave you committed {commitment.total} "
                        f"against a budget of {offender_budget}",
                    )
                raise ApiError(409, "budget_exceeded",
                               "Another buyer's budget would be exceeded by this bid")

            now = _now()
            if mine is None:
                cur = conn.execute(
                    "INSERT INTO bids(event_id, lot_id, buyer_id, max_bid, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (event_id, lot_id, buyer_id, max_bid, now, now),
                )
                bid_id = cur.lastrowid
            else:
                conn.execute("UPDATE bids SET max_bid=?, updated_at=? WHERE id=?",
                             (max_bid, now, mine.id))
                bid_id = mine.id
            conn.execute("UPDATE lots SET version=version+1 WHERE id=?", (lot_id,))
            _audit(conn, event_id, buyer_id, "bid_placed",
                   f"lot={lot_id} max={max_bid}")

            outcome = evaluate_lot(lot["reserve"], preview)
            return {
                "bid_id": bid_id,
                "lot_id": lot_id,
                "max_bid": max_bid,
                "current_price": outcome.price,
                "leading": outcome.leader_id == buyer_id,
            }

    # ---------------------------------------------------------------- closing
    def close_lot(self, event_id: int, actor_id: int, lot_id: int) -> dict:
        with self.db.tx() as conn:
            actor = _user(conn, event_id, actor_id)
            lot = _lot(conn, event_id, lot_id)
            if actor["role"] != "admin" and not (
                actor["role"] == "seller" and actor["id"] == lot["seller_id"]
            ):
                raise ApiError(403, "forbidden",
                               "Only an admin or the lot's seller can close it")
            if lot["status"] != "open":
                raise ApiError(409, "lot_closed", "Lot is already closed")
            return self._close_lot_locked(conn, event_id, lot_id)

    def _close_lot_locked(self, conn: sqlite3.Connection, event_id: int,
                          lot_id: int) -> dict:
        """Settle at the price effective *inside this* serialised transaction."""
        lot = _lot(conn, event_id, lot_id)
        if lot["status"] != "open":
            raise ApiError(409, "lot_closed", "Lot is already closed")
        outcome = evaluate_lot(lot["reserve"], _lot_bids(conn, lot_id))
        # A sale must clear the reserve.  The API never lets a max sit below
        # the reserve, so with any bid the price is >= reserve; the explicit
        # check keeps the rule honest if that ever changes.
        win = (
            outcome.has_leader
            and outcome.price is not None
            and outcome.price >= lot["reserve"]
        )
        winner_id = outcome.leader_id if win else None
        price = outcome.price if win else None

        # Bids are kept (not deleted): they are history and the loser's max is
        # what set the price.  Budget maths ignores them because the lot is
        # closed, and the winner's used amount becomes the purchase price.
        conn.execute(
            "UPDATE lots SET status='closed', winner_id=?, purchase_price=?, "
            "closed_at=?, version=version+1 WHERE id=?",
            (winner_id, price, _now(), lot_id),
        )
        _audit(conn, event_id, None, "lot_closed",
               f"lot={lot_id} winner={winner_id} price={price}")

        # Invariant: price <= leader max <= their budget head-room, so this
        # guard should never fire; it protects against future rule bugs.
        lots = _lots(conn, event_id)
        bids = _bids_by_lot(conn, event_id)
        if budget_violations(lots, bids, _budgets(conn, event_id)):  # pragma: no cover
            raise ApiError(500, "invariant_violation",
                           "Closing this lot would overrun a buyer's budget")

        return {
            "lot_id": lot_id,
            "status": "closed",
            "winner_alias": (
                alias_for(event_id, winner_id, self.db.alias_secret)
                if winner_id is not None else None
            ),
            "purchase_price": price,
        }

    # ------------------------------------------------------- buyer private v.
    def my_bids(self, event_id: int, buyer_id: int) -> dict:
        with self.db.read() as conn:
            buyer = _user(conn, event_id, buyer_id)
            if buyer["role"] != "buyer":
                raise ApiError(403, "forbidden", "Only buyers have bids")
            rows = conn.execute(
                "SELECT b.*, l.title, l.status AS lot_status, l.reserve, "
                "l.winner_id, l.purchase_price "
                "FROM bids b JOIN lots l ON l.id=b.lot_id "
                "WHERE b.event_id=? AND b.buyer_id=? ORDER BY b.id",
                (event_id, buyer_id),
            ).fetchall()
            out = []
            for r in rows:
                if r["lot_status"] == "open":
                    outcome = evaluate_lot(r["reserve"], _lot_bids(conn, r["lot_id"]))
                    out.append({
                        "lot_id": r["lot_id"],
                        "lot_title": r["title"],
                        "max_bid": r["max_bid"],
                        "lot_status": "open",
                        "leading": outcome.leader_id == buyer_id,
                        "won": False,
                        "purchase_price": None,
                        "submitted_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    })
                else:
                    won = r["winner_id"] == buyer_id
                    out.append({
                        "lot_id": r["lot_id"],
                        "lot_title": r["title"],
                        "max_bid": r["max_bid"],
                        "lot_status": "closed",
                        "leading": False,
                        "won": won,
                        "purchase_price": r["purchase_price"] if won else None,
                        "submitted_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    })
            return {"bids": out}
