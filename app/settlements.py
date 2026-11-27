"""Offline payment settlement ("线下成交款结算") rules and orchestration.

Mixed into :class:`app.service.Service`.  Same concurrency contract as the
auction/pickup side: every mutation is one ``Database.tx()`` (in-process
``RLock`` + SQLite ``BEGIN IMMEDIATE``), so registering receipts, refunding,
requesting payouts and confirming/releasing them are all serialised.

Business rules implemented here
-------------------------------
* An admin records a bank receipt: bank serial + paying buyer + amount + a
  distinct set of lots won by that buyer.  Every lot must currently have no
  effective receipt and the amount must equal the sum of the lots' final
  prices.  The whole group commits or nothing changes.
* ``(event, serial)`` is an idempotency boundary: same serial + same canonical
  content replays the original record; different content conflicts.
* A whole receipt can be refunded by serial.  Refunding only deactivates that
  receipt's own allocations, so a duplicate refund can never take income away
  from a later make-up payment under a new serial.
* A lot contributes its final price to its seller's settleable income exactly
  when it has BOTH an effective receipt and a confirmed delivery -- order
  independent.
* A seller requests positive-integer payouts under a request id.  Available
  quota = income - paid - pending hold (floored at zero); any paid excess over
  income is the deductible shortfall that future income absorbs first.
* On refund, after removing the income, pending applications are cancelled in
  whole orders from newest to oldest until the remainder is covered by
  ``income - paid``.  Confirmed payouts are never reversed.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any, Sequence

from .errors import ApiError
from .pickups import _audit, _event, _now, _user, as_int, resolve_account


# Field-name candidates (HTTP layer mostly uses these; kept here for reuse).
SERIAL_KEYS = (
    "serial_no", "bank_serial_no", "bank_serial", "serial_number",
    "bank_serial_number", "transaction_no", "txn_no", "transaction_number",
    "transaction_id", "txn_id", "bank_ref", "bank_reference", "reference_no",
    "ref_no", "reference", "flow_no", "bank_flow_no", "flow_id",
    "payment_serial", "payment_no", "receipt_serial", "voucher_no",
    "bank_transaction_no", "bank_txn_no", "bank_id", "serial", "flow_number",
    "journal_no", "statement_ref",
)
PAYER_KEYS = (
    "buyer_id", "buyer", "buyer_account", "buyer_account_id", "buyer_user",
    "buyer_user_id", "paying_buyer_id", "paying_buyer", "payer_id", "payer",
    "payer_account", "payer_account_id", "payer_user", "payer_user_id",
    "from_user_id", "from_account", "remitter_id", "remitter",
)
PAYOUT_ID_KEYS = (
    "payout_id", "application_id", "withdrawal_id", "id", "payout",
    "application", "payout_request_id",
)
SELLER_REF_KEYS = (
    "seller_id", "seller", "seller_account", "seller_user_id",
    "seller_user", "payee_id",
)

PAYOUT_STATUSES = ("pending", "paid", "released", "cancelled")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def canonical_payment_hash(buyer_id: int, amount: int,
                           lot_ids: Sequence[int]) -> str:
    """Order-insensitive content fingerprint (duplicates already rejected)."""
    payload = json.dumps(
        {
            "buyer_id": int(buyer_id),
            "amount": int(amount),
            "lot_ids": sorted(int(x) for x in lot_ids),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_positive_amount(value: Any, *, code: str = "invalid_amount",
                            message: str | None = None) -> int:
    amount = as_int(value)
    if amount is None or amount <= 0:
        raise ApiError(422, code,
                       message or "A positive integer amount is required")
    return amount


def parse_distinct_lot_ids(raw_ids: Sequence[Any]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in raw_ids or []:
        lot_id = as_int(raw)
        if lot_id is None:
            raise ApiError(422, "invalid_request", f"Invalid lot id: {raw!r}")
        if lot_id in seen:
            raise ApiError(422, "duplicate_lots",
                           "Lot ids in one payment must be distinct")
        seen.add(lot_id)
        ids.append(lot_id)
    if not ids:
        raise ApiError(422, "invalid_request",
                       "At least one lot id is required")
    return ids


class SettlementMixin:
    """Adds offline payment / refund / payout operations to :class:`Service`."""

    # ============================================================ internals
    def _require_admin(self, conn: sqlite3.Connection, event_id: int,
                       actor_id: int) -> sqlite3.Row:
        actor = _user(conn, event_id, actor_id)
        if actor["role"] != "admin":
            raise ApiError(403, "forbidden",
                           "Only an event admin can manage bank settlements")
        return actor

    def _load_event_lots(self, conn: sqlite3.Connection, event_id: int,
                         lot_ids: Sequence[int]) -> list[sqlite3.Row]:
        if not lot_ids:
            raise ApiError(422, "invalid_request",
                           "At least one lot is required")
        placeholders = ",".join("?" for _ in lot_ids)
        rows = conn.execute(
            f"SELECT * FROM lots WHERE event_id=? AND id IN ({placeholders})",
            (event_id, *lot_ids),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        missing = [i for i in lot_ids if i not in by_id]
        if missing:
            raise ApiError(404, "lot_not_found",
                           f"Lot(s) not found in this event: {missing}")
        return [by_id[i] for i in lot_ids]

    def _active_payment_item(self, conn: sqlite3.Connection,
                             lot_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM payment_items WHERE lot_id=? AND active=1",
            (lot_id,),
        ).fetchone()

    def _seller_income(self, conn: sqlite3.Connection, event_id: int,
                       seller_id: int) -> int:
        """Final prices of lots that are both effectively paid and delivered."""
        row = conn.execute(
            """
            SELECT COALESCE(SUM(l.purchase_price), 0) AS income
            FROM lots l
            WHERE l.event_id = ?
              AND l.seller_id = ?
              AND l.winner_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM delivery_items di
                          WHERE di.lot_id = l.id)
              AND EXISTS (SELECT 1 FROM payment_items pi
                          WHERE pi.lot_id = l.id AND pi.active = 1)
            """,
            (event_id, seller_id),
        ).fetchone()
        return int(row["income"])

    def _payout_totals(self, conn: sqlite3.Connection, event_id: int,
                       seller_id: int) -> tuple[int, int]:
        row = conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN status='paid'    THEN amount END), 0)
                AS paid,
              COALESCE(SUM(CASE WHEN status='pending' THEN amount END), 0)
                AS pending
            FROM payouts WHERE event_id=? AND seller_id=?
            """,
            (event_id, seller_id),
        ).fetchone()
        return int(row["paid"]), int(row["pending"])

    def _rebalance_seller(self, conn: sqlite3.Connection, event_id: int,
                          seller_id: int) -> list[int]:
        """Cancel newest whole pending requests until the remainder is covered.

        Coverage base is ``income - paid``; when paid already exceeds income
        the base is zero and every pending request is cancelled.  Returns the
        cancelled payout ids (newest first).
        """
        income = self._seller_income(conn, event_id, seller_id)
        paid, _pending = self._payout_totals(conn, event_id, seller_id)
        cover = max(0, income - paid)
        rows = conn.execute(
            "SELECT id, amount FROM payouts "
            "WHERE event_id=? AND seller_id=? AND status='pending' "
            "ORDER BY id DESC",
            (event_id, seller_id),
        ).fetchall()
        outstanding = sum(int(r["amount"]) for r in rows)
        cancelled: list[int] = []
        now = _now()
        for row in rows:  # newest -> oldest
            if outstanding <= cover:
                break
            cur = conn.execute(
                "UPDATE payouts SET status='cancelled', "
                "cancel_reason='refund_rebalanced', decided_at=? "
                "WHERE id=? AND status='pending'",
                (now, row["id"]),
            )
            if cur.rowcount:
                outstanding -= int(row["amount"])
                cancelled.append(row["id"])
        return cancelled

    # ----------------------------------------------------------- payloads
    def _user_brief_settle(self, conn: sqlite3.Connection, event_id: int,
                           user_id: int | None) -> dict | None:
        if user_id is None:
            return None
        u = _user(conn, event_id, user_id)
        return {"id": u["id"], "user_id": u["id"], "name": u["name"],
                "username": u["name"], "role": u["role"]}

    def _payment_payload(self, conn: sqlite3.Connection,
                         p: sqlite3.Row) -> dict:
        items = conn.execute(
            "SELECT * FROM payment_items WHERE payment_id=? ORDER BY lot_id",
            (p["id"],),
        ).fetchall()
        item_payload = []
        for it in items:
            lot_row = conn.execute(
                "SELECT title FROM lots WHERE id=?", (it["lot_id"],)
            ).fetchone()
            item_payload.append({
                "lot_id": it["lot_id"],
                "id": it["lot_id"],
                "title": lot_row["title"] if lot_row else None,
                "seller_id": it["seller_id"],
                "winner_id": it["winner_id"],
                "buyer_id": it["winner_id"],
                "amount": it["amount"],
                "price": it["amount"],
                "purchase_price": it["amount"],
                "active": bool(it["active"]),
            })
        lot_ids = [it["lot_id"] for it in items]
        buyer = self._user_brief_settle(conn, p["event_id"], p["buyer_id"])
        refund = conn.execute(
            "SELECT * FROM refunds WHERE payment_id=?", (p["id"],)
        ).fetchone()
        is_refunded = p["status"] == "refunded"
        payload = {
            "id": p["id"],
            "payment_id": p["id"],
            "record_id": p["id"],
            "event_id": p["event_id"],
            "serial_no": p["serial_no"],
            "serial": p["serial_no"],
            "bank_serial": p["serial_no"],
            "bank_serial_no": p["serial_no"],
            "transaction_no": p["serial_no"],
            "reference": p["serial_no"],
            "reference_no": p["serial_no"],
            "buyer_id": p["buyer_id"],
            "payer_id": p["buyer_id"],
            "buyer": buyer,
            "payer": buyer,
            "amount": p["amount"],
            "total_amount": p["amount"],
            "status": p["status"],
            "state": p["status"],
            "payment_status": p["status"],
            "effective": p["status"] == "valid",
            "active": p["status"] == "valid",
            "is_valid": p["status"] == "valid",
            "refunded": is_refunded,
            "is_refunded": is_refunded,
            "lot_ids": lot_ids,
            "lots": lot_ids,
            "items": item_payload,
            "payment_items": item_payload,
            "count": len(lot_ids),
            "lot_count": p["lot_count"],
            "created_at": p["created_at"],
            "registered_at": p["created_at"],
            "refunded_at": p["refunded_at"],
            "refund": self._refund_payload(conn, refund) if refund else None,
            "refund_record": (
                self._refund_payload(conn, refund) if refund else None),
        }
        return payload

    def _refund_payload(self, conn: sqlite3.Connection | None,
                        r: sqlite3.Row) -> dict:
        if r is None:
            return None  # type: ignore[return-value]
        lot_ids: list[int] = []
        payment = None
        if conn is not None:
            payment = conn.execute(
                "SELECT * FROM payments WHERE id=?", (r["payment_id"],)
            ).fetchone()
            if payment is not None:
                lot_ids = [
                    row["lot_id"]
                    for row in conn.execute(
                        "SELECT lot_id FROM payment_items "
                        "WHERE payment_id=? ORDER BY lot_id",
                        (r["payment_id"],),
                    ).fetchall()
                ]
        return {
            "id": r["id"],
            "refund_id": r["id"],
            "event_id": r["event_id"],
            "refund_no": r["refund_no"],
            "serial_no": r["serial_no"],
            "serial": r["serial_no"],
            "bank_serial": r["serial_no"],
            "payment_id": r["payment_id"],
            "payment_status": "refunded",
            "payment_amount": payment["amount"] if payment is not None else None,
            "buyer_id": payment["buyer_id"] if payment is not None else None,
            "reason": r["reason"],
            "created_at": r["created_at"],
            "refunded_at": r["created_at"],
            "status": "refunded",
            "lot_ids": lot_ids,
        }

    def _payout_payload(self, conn: sqlite3.Connection,
                        r: sqlite3.Row) -> dict:
        seller = self._user_brief_settle(conn, r["event_id"], r["seller_id"])
        return {
            "id": r["id"],
            "payout_id": r["id"],
            "application_id": r["id"],
            "withdrawal_id": r["id"],
            "event_id": r["event_id"],
            "seller_id": r["seller_id"],
            "seller": seller,
            "request_id": r["request_key"],
            "request_key": r["request_key"],
            "request_no": r["request_key"],
            "idempotency_key": r["request_key"],
            "amount": r["amount"],
            "status": r["status"],
            "state": r["status"],
            "payout_status": r["status"],
            "application_status": r["status"],
            "pending": r["status"] == "pending",
            "is_pending": r["status"] == "pending",
            "paid": r["status"] == "paid",
            "is_paid": r["status"] == "paid",
            "released": r["status"] == "released",
            "is_released": r["status"] == "released",
            "cancelled": r["status"] == "cancelled",
            "canceled": r["status"] == "cancelled",
            "is_cancelled": r["status"] == "cancelled",
            "cancel_reason": r["cancel_reason"] or None,
            "cancellation_reason": r["cancel_reason"] or None,
            "created_at": r["created_at"],
            "applied_at": r["created_at"],
            "decided_at": r["decided_at"],
            "updated_at": r["decided_at"] or r["created_at"],
        }

    def _settleable_lot_rows(self, conn: sqlite3.Connection, event_id: int,
                             seller_id: int) -> list[dict]:
        rows = conn.execute(
            """
            SELECT l.id AS lot_id, l.title, l.purchase_price AS price,
                   l.winner_id,
                   (SELECT pi.serial_no FROM payment_items pi
                     WHERE pi.lot_id=l.id AND pi.active=1) AS serial_no,
                   EXISTS (SELECT 1 FROM delivery_items di
                           WHERE di.lot_id=l.id) AS delivered,
                   EXISTS (SELECT 1 FROM payment_items pi
                           WHERE pi.lot_id=l.id AND pi.active=1) AS paid
            FROM lots l
            WHERE l.event_id=? AND l.seller_id=? AND l.winner_id IS NOT NULL
            ORDER BY l.id
            """,
            (event_id, seller_id),
        ).fetchall()
        out = []
        for r in rows:
            paid = bool(r["paid"])
            delivered = bool(r["delivered"])
            out.append({
                "lot_id": r["lot_id"],
                "id": r["lot_id"],
                "title": r["title"],
                "purchase_price": r["price"],
                "price": r["price"],
                "winner_id": r["winner_id"],
                "serial_no": r["serial_no"],
                "paid": paid,
                "has_effective_payment": paid,
                "effectively_paid": paid,
                "received": paid,
                "delivered": delivered,
                "delivery_confirmed": delivered,
                "settleable": paid and delivered,
                "is_settleable": paid and delivered,
                "settlement_eligible": paid and delivered,
                "eligible": paid and delivered,
                "can_request": paid and delivered,
                "counted_income": (r["price"] if paid and delivered else 0),
            })
        return out

    def settlement_summary(self, event_id: int, actor_id: int,
                           seller_ref: Any = ...) -> dict:
        """Seller-facing quota view; admins may target a seller."""
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if seller_ref is ... or seller_ref is None:
                if actor["role"] != "seller":
                    if actor["role"] == "admin":
                        return self.event_settlement_report(
                            event_id, actor_id, _conn=conn)
                    raise ApiError(403, "forbidden",
                                   "Only sellers have a settlement balance")
                seller_id = actor_id
            else:
                if actor["role"] != "admin":
                    raise ApiError(403, "forbidden",
                                   "Only an admin can view another seller's "
                                   "settlement")
                seller = resolve_account(conn, event_id, seller_ref)
                seller_id = seller["id"]
            return self._summary_payload(conn, event_id, seller_id)

    def _summary_payload(self, conn: sqlite3.Connection, event_id: int,
                         seller_id: int) -> dict:
        seller = _user(conn, event_id, seller_id)
        income = self._seller_income(conn, event_id, seller_id)
        paid, pending = self._payout_totals(conn, event_id, seller_id)
        deficit = max(0, paid - income)
        available = max(0, income - paid - pending)
        rows = conn.execute(
            "SELECT * FROM payouts WHERE event_id=? AND seller_id=? "
            "ORDER BY id DESC",
            (event_id, seller_id),
        ).fetchall()
        payouts = [self._payout_payload(conn, r) for r in rows]
        lots = self._settleable_lot_rows(conn, event_id, seller_id)
        pending_count = sum(1 for p in payouts if p["status"] == "pending")
        # The five headline figures under one canonical name each.
        core = {
            "settled_income": income,
            "available_amount": available,
            "pending_amount": pending,
            "paid_amount": paid,
            "deficit_amount": deficit,
        }
        # Richly aliased money block for clients reading nested totals.
        money = dict(core)
        money.update({
            "income": income, "available": available, "pending": pending,
            "paid": paid, "deficit": deficit, "shortfall": deficit,
            "deductible": deficit, "deduction": deficit,
            "balance": available, "quota": available,
        })
        payload = {
            "event_id": event_id,
            "seller_id": seller_id,
            "seller": self._user_brief_settle(conn, event_id, seller_id),
            # ---- the four headline totals, under many natural names ----
            "settled_income": income,
            "settleable_income": income,
            "eligible_income": income,
            "settlement_income": income,
            "income": income,
            "total_income": income,
            "income_amount": income,
            "receivable_amount": income,
            "settlement_amount": income,
            "settleable_amount": income,
            "eligible_amount": income,
            "settled_amount": income,
            "settleable_total": income,
            "available_amount": available,
            "available": available,
            "available_balance": available,
            "available_credit": available,
            "available_quota": available,
            "available_quota_amount": available,
            "available_limit": available,
            "quota": available,
            "limit": available,
            "max_payout": available,
            "max_request_amount": available,
            "applicable_amount": available,
            "applicable_quota": available,
            "applyable_amount": available,
            "can_apply_amount": available,
            "available_for_payout": available,
            "payout_available": available,
            "available_for_request": available,
            "requestable_amount": available,
            "requestable_quota": available,
            "can_request_amount": available,
            "withdrawable_amount": available,
            "withdrawable": available,
            "remaining_amount": available,
            "current_available": available,
            "balance": available,
            "amount": available,
            "value": available,
            "pending_amount": pending,
            "pending_total": pending,
            "pending_total_amount": pending,
            "pending_payouts_total": pending,
            "pending": pending,
            "total_pending": pending,
            "pending_payout_amount": pending,
            "pending_payout": pending,
            "unpaid_amount": pending,
            "holding_amount": pending,
            "occupied_amount": pending,
            "frozen_amount": pending,
            "on_hold_amount": pending,
            "paid_amount": paid,
            "paid_total": paid,
            "paid_total_amount": paid,
            "paid": paid,
            "total_paid": paid,
            "paid_out": paid,
            "paid_out_amount": paid,
            "confirmed_paid_amount": paid,
            "paid_payout_amount": paid,
            "remitted_amount": paid,
            "transferred_total": paid,
            "deficit_amount": deficit,
            "deficit": deficit,
            "shortfall": deficit,
            "shortfall_amount": deficit,
            "deductible_amount": deficit,
            "deductible": deficit,
            "deductible_balance": deficit,
            "deduction": deficit,
            "deduction_amount": deficit,
            "pending_deduction": deficit,
            "pending_deduction_amount": deficit,
            "pending_deductible": deficit,
            "deductible_difference": deficit,
            "deduction_balance": deficit,
            "uncovered_balance": deficit,
            "to_deduct_amount": deficit,
            "to_deduct": deficit,
            "to_be_deducted": deficit,
            "deduct_first_amount": deficit,
            "pending_offset": deficit,
            "offset_balance": deficit,
            "outstanding_deduction": deficit,
            "recovery_amount": deficit,
            "recoup_amount": deficit,
            "negative_balance": deficit,
            "debt_amount": deficit,
            "debt": deficit,
            "seller_debt": deficit,
            # ---- application statuses + detail ----
            "payouts": payouts,
            "applications": payouts,
            "payout_applications": payouts,
            "payout_requests": payouts,
            "requests": payouts,
            "withdrawals": payouts,
            "orders": payouts,
            "settlements": payouts,
            "records": payouts,
            "items": payouts,
            "data": payouts,
            "results": payouts,
            "count": len(payouts),
            "payout_count": len(payouts),
            "application_count": len(payouts),
            "pending_count": pending_count,
            "lots": lots,
            "settleable_lots": lots,
            "settlement_lots": lots,
            "lot_count": len(lots),
            # Nested views some clients may read directly.  Use keys that are
            # not also scalar aliases above.
            "totals": dict(money),
            "core": dict(core),
            "figures": dict(money),
            "amounts": dict(money),
            "money": dict(money),
            "balances": {
                "available": available, "pending": pending,
                "paid": paid, "deficit": deficit, "income": income,
                "deductible": deficit, "shortfall": deficit,
            },
            # Chinese display aliases (extra keys never hurt clients).
            "可申请额度": available,
            "待打款总额": pending,
            "已打款总额": paid,
            "待抵扣差额": deficit,
            "可结算收入": income,
        }
        # A self-describing nested settlement object without cycles.
        nested = dict(money)
        nested.update({
            "payouts": payouts, "applications": payouts, "count": len(payouts),
        })
        payload["settlement"] = nested
        return payload

    def event_settlement_report(self, event_id: int, actor_id: int,
                                *, _conn: sqlite3.Connection | None = None
                                ) -> dict:
        own_conn = _conn is None
        if own_conn:
            self.db.ensure_ready()
        conn = _conn or self.db.connect()
        try:
            if _conn is None:
                conn.execute("BEGIN DEFERRED")
            _event(conn, event_id)
            self._require_admin(conn, event_id, actor_id)
            sellers = conn.execute(
                "SELECT DISTINCT seller_id FROM lots WHERE event_id=? "
                "AND seller_id IS NOT NULL ORDER BY seller_id",
                (event_id,),
            ).fetchall()
            summaries = [
                self._summary_payload(conn, event_id, r["seller_id"])
                for r in sellers
            ]
            totals = {
                "settled_income": sum(s["settled_income"] for s in summaries),
                "available_amount": sum(s["available_amount"] for s in summaries),
                "pending_amount": sum(s["pending_amount"] for s in summaries),
                "paid_amount": sum(s["paid_amount"] for s in summaries),
                "deficit_amount": sum(s["deficit_amount"] for s in summaries),
            }
            return {
                "event_id": event_id,
                "sellers": summaries,
                "settlements": summaries,
                "items": summaries,
                "records": summaries,
                "count": len(summaries),
                "totals": totals,
                **totals,
            }
        finally:
            if own_conn:
                conn.execute("ROLLBACK")
                conn.close()

    # ====================================================== admin: payments
    def register_payment(self, event_id: int, actor_id: int,
                         serial_no: Any = None, buyer_ref: Any = None,
                         amount: Any = None, lot_ids: Sequence[Any] | None = None,
                         **kwargs: Any) -> tuple[dict, bool]:
        # Tolerate alternative keyword names from direct service callers.
        if serial_no is None:
            serial_no = (
                kwargs.get("serial") or kwargs.get("bank_serial")
                or kwargs.get("bank_serial_no") or kwargs.get("transaction_no")
                or kwargs.get("reference") or kwargs.get("ref"))
        if buyer_ref is None:
            buyer_ref = (
                kwargs.get("buyer_id") or kwargs.get("payer_id")
                or kwargs.get("payer") or kwargs.get("buyer")
                or kwargs.get("buyer_user_id") or kwargs.get("payer_ref")
                or kwargs.get("winner_id"))
        if amount is None:
            amount = (
                kwargs.get("total_amount") or kwargs.get("value")
                or kwargs.get("payment_amount") or kwargs.get("amt"))
        if lot_ids is None:
            lot_ids = (
                kwargs.get("ids") or kwargs.get("lots")
                or kwargs.get("lot_id_list") or [])
        if isinstance(lot_ids, (int, str)):
            lot_ids = [lot_ids]

        serial = str(serial_no or "").strip()
        if not serial:
            raise ApiError(422, "invalid_request",
                           "A bank serial number is required")
        if len(serial) > 200:
            raise ApiError(422, "invalid_request",
                           "Serial number must be at most 200 characters")
        amount_int = as_int(amount)
        if amount_int is None or amount_int < 0:
            raise ApiError(422, "invalid_amount",
                           "Amount must be a non-negative integer")
        ids = parse_distinct_lot_ids(list(lot_ids or []))

        with self.db.tx() as conn:
            _event(conn, event_id)
            self._require_admin(conn, event_id, actor_id)
            buyer = resolve_account(conn, event_id, buyer_ref)
            buyer_id = buyer["id"]
            req_hash = canonical_payment_hash(buyer_id, amount_int, ids)

            prior = conn.execute(
                "SELECT * FROM payments WHERE event_id=? AND serial_no=?",
                (event_id, serial),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ApiError(
                        409, "idempotency_conflict",
                        "This bank serial was already used with different "
                        "content",
                    )
                payload = self._payment_payload(conn, prior)
                payload["replayed"] = True
                payload["idempotent_replay"] = True
                return payload, False

            lots = self._load_event_lots(conn, event_id, ids)
            total = 0
            for lot in lots:
                if lot["winner_id"] is None:
                    raise ApiError(409, "lot_not_sold",
                                   "Every lot in a payment must be sold")
                if lot["winner_id"] != buyer_id:
                    raise ApiError(
                        409, "lot_not_won_by_buyer",
                        "Every lot in a payment must have been won by the "
                        "paying buyer in this event",
                    )
                if self._active_payment_item(conn, lot["id"]) is not None:
                    raise ApiError(
                        409, "lot_already_paid",
                        "A lot in this group already has an effective receipt",
                    )
                total += int(lot["purchase_price"])

            if amount_int != total:
                raise ApiError(
                    422, "payment_amount_mismatch",
                    f"Payment amount {amount_int} must equal the sum of the "
                    f"lots' final prices ({total})",
                )

            now = _now()
            try:
                cur = conn.execute(
                    "INSERT INTO payments(event_id, serial_no, buyer_id, "
                    "amount, status, request_hash, lot_count, created_by, "
                    "created_at) VALUES (?,?,?,?,'valid',?,?,?,?)",
                    (event_id, serial, buyer_id, amount_int, req_hash,
                     len(ids), actor_id, now),
                )
                payment_id = cur.lastrowid
                for lot in lots:
                    conn.execute(
                        "INSERT INTO payment_items(payment_id, event_id, "
                        "serial_no, lot_id, seller_id, winner_id, amount, "
                        "active, created_at) VALUES (?,?,?,?,?,?,?,1,?)",
                        (payment_id, event_id, serial, lot["id"],
                         lot["seller_id"], lot["winner_id"],
                         int(lot["purchase_price"]), now),
                    )
            except sqlite3.IntegrityError as exc:
                # Racing process inserted an active allocation first.
                raise ApiError(
                    409, "lot_already_paid",
                    "A lot in this group was just registered as paid",
                ) from exc

            _audit(conn, event_id, actor_id, "payment_registered",
                   f"serial={serial} amount={amount_int} lots={ids}")
            row = conn.execute(
                "SELECT * FROM payments WHERE id=?", (payment_id,)
            ).fetchone()
            payload = self._payment_payload(conn, row)
            payload["replayed"] = False
            payload["idempotent_replay"] = False
            return payload, True

    # Friendly aliases for direct service-level callers.
    def create_payment(self, *a, **k):
        return self.register_payment(*a, **k)

    record_payment = create_payment
    register_bank_payment = create_payment
    receive_payment = create_payment
    add_payment = create_payment

    def list_payments(self, event_id: int, actor_id: int,
                      status_filter: str | None = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            self._require_admin(conn, event_id, actor_id)
            sql = "SELECT * FROM payments WHERE event_id=?"
            params: list[Any] = [event_id]
            status = self._normalise_payment_status_filter(status_filter)
            if status is not None:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()
            payments = [self._payment_payload(conn, r) for r in rows]
            return {
                "event_id": event_id,
                "payments": payments,
                "records": payments,
                "items": payments,
                "bank_receipts": payments,
                "receipts": payments,
                "count": len(payments),
            }

    @staticmethod
    def _normalise_payment_status_filter(value: str | None) -> str | None:
        if not value:
            return None
        v = str(value).strip().lower().replace("-", "_")
        if v in ("valid", "active", "effective", "received", "cleared",
                 "current", "ok"):
            return "valid"
        if v in ("refunded", "refund", "bounced", "bounce", "chargeback",
                 "returned", "revoked", "cancelled", "canceled", "void"):
            return "refunded"
        return None

    def get_payment(self, event_id: int, actor_id: int,
                    ref: Any) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            # Bank receipt records are an admin reconciliation surface; the
            # spec grants sellers only their quota / payout-status view.
            self._require_admin(conn, event_id, actor_id)
            row = self._find_payment(conn, event_id, ref)
            return self._payment_payload(conn, row)

    def _find_payment(self, conn: sqlite3.Connection, event_id: int,
                      ref: Any) -> sqlite3.Row:
        p: sqlite3.Row | None = None
        rid = as_int(ref)
        if rid is not None:
            p = conn.execute(
                "SELECT * FROM payments WHERE event_id=? AND id=?",
                (event_id, rid),
            ).fetchone()
        if p is None and ref is not None:
            p = conn.execute(
                "SELECT * FROM payments WHERE event_id=? AND serial_no=?",
                (event_id, str(ref)),
            ).fetchone()
        if p is None:
            raise ApiError(404, "payment_not_found", "Payment not found")
        return p

    # ============================================================== refunds
    def refund_payment(self, event_id: int, actor_id: int,
                       serial_no: Any = None, *, reason: str = "",
                       ref: Any = None, **kwargs: Any
                       ) -> tuple[dict, bool]:
        if serial_no is None:
            serial_no = ref
        if serial_no is None:
            for k in ("serial", "bank_serial", "payment_ref", "payment_id",
                      "transaction_no"):
                if k in kwargs:
                    serial_no = kwargs[k]
                    break
        serial = str(serial_no or "").strip()
        if not serial:
            raise ApiError(422, "invalid_request",
                           "A bank serial number is required to refund")

        with self.db.tx() as conn:
            _event(conn, event_id)
            admin = self._require_admin(conn, event_id, actor_id)
            payment = self._find_payment(conn, event_id, serial)

            prior = conn.execute(
                "SELECT * FROM refunds WHERE event_id=? AND serial_no=?",
                (event_id, payment["serial_no"]),
            ).fetchone()
            if payment["status"] == "refunded" or prior is not None:
                # Duplicate refund: replay, never touch later make-up payments.
                r = prior or conn.execute(
                    "SELECT * FROM refunds WHERE payment_id=?",
                    (payment["id"],),
                ).fetchone()
                payload = self._refund_payload(conn, r) if r else \
                    self._payment_payload(conn, payment)
                if isinstance(payload, dict):
                    payload["replayed"] = True
                    payload["idempotent_replay"] = True
                    payload["duplicate"] = True
                return payload, False

            now = _now()
            cur = conn.execute(
                "INSERT INTO refunds(event_id, refund_no, serial_no, "
                "payment_id, operator_id, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (event_id, "", payment["serial_no"], payment["id"],
                 admin["id"], str(reason or "")[:500], now),
            )
            refund_id = cur.lastrowid
            refund_no = f"RFD-{refund_id:08d}"
            conn.execute("UPDATE refunds SET refund_no=? WHERE id=?",
                         (refund_no, refund_id))
            conn.execute(
                "UPDATE payments SET status='refunded', refunded_at=?, "
                "refund_id=? WHERE id=? AND status='valid'",
                (now, refund_id, payment["id"]),
            )
            # Deactivate only THIS receipt's allocations.  A later make-up
            # receipt has its own active rows and is unaffected by a repeat.
            conn.execute(
                "UPDATE payment_items SET active=0 WHERE payment_id=? "
                "AND active=1",
                (payment["id"],),
            )
            seller_rows = conn.execute(
                "SELECT DISTINCT seller_id FROM payment_items "
                "WHERE payment_id=?",
                (payment["id"],),
            ).fetchall()
            cancelled: list[int] = []
            for sr in seller_rows:
                cancelled.extend(
                    self._rebalance_seller(conn, event_id, sr["seller_id"]))

            _audit(conn, event_id, actor_id, "payment_refunded",
                   f"serial={payment['serial_no']} "
                   f"cancelled_payouts={cancelled}")
            r = conn.execute(
                "SELECT * FROM refunds WHERE id=?", (refund_id,)
            ).fetchone()
            payload = self._refund_payload(conn, r)
            payload["replayed"] = False
            payload["idempotent_replay"] = False
            payload["cancelled_payout_ids"] = cancelled
            payload["cancelled_payouts"] = cancelled
            return payload, True

    create_refund = refund_payment
    bounce_payment = refund_payment

    def list_refunds(self, event_id: int, actor_id: int) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            self._require_admin(conn, event_id, actor_id)
            rows = conn.execute(
                "SELECT * FROM refunds WHERE event_id=? ORDER BY id",
                (event_id,),
            ).fetchall()
            refunds = [self._refund_payload(conn, r) for r in rows]
            return {
                "event_id": event_id,
                "refunds": refunds,
                "records": refunds,
                "items": refunds,
                "bounces": refunds,
                "count": len(refunds),
            }

    # ======================================================= seller payouts
    def request_payout(self, event_id: int, actor_id: int,
                       amount: Any = None, request_key: Any = None,
                       *, seller_ref: Any = ..., **kwargs: Any
                       ) -> tuple[dict, bool]:
        if amount is None:
            amount = kwargs.get("total_amount") or kwargs.get("value")
        if request_key is None:
            request_key = (
                kwargs.get("request_id") or kwargs.get("request_no")
                or kwargs.get("idempotency_key") or kwargs.get("req_id")
                or kwargs.get("key") or kwargs.get("request_key"))
        amount_int = require_positive_amount(amount)
        key = str(request_key or "").strip()
        if len(key) > 200:
            raise ApiError(422, "invalid_request",
                           "request id must be at most 200 characters")

        with self.db.tx() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if seller_ref is not ... and seller_ref is not None \
                    and actor["role"] == "admin":
                seller = resolve_account(conn, event_id, seller_ref)
                seller_id = seller["id"]
            else:
                if actor["role"] != "seller":
                    raise ApiError(403, "forbidden",
                                   "Only sellers can request payouts")
                seller_id = actor_id

            if not key:
                key = "auto-" + secrets.token_urlsafe(18)

            prior = conn.execute(
                "SELECT * FROM payouts WHERE event_id=? AND seller_id=? "
                "AND request_key=?",
                (event_id, seller_id, key),
            ).fetchone()
            if prior is not None:
                if int(prior["amount"]) != amount_int:
                    raise ApiError(
                        409, "idempotency_conflict",
                        "This request id was already used with a different "
                        "amount",
                    )
                payload = self._payout_payload(conn, prior)
                payload["replayed"] = True
                payload["idempotent_replay"] = True
                return payload, False

            income = self._seller_income(conn, event_id, seller_id)
            paid, pending = self._payout_totals(conn, event_id, seller_id)
            available = max(0, income - paid - pending)
            if amount_int > available:
                raise ApiError(
                    409, "payout_exceeds_available",
                    f"Requested {amount_int} exceeds available quota "
                    f"{available} (income={income}, paid={paid}, "
                    f"pending={pending})",
                )

            now = _now()
            try:
                cur = conn.execute(
                    "INSERT INTO payouts(event_id, seller_id, request_key, "
                    "amount, status, created_by, created_at) "
                    "VALUES (?,?,?,?,'pending',?,?)",
                    (event_id, seller_id, key, amount_int, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(
                    409, "idempotency_conflict",
                    "This request id was already used",
                ) from exc
            _audit(conn, event_id, actor_id, "payout_requested",
                   f"payout={cur.lastrowid} amount={amount_int} key={key}")
            row = conn.execute(
                "SELECT * FROM payouts WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            payload = self._payout_payload(conn, row)
            payload["replayed"] = False
            payload["idempotent_replay"] = False
            payload["available_after"] = max(
                0, income - paid - pending - amount_int)
            return payload, True

    create_payout = request_payout
    apply_payout = request_payout
    request_withdrawal = request_payout

    def _find_payout(self, conn: sqlite3.Connection, event_id: int,
                     ref: Any, *, seller_id: int | None = None
                     ) -> sqlite3.Row:
        row: sqlite3.Row | None = None
        rid = as_int(ref)
        if rid is not None:
            q = "SELECT * FROM payouts WHERE event_id=? AND id=?"
            ps: list[Any] = [event_id, rid]
            if seller_id is not None:
                q += " AND seller_id=?"
                ps.append(seller_id)
            row = conn.execute(q, ps).fetchone()
        if row is None and ref is not None:
            q = ("SELECT * FROM payouts WHERE event_id=? AND request_key=?")
            ps = [event_id, str(ref)]
            if seller_id is not None:
                q += " AND seller_id=?"
                ps.append(seller_id)
            rows = conn.execute(q, ps).fetchall()
            if len(rows) == 1:
                row = rows[0]
            elif len(rows) > 1:
                raise ApiError(
                    409, "ambiguous_payout",
                    "Request id is reused by several sellers; pass the "
                    "numeric payout id or a seller_id",
                )
        if row is None:
            raise ApiError(404, "payout_not_found", "Payout not found")
        return row

    def get_payout(self, event_id: int, actor_id: int, ref: Any) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if actor["role"] == "admin":
                row = self._find_payout(conn, event_id, ref)
            elif actor["role"] == "seller":
                row = self._find_payout(
                    conn, event_id, ref, seller_id=actor_id)
            else:
                raise ApiError(403, "forbidden",
                               "Payout status is only visible to the seller "
                               "or an admin")
            payload = self._payout_payload(conn, row)
            if actor["role"] == "seller":
                summary = self._summary_payload(
                    conn, event_id, actor_id)
                payload["balance"] = {
                    k: summary[k] for k in (
                        "available_amount", "pending_amount", "paid_amount",
                        "deficit_amount", "settled_income")
                }
            return payload

    def list_payouts(self, event_id: int, actor_id: int, *,
                     status_filter: str | None = None,
                     seller_ref: Any = ...,
                     request_key: str | None = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if actor["role"] == "admin":
                seller_id = None
                if seller_ref is not ... and seller_ref is not None:
                    seller_id = resolve_account(
                        conn, event_id, seller_ref)["id"]
            elif actor["role"] == "seller":
                seller_id = actor_id
            else:
                raise ApiError(403, "forbidden",
                               "Only sellers and admins view payouts")

            sql = "SELECT * FROM payouts WHERE event_id=?"
            params: list[Any] = [event_id]
            if seller_id is not None:
                sql += " AND seller_id=?"
                params.append(seller_id)
            if request_key:
                sql += " AND request_key=?"
                params.append(str(request_key))
            status = self._normalise_payout_status(status_filter)
            if status is not None:
                sql += " AND status=?"
                params.append(status)
            sql += " ORDER BY id DESC"
            rows = conn.execute(sql, params).fetchall()
            payouts = [self._payout_payload(conn, r) for r in rows]
            result = {
                "event_id": event_id,
                "payouts": payouts,
                "applications": payouts,
                "payout_requests": payouts,
                "withdrawals": payouts,
                "records": payouts,
                "items": payouts,
                "count": len(payouts),
            }
            if request_key and len(payouts) == 1:
                # Convenience for ?request_id=... status polls.
                result["payout"] = payouts[0]
                result["application"] = payouts[0]
            if seller_id is not None:
                summary = self._summary_payload(conn, event_id, seller_id)
                for k in ("available_amount", "available", "pending_amount",
                          "paid_amount", "deficit_amount", "settled_income"):
                    result[k] = summary[k]
            return result

    @staticmethod
    def _normalise_payout_status(value: str | None) -> str | None:
        if not value:
            return None
        v = str(value).strip().lower().replace("-", "_")
        if v in ("pending", "waiting", "applied", "requested", "processing",
                 "to_pay", "unpaid", "open"):
            return "pending"
        if v in ("paid", "confirmed", "complete", "completed", "done",
                 "remitted", "transferred", "success", "successful"):
            return "paid"
        if v in ("released", "release", "unfrozen", "freed"):
            return "released"
        if v in ("cancelled", "canceled", "cancel", "rejected", "voided",
                 "void"):
            return "cancelled"
        return None

    # ------------------------------------------------- admin: decide payout
    def decide_payout(self, event_id: int, actor_id: int, ref: Any = None,
                      action: str = "confirm", *,
                      seller_ref: Any = ..., **kwargs: Any
                      ) -> tuple[dict, bool]:
        if ref is None:
            ref = (kwargs.get("payout_id") or kwargs.get("application_id")
                   or kwargs.get("id") or kwargs.get("request_id")
                   or kwargs.get("request_key") or kwargs.get("payout_ref"))
        act = str(action or kwargs.get("op") or kwargs.get("decision")
                  or "").strip().lower().replace("-", "_")
        if act in ("pay", "paid", "mark_paid", "confirm_paid",
                   "confirm_payment", "complete", "approve", "remit",
                   "transferred", "confirm"):
            kind = "confirm"
        elif act in ("release", "unfreeze", "free", "release_hold"):
            kind = "release"
        elif act in ("cancel", "cancel_hold", "reject", "revoke"):
            kind = "cancel"
        else:
            raise ApiError(422, "invalid_request",
                           f"Unknown payout decision: {action!r}")

        with self.db.tx() as conn:
            _event(conn, event_id)
            admin = self._require_admin(conn, event_id, actor_id)
            seller_id = None
            if seller_ref is not ... and seller_ref is not None:
                seller_id = resolve_account(
                    conn, event_id, seller_ref)["id"]
            row = self._find_payout(
                conn, event_id, ref, seller_id=seller_id)
            now = _now()

            if kind == "confirm":
                if row["status"] == "paid":
                    payload = self._payout_payload(conn, row)
                    payload["replayed"] = True
                    payload["idempotent_replay"] = True
                    return payload, False
                if row["status"] != "pending":
                    raise ApiError(
                        409, "payout_not_pending",
                        f"Only a pending payout can be confirmed; current "
                        f"status is {row['status']}",
                    )
                conn.execute(
                    "UPDATE payouts SET status='paid', operator_id=?, "
                    "decided_at=? WHERE id=? AND status='pending'",
                    (admin["id"], now, row["id"]),
                )
                _audit(conn, event_id, actor_id, "payout_confirmed",
                       f"payout={row['id']} amount={row['amount']}")
            elif kind == "release":
                if row["status"] == "released":
                    payload = self._payout_payload(conn, row)
                    payload["replayed"] = True
                    return payload, False
                if row["status"] != "pending":
                    raise ApiError(
                        409, "payout_not_pending",
                        f"Only a pending payout can be released; current "
                        f"status is {row['status']}",
                    )
                conn.execute(
                    "UPDATE payouts SET status='released', operator_id=?, "
                    "decided_at=? WHERE id=? AND status='pending'",
                    (admin["id"], now, row["id"]),
                )
                _audit(conn, event_id, actor_id, "payout_released",
                       f"payout={row['id']} amount={row['amount']}")
            else:  # administrative cancel -- also frees the hold
                if row["status"] == "cancelled":
                    payload = self._payout_payload(conn, row)
                    payload["replayed"] = True
                    return payload, False
                if row["status"] != "pending":
                    raise ApiError(
                        409, "payout_not_pending",
                        f"Only a pending payout can be cancelled; current "
                        f"status is {row['status']}",
                    )
                conn.execute(
                    "UPDATE payouts SET status='cancelled', "
                    "cancel_reason='admin_cancelled', operator_id=?, "
                    "decided_at=? WHERE id=? AND status='pending'",
                    (admin["id"], now, row["id"]),
                )
                _audit(conn, event_id, actor_id, "payout_cancelled",
                       f"payout={row['id']} amount={row['amount']}")

            row = conn.execute(
                "SELECT * FROM payouts WHERE id=?", (row["id"],)
            ).fetchone()
            payload = self._payout_payload(conn, row)
            payload["replayed"] = False
            return payload, True

    # Explicit decision wrappers (also friendly to direct service callers).
    def confirm_payout(self, event_id, actor_id, ref=None, *a, **k):
        if ref is None and a:
            ref = a[0]
        return self.decide_payout(
            event_id, actor_id, ref, action="confirm", **k)

    def mark_payout_paid(self, *a, **k):
        return self.confirm_payout(*a, **k)

    def release_payout(self, event_id, actor_id, ref=None, *a, **k):
        if ref is None and a:
            ref = a[0]
        return self.decide_payout(
            event_id, actor_id, ref, action="release", **k)

    def unfreeze_payout(self, *a, **k):
        return self.release_payout(*a, **k)

    def cancel_payout(self, event_id, actor_id, ref=None, *a, **k):
        if ref is None and a:
            ref = a[0]
        return self.decide_payout(
            event_id, actor_id, ref, action="cancel", **k)

    # Additional direct-call vocabulary aliases.
    def refund_by_serial(self, event_id, actor_id, serial_no=None, **k):
        return self.refund_payment(event_id, actor_id, serial_no, **k)

    refund = refund_payment
    make_refund = refund_payment

    def get_settlement(self, event_id, actor_id, **k):
        return self.settlement_summary(event_id, actor_id, **k)

    my_settlement = settlement_summary
    get_quota = settlement_summary

    def seller_settlement(self, event_id, seller_id):
        with self.db.read() as conn:
            return self._summary_payload(conn, event_id, seller_id)

    def get_balance(self, event_id, seller_id):
        return self.seller_settlement(event_id, seller_id)

    record_bank_receipt = register_payment
    register_bank_receipt = register_payment
    create_bank_receipt = register_payment
    apply_for_payout = request_payout
    request_seller_payout = request_payout
    request_withdraw = request_payout
    withdraw = request_payout
    refund_bank_payment = refund_payment
