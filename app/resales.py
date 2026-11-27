"""Revocation of a sale and directed re-sale ("撤销成交 / 定向补售").

Mixed into :class:`app.service.Service`; every mutation runs in one
``Database.tx()`` (in-process ``RLock`` + SQLite ``BEGIN IMMEDIATE``).

Model
-----
* A lot keeps its immutable *auction* result on ``lots`` (winner / price /
  status).  The public view therefore keeps showing the original auction
  outcome, and a revocation never rewrites the bids or the auction result.
* Every actual sale is a row in ``deals`` with a citable ``DL-...`` number.
  An auction close inserts the first ``kind='auction'`` deal; a directed
  re-sale inserts a ``kind='resale'`` deal.  Exactly one deal per lot may be
  ``active`` (partial unique index); a revoked deal stays as history.
* Budget "used" follows the *active* deal: revoking releases the original
  buyer and accepting an offer occupies the new buyer's budget.
* A directed offer (``offers``) names a buyer and a positive integer price and
  never occupies budget.  At most one offer per lot is ``active``; re-issuing
  replaces it and an admin may withdraw it.  Only the named buyer may accept
  it, and only if their remaining budget (after other used + leading
  reservations) covers the price.  Accepting creates the new deal; a repeat
  accept returns the original deal without a second occupation.
* Revocation is refused while that deal has an effective receipt or a
  confirmed delivery.  Receipts, delegations and deliveries are bound to a
  deal id, so records made for the original sale cannot settle the re-sale.

Concurrency
-----------
Revoke, accept, receipt registration and delivery all take the same process
lock + ``BEGIN IMMEDIATE`` transaction.  A revoke therefore either commits
before a receipt/delivery (which then sees the deal already revoked and is
refused) or after it (in which case the revoke is refused because that deal
is already paid/delivered).  There is no interleaving where a deal is both
revoked and gains a new effective receipt or delivery.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .errors import ApiError
from .pickups import _audit, _event, _now, _user, as_int, resolve_account


def create_deal(conn: sqlite3.Connection, event_id: int, lot_id: int,
                buyer_id: int, price: int, *, kind: str,
                offer_id: int | None = None) -> sqlite3.Row:
    """Insert one effective sale and assign its citable ``DL-...`` number."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO deals(event_id, lot_id, buyer_id, price, kind, status, "
        "offer_id, created_at) VALUES (?,?,?,?,?, 'active', ?, ?)",
        (event_id, lot_id, buyer_id, int(price), kind, offer_id, now),
    )
    deal_id = cur.lastrowid
    conn.execute("UPDATE deals SET deal_no=? WHERE id=?",
                 (f"DL-{deal_id:08d}", deal_id))
    return conn.execute(
        "SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()


def _user_brief(conn: sqlite3.Connection, user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "user_id": row["id"], "name": row["name"],
            "username": row["name"], "role": row["role"]}


def _lot_brief(conn: sqlite3.Connection, lot_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "lot_id": row["id"], "title": row["title"],
            "seller_id": row["seller_id"], "status": row["status"],
            "reserve": row["reserve"]}


def resolve_deal_ref(conn: sqlite3.Connection, event_id: int,
                     ref: Any) -> sqlite3.Row:
    """Find one deal in an event by numeric pk or citable ``DL-...`` number."""
    row: sqlite3.Row | None = None
    rid = as_int(ref)
    if rid is not None:
        row = conn.execute(
            "SELECT * FROM deals WHERE event_id=? AND id=?",
            (event_id, rid)).fetchone()
    if row is None and ref is not None:
        row = conn.execute(
            "SELECT * FROM deals WHERE event_id=? AND deal_no=?",
            (event_id, str(ref).strip())).fetchone()
    if row is None:
        raise ApiError(404, "deal_not_found", "Deal not found in this event")
    return row


def lot_has_revoked_deal(conn: sqlite3.Connection, lot_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM deals WHERE lot_id=? AND status='revoked' LIMIT 1",
        (lot_id,),
    ).fetchone() is not None


def active_deal_for_lot(conn: sqlite3.Connection,
                        lot_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM deals WHERE lot_id=? AND status='active'",
        (lot_id,),
    ).fetchone()


def scoped_deal(conn: sqlite3.Connection, event_id: int, lot_id: int,
                deal_ref: Any) -> sqlite3.Row:
    """Resolve the deal an operation applies to.

    * An explicit deal reference must belong to this event *and this lot*.
    * Without a reference the active deal is used -- but only for lots that
      were never revoked.  Once a lot has revocation history an explicit
      reference is mandatory, so a legacy request addressing only the lot can
      never be reinterpreted as acting on the new buyer's deal.
    """
    if deal_ref is not None:
        deal = resolve_deal_ref(conn, event_id, deal_ref)
        if deal["lot_id"] != lot_id:
            raise ApiError(409, "deal_lot_mismatch",
                           "The referenced deal is for a different lot")
        return deal
    active = active_deal_for_lot(conn, lot_id)
    if active is None:
        raise ApiError(409, "lot_not_sold",
                       "This lot currently has no effective sale")
    if lot_has_revoked_deal(conn, lot_id):
        raise ApiError(
            409, "deal_reference_required",
            "This lot has a revoked sale; specify which deal (deal_id/deal_no) "
            "the operation applies to",
        )
    return active


def _active_deals_map(conn: sqlite3.Connection, event_id: int) -> dict[int, Any]:
    from .domain import Sale

    rows = conn.execute(
        "SELECT lot_id, buyer_id, price FROM deals "
        "WHERE event_id=? AND status='active'", (event_id,)).fetchall()
    return {r["lot_id"]: Sale(r["lot_id"], r["buyer_id"], int(r["price"]))
            for r in rows}


def _normalise_deal_status(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("active", "effective", "valid", "current"):
        return "active"
    if v in ("revoked", "cancelled", "canceled", "void", "reverted"):
        return "revoked"
    return None


def _normalise_offer_status(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()
    aliases = {
        "active": "active", "open": "active", "pending": "active",
        "effective": "active",
        "accepted": "accepted", "taken": "accepted",
        "withdrawn": "withdrawn", "revoked": "withdrawn",
        "cancelled": "withdrawn", "canceled": "withdrawn",
        "replaced": "replaced", "superseded": "replaced",
    }
    return aliases.get(v)


class ResaleMixin:
    # ----------------------------------------------------------- payloads
    def _deal_payload(self, conn: sqlite3.Connection, d: sqlite3.Row) -> dict:
        buyer = _user_brief(conn, d["buyer_id"])
        lot = _lot_brief(conn, d["lot_id"])
        seller = None
        if lot is not None:
            seller = _user_brief(conn, lot["seller_id"])
        offer = None
        if d["offer_id"] is not None:
            r = conn.execute("SELECT * FROM offers WHERE id=?",
                             (d["offer_id"],)).fetchone()
            offer = self._offer_payload(conn, r) if r is not None else None
        delivered = conn.execute(
            "SELECT delivery_id, winner_id, picker_id, delivered_at "
            "FROM delivery_items WHERE lot_id=? AND deal_id=?",
            (d["lot_id"], d["id"]),
        ).fetchone()
        payment = conn.execute(
            "SELECT id, serial_no, amount FROM payment_items "
            "WHERE lot_id=? AND deal_id=? AND active=1",
            (d["lot_id"], d["id"]),
        ).fetchone()
        payload = {
            "id": d["id"],
            "deal_id": d["id"],
            "deal_no": d["deal_no"],
            "reference": d["deal_no"],
            "event_id": d["event_id"],
            "lot_id": d["lot_id"],
            "lot": lot,
            "lot_title": lot["title"] if lot else None,
            "buyer": buyer,
            "buyer_id": d["buyer_id"],
            "winner_id": d["buyer_id"],
            "seller": seller,
            "seller_id": lot["seller_id"] if lot else None,
            "price": d["price"],
            "purchase_price": d["price"],
            "amount": d["price"],
            "kind": d["kind"],
            "source": d["kind"],
            "status": d["status"],
            "state": d["status"],
            "active": d["status"] == "active",
            "effective": d["status"] == "active",
            "revoked": d["status"] == "revoked",
            "offer_id": d["offer_id"],
            "offer": offer,
            "delivered": delivered is not None,
            "delivery_id": delivered["delivery_id"] if delivered else None,
            "delivered_at": delivered["delivered_at"] if delivered else None,
            "payable": payment is not None,
            "has_effective_payment": payment is not None,
            "payment_id": payment["id"] if payment else None,
            "payment_serial_no": payment["serial_no"] if payment else None,
            "created_at": d["created_at"],
            "revoked_at": d["revoked_at"],
            "revoked_by": d["revoked_by"],
            "revoked_by_user": _user_brief(conn, d["revoked_by"]),
            "revoke_reason": d["revoke_reason"] or None,
        }
        return payload

    def _offer_payload(self, conn: sqlite3.Connection, o: sqlite3.Row) -> dict:
        buyer = _user_brief(conn, o["buyer_id"])
        lot = _lot_brief(conn, o["lot_id"])
        return {
            "id": o["id"],
            "offer_id": o["id"],
            "event_id": o["event_id"],
            "lot_id": o["lot_id"],
            "lot": lot,
            "lot_title": lot["title"] if lot else None,
            "buyer": buyer,
            "buyer_id": o["buyer_id"],
            "designated_buyer_id": o["buyer_id"],
            "price": o["price"],
            "offer_price": o["price"],
            "status": o["status"],
            "state": o["status"],
            "active": o["status"] == "active",
            "effective": o["status"] == "active",
            "deal_id": o["deal_id"],
            "created_by": o["created_by"],
            "created_at": o["created_at"],
            "decided_at": o["decided_at"],
            "accepted_at": o["decided_at"] if o["status"] == "accepted" else None,
            "withdrawn_at": o["decided_at"] if o["status"] == "withdrawn" else None,
            "note": o["note"] or None,
        }

    # ------------------------------------------------------------ revocation
    def revoke_deal(self, event_id: int, actor_id: int, deal_ref: Any = None,
                    *, reason: str = "") -> tuple[dict, bool]:
        if deal_ref is None:
            raise ApiError(422, "invalid_request",
                           "A deal reference (deal_id or deal_no) is required")
        with self.db.tx() as conn:
            _event(conn, event_id)
            admin = self._require_admin(conn, event_id, actor_id)
            deal = resolve_deal_ref(conn, event_id, deal_ref)
            if deal["status"] == "revoked":
                # Replay: must not take effect a second time.
                payload = self._deal_payload(conn, deal)
                payload["replayed"] = True
                payload["idempotent_replay"] = True
                return payload, False
            delivered = conn.execute(
                "SELECT 1 FROM delivery_items WHERE lot_id=? AND deal_id=?",
                (deal["lot_id"], deal["id"]),
            ).fetchone()
            if delivered is not None:
                raise ApiError(
                    409, "already_delivered",
                    "The sale has already been delivered and cannot be revoked",
                )
            paid = conn.execute(
                "SELECT 1 FROM payment_items WHERE lot_id=? AND deal_id=? "
                "AND active=1", (deal["lot_id"], deal["id"]),
            ).fetchone()
            if paid is not None:
                raise ApiError(
                    409, "deal_paid",
                    "The sale still has an effective receipt; it cannot be "
                    "revoked",
                )
            now = _now()
            conn.execute(
                "UPDATE deals SET status='revoked', revoked_at=?, "
                "revoked_by=?, revoke_reason=? WHERE id=? AND status='active'",
                (now, admin["id"], str(reason or "")[:500], deal["id"]),
            )
            # Drop the (now obsolete) authorisation row: any delegated carrier/
            # driver and credentials derived for the revoked sale must not
            # carry over to a future re-sale.
            conn.execute("DELETE FROM pickup_auth WHERE lot_id=?",
                         (deal["lot_id"],))
            _audit(conn, event_id, admin["id"], "deal_revoked",
                   f"deal={deal['deal_no']} lot={deal['lot_id']} "
                   f"buyer={deal['buyer_id']} price={deal['price']}")
            row = conn.execute(
                "SELECT * FROM deals WHERE id=?", (deal["id"],)).fetchone()
            payload = self._deal_payload(conn, row)
            payload["replayed"] = False
            payload["idempotent_replay"] = False
            return payload, True

    revoke_sale = revoke_deal
    cancel_deal = revoke_deal

    # --------------------------------------------------------------- offers
    def create_offer(self, event_id: int, actor_id: int, lot_id: int,
                     buyer_ref: Any, price: Any, *, note: str = ""
                     ) -> tuple[dict, bool]:
        value = as_int(price)
        if value is None or value <= 0:
            raise ApiError(422, "invalid_amount",
                           "The offer price must be a positive integer")
        with self.db.tx() as conn:
            _event(conn, event_id)
            admin = self._require_admin(conn, event_id, actor_id)
            lot = conn.execute(
                "SELECT * FROM lots WHERE event_id=? AND id=?",
                (event_id, lot_id)).fetchone()
            if lot is None:
                raise ApiError(404, "lot_not_found", "Lot not found")
            if lot["status"] != "closed":
                raise ApiError(409, "lot_not_closed",
                               "An offer can only be issued for a closed lot")
            if active_deal_for_lot(conn, lot_id) is not None:
                raise ApiError(409, "lot_has_effective_deal",
                               "This lot still has an effective sale")
            if not lot_has_revoked_deal(conn, lot_id):
                raise ApiError(
                    409, "lot_not_revoked",
                    "Only a lot whose sale was revoked can be re-offered",
                )
            if buyer_ref is None:
                raise ApiError(422, "invalid_request",
                               "A designated buyer is required")
            buyer = resolve_account(conn, event_id, buyer_ref)
            if buyer["role"] != "buyer":
                raise ApiError(422, "invalid_buyer",
                               "The designated account must be a buyer")

            replaced = None
            prior = conn.execute(
                "SELECT * FROM offers WHERE lot_id=? AND status='active'",
                (lot_id,)).fetchone()
            if prior is not None:
                conn.execute(
                    "UPDATE offers SET status='replaced', decided_at=? "
                    "WHERE id=? AND status='active'",
                    (_now(), prior["id"]),
                )
                replaced = prior["id"]
            now = _now()
            cur = conn.execute(
                "INSERT INTO offers(event_id, lot_id, buyer_id, price, status, "
                "created_by, created_at, note) VALUES (?,?,?,?, 'active', ?,?,?)",
                (event_id, lot_id, buyer["id"], value, admin["id"], now,
                 str(note or "")[:500]),
            )
            offer_id = cur.lastrowid
            _audit(conn, event_id, admin["id"], "offer_created",
                   f"offer={offer_id} lot={lot_id} buyer={buyer['id']} "
                   f"price={value} replaced={replaced}")
            row = conn.execute(
                "SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            payload = self._offer_payload(conn, row)
            payload["replaced_offer_id"] = replaced
            payload["replayed"] = False
            return payload, True

    offer_resale = create_offer

    def _find_offer(self, conn: sqlite3.Connection, event_id: int,
                    ref: Any) -> sqlite3.Row:
        row = None
        rid = as_int(ref)
        if rid is not None:
            row = conn.execute(
                "SELECT * FROM offers WHERE event_id=? AND id=?",
                (event_id, rid)).fetchone()
        if row is None:
            raise ApiError(404, "offer_not_found", "Offer not found")
        return row

    def withdraw_offer(self, event_id: int, actor_id: int,
                       offer_ref: Any) -> tuple[dict, bool]:
        if offer_ref is None:
            raise ApiError(422, "invalid_request", "An offer id is required")
        with self.db.tx() as conn:
            _event(conn, event_id)
            admin = self._require_admin(conn, event_id, actor_id)
            offer = self._find_offer(conn, event_id, offer_ref)
            if offer["status"] == "withdrawn":
                payload = self._offer_payload(conn, offer)
                payload["replayed"] = True
                return payload, False
            if offer["status"] == "accepted":
                raise ApiError(409, "offer_already_accepted",
                               "An accepted offer cannot be withdrawn; revoke "
                               "the resulting deal instead")
            if offer["status"] != "active":
                raise ApiError(409, "offer_not_active",
                               f"The offer is already {offer['status']}")
            conn.execute(
                "UPDATE offers SET status='withdrawn', decided_at=? "
                "WHERE id=? AND status='active'",
                (_now(), offer["id"]),
            )
            _audit(conn, event_id, admin["id"], "offer_withdrawn",
                   f"offer={offer['id']} lot={offer['lot_id']}")
            row = conn.execute(
                "SELECT * FROM offers WHERE id=?", (offer["id"],)).fetchone()
            payload = self._offer_payload(conn, row)
            payload["replayed"] = False
            return payload, True

    cancel_offer = withdraw_offer

    def accept_offer(self, event_id: int, actor_id: int,
                     offer_ref: Any = None) -> tuple[dict, bool]:
        with self.db.tx() as conn:
            _event(conn, event_id)
            buyer = _user(conn, event_id, actor_id)
            if buyer["role"] != "buyer":
                raise ApiError(403, "forbidden", "Only buyers accept offers")
            offer_id = as_int(offer_ref)
            if offer_id is None and offer_ref is not None:
                raise ApiError(422, "invalid_request",
                               "A numeric offer id is required")
            if offer_id is None:
                # Only auto-resolve when exactly one active offer exists for
                # this buyer; never guess across buyers.
                rows = conn.execute(
                    "SELECT * FROM offers WHERE event_id=? AND buyer_id=? "
                    "AND status='active' ORDER BY id DESC",
                    (event_id, actor_id)).fetchall()
                if len(rows) != 1:
                    raise ApiError(422, "invalid_request",
                                   "Specify the offer id to accept")
                offer = rows[0]
            else:
                offer = conn.execute(
                    "SELECT * FROM offers WHERE event_id=? AND id=?",
                    (event_id, offer_id)).fetchone()
                if offer is None:
                    raise ApiError(404, "offer_not_found", "Offer not found")
            if offer["buyer_id"] != actor_id:
                raise ApiError(403, "forbidden",
                               "Only the designated buyer may accept this offer")
            if offer["status"] == "accepted":
                # Repeat accept -> the original deal; no new budget occupation.
                deal = conn.execute(
                    "SELECT * FROM deals WHERE id=?", (offer["deal_id"],
                                                       )).fetchone()
                payload = self._deal_payload(conn, deal)
                payload["offer"] = self._offer_payload(conn, offer)
                payload["replayed"] = True
                payload["idempotent_replay"] = True
                payload["created"] = False
                return payload, False
            if offer["status"] != "active":
                raise ApiError(409, "offer_not_active",
                               f"The offer is no longer active ({offer['status']})")
            if active_deal_for_lot(conn, offer["lot_id"]) is not None:
                raise ApiError(409, "lot_has_effective_deal",
                               "This lot already has an effective sale")

            budget = buyer["budget"]
            if budget is None:
                raise ApiError(409, "budget_required",
                               "Set your event budget before accepting an offer")
            from .domain import (Commitment, compute_commitments,
                                 evaluate_open_lots)
            from .service import _bids_by_lot, _lots

            lots = _lots(conn, event_id)
            bids = _bids_by_lot(conn, event_id)

            commitments = compute_commitments(
                lots, bids, evaluate_open_lots(lots, bids),
                _active_deals_map(conn, event_id),
            )
            used = (commitments.get(actor_id) or Commitment()).total
            if budget - used < offer["price"]:
                raise ApiError(
                    409, "budget_exceeded",
                    f"The price {offer['price']} exceeds your remaining budget "
                    f"{max(0, budget - used)}",
                )

            deal = create_deal(conn, event_id, offer["lot_id"], actor_id,
                               offer["price"], kind="resale",
                               offer_id=offer["id"])
            conn.execute(
                "UPDATE offers SET status='accepted', deal_id=?, decided_at=? "
                "WHERE id=? AND status='active'",
                (deal["id"], _now(), offer["id"]),
            )
            # Fresh authorisation for the new buyer.
            conn.execute("DELETE FROM pickup_auth WHERE lot_id=?",
                         (offer["lot_id"],))
            _audit(conn, event_id, actor_id, "offer_accepted",
                   f"offer={offer['id']} deal={deal['deal_no']} "
                   f"lot={offer['lot_id']} price={offer['price']}")
            payload = self._deal_payload(conn, deal)
            payload["offer"] = self._offer_payload(
                conn, conn.execute("SELECT * FROM offers WHERE id=?",
                                   (offer["id"],)).fetchone())
            payload["replayed"] = False
            payload["idempotent_replay"] = False
            payload["created"] = True
            return payload, True

    accept_resale_offer = accept_offer

    # ---------------------------------------------------------------- views
    def _deal_visible(self, lot, deal, actor) -> bool:
        if actor["role"] == "admin":
            return True
        if deal["buyer_id"] == actor["id"]:
            return True
        return lot["seller_id"] == actor["id"]

    def _offer_visible(self, lot, offer, actor) -> bool:
        if actor["role"] == "admin":
            return True
        if offer["buyer_id"] == actor["id"]:
            return True
        return lot["seller_id"] == actor["id"]

    def get_deal(self, event_id: int, actor_id: int, deal_ref: Any) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            deal = resolve_deal_ref(conn, event_id, deal_ref)
            lot = conn.execute("SELECT * FROM lots WHERE id=?",
                               (deal["lot_id"],)).fetchone()
            if not self._deal_visible(lot, deal, actor):
                raise ApiError(403, "forbidden", "You cannot view this deal")
            return self._deal_payload(conn, deal)

    def get_offer(self, event_id: int, actor_id: int, offer_ref: Any) -> dict:
        if offer_ref is None:
            raise ApiError(422, "invalid_request", "An offer id is required")
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            offer = self._find_offer(conn, event_id, offer_ref)
            lot = conn.execute("SELECT * FROM lots WHERE id=?",
                               (offer["lot_id"],)).fetchone()
            if not self._offer_visible(lot, offer, actor):
                raise ApiError(403, "forbidden", "You cannot view this offer")
            return self._offer_payload(conn, offer)

    def list_deals(self, event_id: int, actor_id: int, *,
                   lot_id: int | None = None,
                   status_filter: str | None = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            sql = ("SELECT d.* FROM deals d JOIN lots l ON l.id=d.lot_id "
                   "WHERE d.event_id=?")
            params: list[Any] = [event_id]
            if actor["role"] == "admin":
                pass
            elif actor["role"] == "seller":
                sql += " AND l.seller_id=?"
                params.append(actor_id)
            else:
                # Buyers (and other roles) see only deals naming them.
                sql += " AND d.buyer_id=?"
                params.append(actor_id)
            if lot_id is not None:
                sql += " AND d.lot_id=?"
                params.append(lot_id)
            status = _normalise_deal_status(status_filter)
            if status is not None:
                sql += " AND d.status=?"
                params.append(status)
            sql += " ORDER BY d.id"
            rows = conn.execute(sql, params).fetchall()
            deals = [self._deal_payload(conn, r) for r in rows]
            return {"event_id": event_id, "deals": deals, "items": deals,
                    "count": len(deals)}

    def list_offers(self, event_id: int, actor_id: int, *,
                    lot_id: int | None = None,
                    status_filter: str | None = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            sql = ("SELECT o.* FROM offers o JOIN lots l ON l.id=o.lot_id "
                   "WHERE o.event_id=?")
            params: list[Any] = [event_id]
            if actor["role"] == "admin":
                pass
            elif actor["role"] == "seller":
                sql += " AND l.seller_id=?"
                params.append(actor_id)
            else:
                sql += " AND o.buyer_id=?"
                params.append(actor_id)
            if lot_id is not None:
                sql += " AND o.lot_id=?"
                params.append(lot_id)
            status = _normalise_offer_status(status_filter)
            if status is not None:
                sql += " AND o.status=?"
                params.append(status)
            sql += " ORDER BY o.id"
            rows = conn.execute(sql, params).fetchall()
            offers = [self._offer_payload(conn, r) for r in rows]
            return {"event_id": event_id, "offers": offers, "items": offers,
                    "count": len(offers)}

    def lot_deals_view(self, event_id: int, actor_id: int, lot_id: int) -> dict:
        """Deal history + offers for one lot, visibility-filtered."""
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lot = conn.execute(
                "SELECT * FROM lots WHERE event_id=? AND id=?",
                (event_id, lot_id)).fetchone()
            if lot is None:
                raise ApiError(404, "lot_not_found", "Lot not found")
            deal_rows = conn.execute(
                "SELECT * FROM deals WHERE lot_id=? ORDER BY id",
                (lot_id,)).fetchall()
            offers_rows = conn.execute(
                "SELECT * FROM offers WHERE lot_id=? ORDER BY id",
                (lot_id,)).fetchall()
            deals = [self._deal_payload(conn, d) for d in deal_rows]
            offers = [self._offer_payload(conn, o) for o in offers_rows]
            visible = actor["role"] == "admin" \
                or lot["seller_id"] == actor["id"] \
                or any(d["buyer_id"] == actor["id"] for d in deals) \
                or any(o["buyer_id"] == actor["id"] for o in offers)
            if not visible:
                raise ApiError(403, "forbidden",
                               "You cannot view this lot's deals")
            if actor["role"] != "admin" and lot["seller_id"] != actor["id"]:
                deals = [d for d in deals if d["buyer_id"] == actor["id"]]
                offers = [o for o in offers if o["buyer_id"] == actor["id"]]
            return {
                "event_id": event_id,
                "lot_id": lot_id,
                "lot": _lot_brief(conn, lot_id),
                "deals": deals,
                "offers": offers,
                "current_deal": next(
                    (d for d in deals if d["status"] == "active"), None),
                "active_offer": next(
                    (o for o in offers if o["status"] == "active"), None),
                "count": len(deals),
            }
