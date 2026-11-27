"""Post-sale pickup / handover ("交割 / 领货") rules and orchestration.

This module is mixed into :class:`app.service.Service`.  Everything here follows
the same concurrency contract as the auction side: every mutation is one
``Database.tx()`` (in-process ``RLock`` + SQLite ``BEGIN IMMEDIATE``), so a
delivery confirmation and a delegation/driver change are fully serialised.

Invariants implemented here
---------------------------
* Only sold lots (``lots.winner_id IS NOT NULL``) participate.
* Effective physical picker of a lot is exactly one person:
  driver if present, else carrier if present, else the winner.
* Every authorisation change bumps ``auth_version``; credentials are an HMAC
  binding (event, lot, version, picker), so any re-delegation / revocation /
  driver change invalidates previously issued credentials -- even when the new
  authorisation names the same picker.  Per-lot versions leave unchanged lots'
  credentials valid.
* Batch mutations validate *every* item before writing anything, so any
  ineligible item leaves the whole batch untouched.
* A delivery is all-or-nothing, keyed by (event, seller, request id); replaying
  the same canonical content returns the original receipt, different content
  conflicts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from .errors import ApiError

CREDENTIAL_PREFIX = "pck_"

# Account-reference keys accepted from lenient HTTP payloads, in priority order.
CARRIER_KEYS = (
    "carrier_id", "carrier", "carrier_account", "carrier_account_id",
    "carrier_user", "carrier_user_id", "trustee_id", "trustee",
    "entrusted_to", "entrust_to", "delegate_id", "delegate", "delegate_to",
)
DRIVER_KEYS = (
    "driver_id", "driver", "driver_account", "driver_account_id",
    "driver_user", "driver_user_id", "designated_driver",
)
PICKER_KEYS = (
    "picker_id", "picker", "pickup_account", "pickup_account_id",
    "pickup_user", "pickup_user_id", "pickup", "pickup_person_id",
    "pickup_person", "actual_picker", "actual_picker_id",
    "authorized_picker", "receiver_id", "receiver", "recipient_id",
    "recipient", "collected_by", "collector_id", "collector",
    "account_id", "account", "username", "user_name", "name",
)
REQUEST_ID_KEYS = (
    "request_id", "idempotency_key", "idempotency_key", "request_no",
    "request_number", "client_request_id", "req_id", "requestid",
)
LOT_ID_KEYS = (
    "lot_ids", "lots_ids", "lot_id_list", "ids", "lot_id", "lotIds",
)
ITEM_CONTAINER_KEYS = (
    "items", "delivery_items", "deliveries", "lot_items", "lines",
    "lots", "records",
)
CREDENTIAL_KEYS = (
    "credential", "pickup_credential", "verification_credential",
    "delivery_credential", "code", "pickup_code", "verification_code",
    "confirmation_code", "token", "proof", "cred", "pickup_token",
    "auth_code", "authorization_code", "verify_code", "verification_token",
    "pickup_verification_code", "verification", "pickup_verification",
)
CREDENTIAL_ARRAY_KEYS = (
    "credentials", "codes", "pickup_codes", "verification_codes", "tokens",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(conn: sqlite3.Connection, event_id: int | None, actor_id: int | None,
           action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(event_id, actor_id, action, detail, created_at) "
        "VALUES (?,?,?,?,?)",
        (event_id, actor_id, action, detail, _now()),
    )


def _event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ApiError(404, "event_not_found", "Event not found")
    return row


def _user(conn: sqlite3.Connection, event_id: int, user_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM users WHERE event_id=? AND id=?", (event_id, user_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "user_not_found", "User not found")
    return row


# ---------------------------------------------------------------------------
# Lenient request extraction (used by the HTTP layer)
# ---------------------------------------------------------------------------
def first_present(data: Any, keys: Sequence[str]) -> Any:
    if not isinstance(data, dict):
        return None
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
    return None


def extract_account_ref(data: dict, keys: Sequence[str]) -> Any:
    """Find an account reference given many possible field names.

    The returned value is an int / numeric string / name string / ``{"id"|"name"}``
    dict; :func:`resolve_account` turns it into a user row.
    """
    value = first_present(data, keys)
    if value is None:
        # Maybe the payload itself is {"carrier": {"name": "c1"}} handled here;
        # otherwise a generic nested "account" object.
        value = first_present(data, ("account_ref", "account_reference", "to_user"))
    return value


def extract_lot_ids(data: dict) -> list[int]:
    """Extract an ordered list of lot ids from many plausible payload shapes."""
    raw = first_present(data, LOT_ID_KEYS)
    if raw is None:
        raw = first_present(data, ("lotIds", "lotID", "lot_id_list"))
    ids: list[int] = []
    if isinstance(raw, str) and "," in raw:
        for part in raw.split(","):
            lot_id = as_int(part.strip())
            if lot_id is not None:
                ids.append(lot_id)
        return ids
    if isinstance(raw, list):
        for element in raw:
            if isinstance(element, dict):
                v = first_present(element, ("lot_id", "id", "lotId", "lot"))
                lot_id = as_int(v)
            else:
                lot_id = as_int(element)
            if lot_id is not None:
                ids.append(lot_id)
    elif raw is not None:
        lot_id = as_int(raw)
        if lot_id is not None:
            ids.append(lot_id)
    return ids


def _credential_from_object(obj: dict) -> str | None:
    value = first_present(obj, CREDENTIAL_KEYS)
    return None if value is None else str(value)


def extract_delivery_items(data: dict) -> list[dict]:
    """Normalise delivery payloads.

    Supported shapes include::

        {"items": [{"lot_id": 1, "credential": "pck_..."}]}
        {"lots": [{"lot_id": 1, "code": "..."}]}
        {"lot_ids": [1, 2], "credentials": ["c1", "c2"]}
        {"items": [1, 2], "credentials": ["c1", "c2"]}
    """
    items: list[dict] = []
    container = first_present(data, ITEM_CONTAINER_KEYS)
    if isinstance(container, list) and container and all(
        isinstance(x, dict) for x in container
    ):
        for element in container:
            lot_id = as_int(first_present(
                element, ("lot_id", "id", "lotId", "lot", "lotID")))
            cred = _credential_from_object(element)
            if lot_id is not None:
                items.append({"lot_id": lot_id, "credential": cred})
        if items:
            return items

    # A container expressed as a bare id list ("items": [1, 2]).
    bare_ids: list[int] = []
    if isinstance(container, list):
        for element in container:
            lot_id = as_int(element)
            if lot_id is not None:
                bare_ids.append(lot_id)
    lot_ids = bare_ids or extract_lot_ids(data)
    cred_array = first_present(data, CREDENTIAL_ARRAY_KEYS)
    if not isinstance(cred_array, list):
        cred_array = None
    single_cred = first_present(data, CREDENTIAL_KEYS)

    for idx, lot_id in enumerate(lot_ids):
        cred: Any = None
        if cred_array is not None and idx < len(cred_array):
            cred = cred_array[idx]
        elif single_cred is not None and len(lot_ids) == 1:
            cred = single_cred
        items.append({
            "lot_id": lot_id,
            "credential": None if cred is None else str(cred),
        })

    credentials_map = first_present(data, (
        "credentials_by_lot", "credential_map", "lot_credentials",
        "credentials_map", "codes_by_lot"))
    if isinstance(credentials_map, dict):
        for item in items:
            if item["credential"] is None:
                v = credentials_map.get(str(item["lot_id"]))
                if v is not None:
                    item["credential"] = str(v)
    return items


def extract_request_key(data: dict, headers: Any = None) -> str | None:
    value = first_present(data, REQUEST_ID_KEYS)
    if value is None and headers is not None:
        for h in ("idempotency-key", "x-request-id", "request-id"):
            try:
                value = headers.get(h)
            except Exception:  # pragma: no cover
                value = None
            if value:
                break
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 200:
        raise ApiError(422, "invalid_request",
                       "request id must be at most 200 characters")
    return text


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def compute_credential(secret: str, event_id: int, lot_id: int,
                       version: int, picker_id: int,
                       deal_id: int | None = None) -> str:
    # ``deal_id`` binds a credential to one specific sale, so a credential
    # issued for a revoked sale can never verify against a re-sale -- even
    # when the same lot, picker and auth version recur.
    msg = (f"pickup-credential:v1:{event_id}:{lot_id}:{deal_id}:"
           f"{version}:{picker_id}").encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    return CREDENTIAL_PREFIX + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def same_credential(a: str | None, b: str) -> bool:
    if not a:
        return False
    return hmac.compare_digest(str(a), b)


def canonical_delivery_hash(picker_id: int,
                            items: Sequence[dict]) -> str:
    canonical = sorted(
        (int(i["lot_id"]), str(i.get("credential") or ""),
         None if i.get("deal_ref") in (None, "") else str(i.get("deal_ref")))
        for i in items
    )
    payload = json.dumps(
        {"picker_id": picker_id, "items": canonical},
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_account(conn: sqlite3.Connection, event_id: int, ref: Any) -> sqlite3.Row:
    """Resolve an account reference (id / numeric string / name / object)."""
    candidate_id: int | None = None
    candidate_name: str | None = None

    if isinstance(ref, sqlite3.Row):
        return ref
    if isinstance(ref, dict):
        candidate_id = as_int(first_present(
            ref, ("id", "user_id", "account_id", "uid")))
        name = first_present(ref, ("name", "username", "user_name", "account"))
        if name is not None:
            candidate_name = str(name)
    elif isinstance(ref, (str, int)):
        text = str(ref).strip()
        if text.lstrip("-").isdigit():
            candidate_id = int(text)
        # Always allow falling back to an exact (possibly numeric) user name.
        candidate_name = text

    if candidate_id is not None:
        row = conn.execute(
            "SELECT * FROM users WHERE event_id=? AND id=?",
            (event_id, candidate_id),
        ).fetchone()
        if row is not None:
            return row
    if candidate_name is not None:
        row = conn.execute(
            "SELECT * FROM users WHERE event_id=? AND name=?",
            (event_id, candidate_name),
        ).fetchone()
        if row is not None:
            return row
    raise ApiError(404, "user_not_found",
                   "Referenced account does not exist in this event")


def unique_lot_ids(lot_ids: Sequence[Any]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in lot_ids:
        lot_id = as_int(raw)
        if lot_id is None:
            raise ApiError(422, "invalid_request", f"Invalid lot id: {raw!r}")
        if lot_id not in seen:
            seen.add(lot_id)
            out.append(lot_id)
    if not out:
        raise ApiError(422, "invalid_request", "At least one lot id is required")
    return out


class PickupMixin:
    """Adds post-sale pickup / delivery operations to :class:`Service`."""

    # ------------------------------------------------------------ internals
    def _effective_deal(self, conn: sqlite3.Connection,
                        lot: sqlite3.Row) -> sqlite3.Row | None:
        """The sale currently governing this lot's pickup state."""
        row = conn.execute(
            "SELECT * FROM deals WHERE lot_id=? AND status='active'",
            (lot["id"],),
        ).fetchone()
        return row

    def _deal_has_revocation(self, conn: sqlite3.Connection,
                             lot_id: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM deals WHERE lot_id=? AND status='revoked' LIMIT 1",
            (lot_id,),
        ).fetchone() is not None

    def _scoped_deal(self, conn: sqlite3.Connection, event_id: int,
                     lot: sqlite3.Row, deal_ref: Any) -> sqlite3.Row | None:
        """Which sale a per-lot operation applies to.

        Explicit references must name a deal of this event on this lot.  With
        no reference the effective deal is used, but only while the lot has no
        revocation history: after a revoke the caller must say which deal, so a
        legacy lot-only request is never silently applied to the new buyer.
        """
        if deal_ref is not None:
            row = None
            rid = as_int(deal_ref)
            if rid is not None:
                row = conn.execute(
                    "SELECT * FROM deals WHERE event_id=? AND id=?",
                    (event_id, rid)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM deals WHERE event_id=? AND deal_no=?",
                    (event_id, str(deal_ref).strip())).fetchone()
            if row is None:
                raise ApiError(404, "deal_not_found",
                               "Deal not found in this event")
            if row["lot_id"] != lot["id"]:
                raise ApiError(409, "deal_lot_mismatch",
                               "The referenced deal belongs to a different lot")
            return row
        deal = self._effective_deal(conn, lot)
        if deal is None:
            return None
        if self._deal_has_revocation(conn, lot["id"]):
            raise ApiError(
                409, "deal_reference_required",
                "This lot has a revoked sale; specify the deal "
                "(deal_id/deal_no) the operation applies to",
            )
        return deal

    def _auth_state(self, conn: sqlite3.Connection, lot: sqlite3.Row,
                    deal: sqlite3.Row | None = None) -> dict:
        if deal is None:
            deal = self._effective_deal(conn, lot)
        row = conn.execute(
            "SELECT * FROM pickup_auth WHERE lot_id=?", (lot["id"],)
        ).fetchone()
        # An authorisation row belongs to the sale it was created for.  A row
        # left over from a revoked/other deal is ignored, so stale delegations
        # never carry over to a re-sale.
        if row is not None and deal is not None \
                and row["deal_id"] not in (None, deal["id"]):
            row = None
        winner_id = deal["buyer_id"] if deal is not None else lot["winner_id"]
        if row is None:
            return {
                "row": None, "deal": deal, "deal_id": (
                    deal["id"] if deal is not None else None),
                "winner_id": winner_id,
                "carrier_id": None, "driver_id": None, "version": 1,
            }
        return {
            "row": row, "deal": deal,
            "deal_id": (deal["id"] if deal is not None else row["deal_id"]),
            "winner_id": row["winner_id"],
            "carrier_id": row["carrier_id"], "driver_id": row["driver_id"],
            "version": row["auth_version"],
        }

    @staticmethod
    def _picker_of(state: dict) -> tuple[int, str]:
        if state["driver_id"] is not None:
            return state["driver_id"], "driver"
        if state["carrier_id"] is not None:
            return state["carrier_id"], "carrier"
        return state["winner_id"], "winner"

    def _delivered_row(self, conn: sqlite3.Connection,
                       lot_id: int,
                       deal_id: int | None = None) -> sqlite3.Row | None:
        sql = (
            "SELECT di.*, d.request_key, d.receipt_no, "
            "d.created_at AS receipt_created "
            "FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id "
            "WHERE di.lot_id=?"
        )
        params: list[Any] = [lot_id]
        if deal_id is not None:
            sql += " AND di.deal_id=?"
            params.append(deal_id)
        return conn.execute(sql, params).fetchone()

    @staticmethod
    def _brief(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "user_id": row["id"],
            "name": row["name"],
            "username": row["name"],
            "role": row["role"],
        }

    def _user_brief(self, conn: sqlite3.Connection, event_id: int,
                    user_id: int | None) -> dict | None:
        if user_id is None:
            return None
        return self._brief(_user(conn, event_id, user_id))

    def _load_batch_lots(self, conn: sqlite3.Connection, event_id: int,
                         lot_ids: Sequence[int]) -> list[sqlite3.Row]:
        if not lot_ids:
            raise ApiError(422, "invalid_request", "At least one lot is required")
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

    def _validate_trustee_target(self, conn, event_id, target_ref, lots, actor_id):
        target = resolve_account(conn, event_id, target_ref)
        for lot in lots:
            winner_id = self._effective_deal(conn, lot)
            winner = winner_id["buyer_id"] if winner_id is not None \
                else lot["winner_id"]
            forbidden = {lot["seller_id"], winner, actor_id}
            if target["id"] in forbidden:
                raise ApiError(
                    422, "invalid_trustee",
                    "The entrusted account cannot be this lot's seller, winner, "
                    "or the acting account",
                )
        return target

    def _upsert_auth(self, conn: sqlite3.Connection, lot: sqlite3.Row,
                    state: dict, *, carrier_id: Any = ...,
                    driver_id: Any = ...) -> None:
        now = _now()
        next_version = state["version"] + 1
        new_carrier = state["carrier_id"] if carrier_id is ... else carrier_id
        new_driver = state["driver_id"] if driver_id is ... else driver_id
        deal_id = state["deal_id"]
        if state["row"] is None:
            conn.execute(
                "INSERT INTO pickup_auth(event_id, lot_id, deal_id, winner_id, "
                "carrier_id, driver_id, auth_version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (lot["event_id"], lot["id"], deal_id, state["winner_id"],
                 new_carrier, new_driver, next_version, now, now),
            )
        else:
            conn.execute(
                "UPDATE pickup_auth SET deal_id=?, carrier_id=?, driver_id=?, "
                "auth_version=?, updated_at=? WHERE lot_id=?",
                (deal_id, new_carrier, new_driver, next_version, now, lot["id"]),
            )

    def _state_payload(self, conn: sqlite3.Connection,
                       lot: sqlite3.Row,
                       deal: sqlite3.Row | None = None) -> dict:
        if deal is None:
            deal = self._effective_deal(conn, lot)
        state = self._auth_state(conn, lot, deal)
        picker_id, kind = self._picker_of(state)
        delivered = self._delivered_row(
            conn, lot["id"], state["deal_id"])
        is_delivered = delivered is not None
        secret = self.db.alias_secret
        credential = None
        if not is_delivered and state["winner_id"] is not None:
            credential = compute_credential(
                secret, lot["event_id"], lot["id"],
                state["version"], picker_id, state["deal_id"],
            )
        winner = self._user_brief(conn, lot["event_id"], state["winner_id"])
        seller = self._user_brief(conn, lot["event_id"], lot["seller_id"])
        carrier = self._user_brief(conn, lot["event_id"], state["carrier_id"])
        driver = self._user_brief(conn, lot["event_id"], state["driver_id"])
        picker = self._user_brief(conn, lot["event_id"], picker_id)
        authorization = {
            "kind": kind,
            "type": kind,
            "auth_version": state["version"],
            "version": state["version"],
            "picker": picker,
            "authorized_picker": picker,
            "carrier": carrier,
            "driver": driver,
            "credential": credential,
            "pickup_credential": credential,
            "code": credential,
            "pickup_code": credential,
            "verification_code": credential,
        }
        payload = {
            "event_id": lot["event_id"],
            "lot_id": lot["id"],
            "id": lot["id"],
            "title": lot["title"],
            "lot_status": lot["status"],
            "deal_id": state["deal_id"],
            "status": "delivered" if is_delivered else "pending_pickup",
            "delivery_status": "delivered" if is_delivered else "pending",
            "pickup_status": "delivered" if is_delivered else "pending",
            "delivered": is_delivered,
            "winner": winner,
            "winner_id": state["winner_id"],
            "winner_name": winner["name"] if winner else None,
            "buyer_id": state["winner_id"],
            "seller": seller,
            "seller_id": lot["seller_id"],
            "seller_name": seller["name"] if seller else None,
            "carrier": carrier,
            "carrier_id": state["carrier_id"],
            "carrier_name": carrier["name"] if carrier else None,
            "driver": driver,
            "driver_id": state["driver_id"],
            "driver_name": driver["name"] if driver else None,
            "authorized_picker": picker,
            "current_picker": picker,
            "picker": picker,
            "holder": picker,
            "current_holder": picker,
            "authorized_account": picker,
            "pickup_account": picker,
            "authorized_picker_id": picker_id,
            "current_picker_id": picker_id,
            "picker_id": picker_id,
            "holder_id": picker_id,
            "picker_kind": kind,
            "authorization": authorization,
            "auth_version": state["version"],
            "version": state["version"],
            "credential": credential,
            "pickup_credential": credential,
            "verification_credential": credential,
            "code": credential,
            "pickup_code": credential,
            "verification_code": credential,
            "token": credential,
            "receipt_id": delivered["delivery_id"] if delivered else None,
            "delivery_id": delivered["delivery_id"] if delivered else None,
            "receipt_no": delivered["receipt_no"] if delivered else None,
            "delivered_at": delivered["delivered_at"] if delivered else None,
            "delivery_time": delivered["delivered_at"] if delivered else None,
        }
        payload["current_authorization"] = authorization
        payload["grant"] = authorization
        return payload


    # ------------------------------------------------- buyer: carrier grants
    def delegate_carrier(self, event_id: int, actor_id: int,
                         lot_ids: Sequence[Any], carrier_ref: Any) -> dict:
        ids = unique_lot_ids(lot_ids)
        with self.db.tx() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lots = self._load_batch_lots(conn, event_id, ids)
            target = self._validate_trustee_target(
                conn, event_id, carrier_ref, lots, actor_id)

            states: list[dict] = []
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                if deal is None:
                    raise ApiError(409, "lot_not_sold",
                                   "Only sold lots can be entrusted for pickup")
                if deal["buyer_id"] != actor_id:
                    raise ApiError(403, "forbidden",
                                   "Only the winning buyer can entrust this lot")
                if self._delivered_row(conn, lot["id"], deal["id"]) is not None:
                    raise ApiError(409, "already_delivered",
                                   "Delivered lots can no longer be changed")
                state = self._auth_state(conn, lot, deal)
                # Re-delegation always bumps the version and clears any driver,
                # even when the carrier is unchanged.
                self._upsert_auth(
                    conn, lot, state,
                    carrier_id=target["id"], driver_id=None,
                )
                states.append(self._auth_state(conn, lot, deal))
            _audit(conn, event_id, actor_id, "pickup_delegate",
                   f"lots={ids} carrier={target['id']}")
            items = [self._state_payload(conn, lot) for lot in lots]
            return {
                "ok": True,
                "action": "delegate_carrier",
                "event_id": event_id,
                "lot_ids": ids,
                "count": len(ids),
                "carrier": self._brief(target),
                "carrier_id": target["id"],
                "items": items,
                "authorizations": items,
                "pickups": items,
            }

    def revoke_carrier(self, event_id: int, actor_id: int,
                       lot_ids: Sequence[Any]) -> dict:
        ids = unique_lot_ids(lot_ids)
        with self.db.tx() as conn:
            _event(conn, event_id)
            _user(conn, event_id, actor_id)
            lots = self._load_batch_lots(conn, event_id, ids)
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                if deal is None:
                    raise ApiError(409, "lot_not_sold", "Lot is not sold")
                if deal["buyer_id"] != actor_id:
                    raise ApiError(403, "forbidden",
                                   "Only the winning buyer can recover this lot")
                if self._delivered_row(conn, lot["id"], deal["id"]) is not None:
                    raise ApiError(409, "already_delivered",
                                   "Delivered lots can no longer be recovered")
                state = self._auth_state(conn, lot, deal)
                if state["carrier_id"] is None:
                    raise ApiError(
                        409, "no_active_delegation",
                        "Every selected lot must have a current carrier "
                        "delegation to recover",
                    )
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                state = self._auth_state(conn, lot, deal)
                self._upsert_auth(conn, lot, state,
                                  carrier_id=None, driver_id=None)
            _audit(conn, event_id, actor_id, "pickup_revoke_carrier",
                   f"lots={ids}")
            items = [self._state_payload(conn, lot) for lot in lots]
            return {
                "ok": True,
                "action": "revoke_carrier",
                "event_id": event_id,
                "lot_ids": ids,
                "count": len(ids),
                "carrier": None,
                "carrier_id": None,
                "items": items,
                "authorizations": items,
                "pickups": items,
            }

    # ----------------------------------------------- carrier: driver grants
    def assign_driver(self, event_id: int, actor_id: int,
                      lot_ids: Sequence[Any], driver_ref: Any) -> dict:
        ids = unique_lot_ids(lot_ids)
        with self.db.tx() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lots = self._load_batch_lots(conn, event_id, ids)
            target = resolve_account(conn, event_id, driver_ref)
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                if deal is None:
                    raise ApiError(409, "lot_not_sold", "Lot is not sold")
                if self._delivered_row(conn, lot["id"], deal["id"]) is not None:
                    raise ApiError(409, "already_delivered",
                                   "Delivered lots can no longer be changed")
                state = self._auth_state(conn, lot, deal)
                if state["carrier_id"] != actor_id:
                    raise ApiError(
                        403, "forbidden",
                        "Only the current carrier can assign its driver",
                    )
                forbidden = {lot["seller_id"], deal["buyer_id"], actor_id}
                if target["id"] in forbidden:
                    raise ApiError(
                        422, "invalid_driver",
                        "The driver cannot be this lot's seller, winner, or "
                        "the acting carrier",
                    )
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                state = self._auth_state(conn, lot, deal)
                # Assign/replace always bumps the version, even for same driver.
                self._upsert_auth(conn, lot, state, driver_id=target["id"])
            _audit(conn, event_id, actor_id, "pickup_assign_driver",
                   f"lots={ids} driver={target['id']}")
            items = [self._state_payload(conn, lot) for lot in lots]
            return {
                "ok": True,
                "action": "assign_driver",
                "event_id": event_id,
                "lot_ids": ids,
                "count": len(ids),
                "driver": self._brief(target),
                "driver_id": target["id"],
                "items": items,
                "authorizations": items,
                "pickups": items,
            }

    def revoke_driver(self, event_id: int, actor_id: int,
                      lot_ids: Sequence[Any]) -> dict:
        ids = unique_lot_ids(lot_ids)
        with self.db.tx() as conn:
            _event(conn, event_id)
            _user(conn, event_id, actor_id)
            lots = self._load_batch_lots(conn, event_id, ids)
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                if deal is None:
                    raise ApiError(409, "lot_not_sold", "Lot is not sold")
                if self._delivered_row(conn, lot["id"], deal["id"]) is not None:
                    raise ApiError(409, "already_delivered",
                                   "Delivered lots can no longer be changed")
                state = self._auth_state(conn, lot, deal)
                if state["carrier_id"] != actor_id:
                    raise ApiError(
                        403, "forbidden",
                        "Only the current carrier can withdraw its driver",
                    )
                if state["driver_id"] is None:
                    raise ApiError(
                        409, "no_active_driver",
                        "Every selected lot must currently have a driver "
                        "assignment to withdraw",
                    )
            for lot in lots:
                deal = self._effective_deal(conn, lot)
                state = self._auth_state(conn, lot, deal)
                self._upsert_auth(conn, lot, state, driver_id=None)
            _audit(conn, event_id, actor_id, "pickup_revoke_driver",
                   f"lots={ids}")
            items = [self._state_payload(conn, lot) for lot in lots]
            return {
                "ok": True,
                "action": "revoke_driver",
                "event_id": event_id,
                "lot_ids": ids,
                "count": len(ids),
                "driver": None,
                "driver_id": None,
                "items": items,
                "authorizations": items,
                "pickups": items,
            }

    # ------------------------------------------------------------- queries
    def _require_pickup_viewer(self, conn, lot, actor, deal=None):
        if deal is None:
            deal = self._effective_deal(conn, lot)
        state = self._auth_state(conn, lot, deal)
        winner_id = state["winner_id"]
        allowed = {
            lot["seller_id"], winner_id,
            state["carrier_id"], state["driver_id"],
        }
        if actor["role"] == "admin":
            return state
        if actor["id"] not in allowed:
            raise ApiError(403, "forbidden",
                           "Only the winner, the lot's seller, or the current "
                           "trustee can view pickup status")
        return state

    def pickup_status(self, event_id: int, actor_id: int, lot_id: int,
                      deal_ref: Any = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lot = self._load_batch_lots(conn, event_id, [lot_id])[0]
            deal = self._scoped_deal(conn, event_id, lot, deal_ref)
            if deal is None:
                raise ApiError(409, "lot_not_sold", "Lot is not sold")
            self._require_pickup_viewer(conn, lot, actor, deal)
            payload = self._state_payload(conn, lot, deal)
            picker_id = payload["authorized_picker_id"]
            payload["viewer_id"] = actor_id
            payload["viewer_is_picker"] = actor_id == picker_id
            payload["viewer_can_pickup"] = (
                actor_id == picker_id and not payload["delivered"])
            payload["viewer_can_view"] = True
            return payload

    def pickup_credential(self, event_id: int, actor_id: int,
                         lot_id: int, deal_ref: Any = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lot = self._load_batch_lots(conn, event_id, [lot_id])[0]
            deal = self._scoped_deal(conn, event_id, lot, deal_ref)
            if deal is None:
                raise ApiError(409, "lot_not_sold", "Lot is not sold")
            self._require_pickup_viewer(conn, lot, actor, deal)
            if self._delivered_row(conn, lot_id, deal["id"]) is not None:
                raise ApiError(409, "already_delivered",
                               "Delivered lots no longer issue credentials")
            payload = self._state_payload(conn, lot, deal)
            return {
                "event_id": event_id,
                "lot_id": lot_id,
                "deal_id": deal["id"],
                "deal_no": deal["deal_no"],
                "id": lot_id,
                "credential": payload["credential"],
                "pickup_credential": payload["credential"],
                "verification_credential": payload["credential"],
                "code": payload["credential"],
                "pickup_code": payload["credential"],
                "verification_code": payload["credential"],
                "token": payload["credential"],
                "authorized_picker": payload["authorized_picker"],
                "picker": payload["picker"],
                "auth_version": payload["auth_version"],
                "expires": None,
            }

    def list_pickups(self, event_id: int, actor_id: int, *,
                     as_role: str | None = None,
                     status_filter: str | None = None) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            # Only lots whose sale is currently effective take part.
            rows = conn.execute(
                "SELECT l.*, d.id AS deal_id, d.buyer_id AS deal_buyer_id, "
                "d.price AS deal_price, d.deal_no AS deal_no "
                "FROM lots l JOIN deals d ON d.lot_id=l.id AND d.status='active' "
                "WHERE l.event_id=? ORDER BY l.id",
                (event_id,),
            ).fetchall()
            items = []
            for lot in rows:
                deal = conn.execute("SELECT * FROM deals WHERE id=?",
                                    (lot["deal_id"],)).fetchone()
                buyer_id = lot["deal_buyer_id"]
                state = self._auth_state(conn, lot, deal)
                if actor["role"] != "admin" or as_role:
                    role = as_role
                    if role in ("winner", "buyer"):
                        if buyer_id != actor_id:
                            continue
                    elif role in ("carrier", "trustee"):
                        if state["carrier_id"] != actor_id:
                            continue
                    elif role == "driver":
                        if state["driver_id"] != actor_id:
                            continue
                    elif role == "seller":
                        if lot["seller_id"] != actor_id:
                            continue
                    elif actor["role"] == "seller":
                        if lot["seller_id"] != actor_id:
                            continue
                    elif actor["role"] == "buyer":
                        if buyer_id != actor_id:
                            continue
                    else:
                        if actor_id not in (buyer_id, state["carrier_id"],
                                            state["driver_id"],
                                            lot["seller_id"]):
                            continue
                payload = self._state_payload(conn, lot, deal)
                if status_filter in ("pending", "undelivered") and payload["delivered"]:
                    continue
                if status_filter == "delivered" and not payload["delivered"]:
                    continue
                items.append(payload)
            return {
                "event_id": event_id,
                "pickups": items,
                "items": items,
                "lots": items,
                "count": len(items),
            }

    # ------------------------------------------------------- seller delivery
    def confirm_delivery(self, event_id: int, actor_id: int, *,
                         request_key: str | None, picker_ref: Any,
                         items: Sequence[dict]) -> tuple[dict, bool]:
        if not items:
            raise ApiError(422, "invalid_request",
                           "A delivery needs at least one {lot_id, credential}")
        seen: set[int] = set()
        normalized: list[dict] = []
        for item in items:
            lot_id = as_int(item.get("lot_id")) if isinstance(item, dict) else None
            if lot_id is None:
                raise ApiError(422, "invalid_request",
                               "Every delivery item needs an integer lot_id")
            if lot_id in seen:
                # Explicit requirement: the delivered group has distinct lots.
                raise ApiError(422, "duplicate_lots",
                               "Lot ids in one delivery must be distinct")
            seen.add(lot_id)
            if not str(item.get("credential") or "").strip():
                raise ApiError(
                    422, "invalid_request",
                    "Every delivery item must carry its verification credential",
                )
            normalized.append({
                "lot_id": lot_id,
                "credential": item.get("credential"),
                "deal_ref": item.get("deal_ref"),
            })

        with self.db.tx() as conn:
            _event(conn, event_id)
            seller = _user(conn, event_id, actor_id)
            picker = resolve_account(conn, event_id, picker_ref)
            picker_id = picker["id"]
            req_key = (request_key or "").strip() or (
                "auto-" + secrets.token_urlsafe(18))
            req_hash = canonical_delivery_hash(picker_id, normalized)

            prior = conn.execute(
                "SELECT * FROM deliveries WHERE event_id=? AND seller_id=? "
                "AND request_key=?",
                (event_id, actor_id, req_key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ApiError(
                        409, "idempotency_conflict",
                        "This request id was already used with different "
                        "delivery content",
                    )
                receipt = self._receipt_payload(
                    conn, prior["id"], viewer_id=actor_id, full=True)
                receipt["replayed"] = True
                receipt["idempotent_replay"] = True
                return receipt, False

            lots = self._load_batch_lots(
                conn, event_id, [i["lot_id"] for i in normalized])
            by_id = {l["id"]: l for l in lots}
            secret = self.db.alias_secret

            # Phase 1 -- validate the ENTIRE group before writing anything.
            for item in normalized:
                lot = by_id[item["lot_id"]]
                if lot["seller_id"] != actor_id:
                    raise ApiError(
                        403, "forbidden",
                        "Every lot in a delivery must belong to the confirming "
                        "seller",
                    )
                deal = self._scoped_deal(
                    conn, event_id, lot, item["deal_ref"])
                if deal is None:
                    raise ApiError(409, "lot_not_sold",
                                   "Only sold lots can be delivered")
                if deal["status"] != "active":
                    raise ApiError(409, "deal_revoked",
                                   "This lot's sale has been revoked")
                item["deal_id"] = deal["id"]
                if self._delivered_row(conn, lot["id"], deal["id"]) is not None:
                    raise ApiError(409, "already_delivered",
                                   "A lot in this group is already delivered")
                state = self._auth_state(conn, lot, deal)
                effective_id, _kind = self._picker_of(state)
                expected = compute_credential(
                    secret, event_id, lot["id"],
                    state["version"], effective_id, deal["id"],
                )
                if not same_credential(item.get("credential"), expected):
                    raise ApiError(
                        409, "invalid_credential",
                        "A verification credential is missing, expired, or no "
                        "longer valid",
                    )
                if effective_id != picker_id:
                    raise ApiError(
                        409, "picker_not_authorized",
                        "A lot in this group is not currently authorised for "
                        "that pickup account",
                    )

            # Phase 2 -- one receipt + one snapshot row per lot.
            now = _now()
            try:
                cur = conn.execute(
                    "INSERT INTO deliveries(event_id, seller_id, receipt_no, "
                    "picker_id, request_key, request_hash, item_count, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, actor_id, "", picker_id, req_key, req_hash,
                     len(normalized), now),
                )
                delivery_id = cur.lastrowid
                receipt_no = f"RCP-{delivery_id:08d}"
                conn.execute(
                    "UPDATE deliveries SET receipt_no=? WHERE id=?",
                    (receipt_no, delivery_id),
                )
                for item in normalized:
                    lot = by_id[item["lot_id"]]
                    deal = conn.execute(
                        "SELECT * FROM deals WHERE id=?", (item["deal_id"],
                                                           )).fetchone()
                    state = self._auth_state(conn, lot, deal)
                    effective_id, _kind = self._picker_of(state)
                    conn.execute(
                        "INSERT INTO delivery_items(delivery_id, event_id, "
                        "lot_id, deal_id, seller_id, winner_id, picker_id, "
                        "carrier_id, driver_id, auth_version, delivered_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (delivery_id, event_id, lot["id"], deal["id"],
                         lot["seller_id"], deal["buyer_id"], picker_id,
                         state["carrier_id"], state["driver_id"],
                         state["version"], now),
                    )
            except sqlite3.IntegrityError as exc:
                # Racing process delivered one of these lots first.
                raise ApiError(409, "already_delivered",
                               "A lot in this group was just delivered") from exc

            _audit(conn, event_id, actor_id, "delivery_confirmed",
                   f"receipt={delivery_id} lots={[i['lot_id'] for i in normalized]} "
                   f"picker={picker_id}")
            receipt = self._receipt_payload(
                conn, delivery_id, viewer_id=actor_id, full=True)
            receipt["replayed"] = False
            receipt["idempotent_replay"] = False
            return receipt, True

    # ------------------------------------------------------------ receipts
    def _receipt_item_payload(self, conn: sqlite3.Connection,
                              row: sqlite3.Row) -> dict:
        winner = self._user_brief(conn, row["event_id"], row["winner_id"])
        picker = self._user_brief(conn, row["event_id"], row["picker_id"])
        carrier = self._user_brief(conn, row["event_id"], row["carrier_id"])
        driver = self._user_brief(conn, row["event_id"], row["driver_id"])
        lot = conn.execute(
            "SELECT title FROM lots WHERE id=?", (row["lot_id"],)
        ).fetchone()
        snapshot = {
            "lot_id": row["lot_id"],
            "deal_id": row["deal_id"],
            "winner_id": row["winner_id"],
            "carrier_id": row["carrier_id"],
            "driver_id": row["driver_id"],
            "picker_id": row["picker_id"],
            "auth_version": row["auth_version"],
        }
        return {
            "lot_id": row["lot_id"],
            "id": row["lot_id"],
            "deal_id": row["deal_id"],
            "title": lot["title"] if lot else None,
            "winner": winner,
            "winner_id": row["winner_id"],
            "winner_name": winner["name"] if winner else None,
            "picker": picker,
            "authorized_picker_at_confirmation": picker,
            "actual_picker": picker,
            "carrier": carrier,
            "carrier_id": row["carrier_id"],
            "driver": driver,
            "driver_id": row["driver_id"],
            "delegation": snapshot,
            "delegation_snapshot": snapshot,
            "auth_version": row["auth_version"],
            "delivered_at": row["delivered_at"],
        }

    def _receipt_payload(self, conn: sqlite3.Connection, delivery_id: int, *,
                         viewer_id: int, full: bool) -> dict:
        d = conn.execute(
            "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        if d is None:
            raise ApiError(404, "receipt_not_found", "Receipt not found")
        rows = conn.execute(
            "SELECT * FROM delivery_items WHERE delivery_id=? ORDER BY lot_id",
            (delivery_id,),
        ).fetchall()
        if full:
            visible_rows = rows
        else:
            visible_rows = [r for r in rows if r["winner_id"] == viewer_id]
        items = [self._receipt_item_payload(conn, r) for r in visible_rows]
        seller = self._user_brief(conn, d["event_id"], d["seller_id"])
        picker = self._user_brief(conn, d["event_id"], d["picker_id"])
        lot_ids = [r["lot_id"] for r in visible_rows]
        snapshot = [
            {
                "lot_id": r["lot_id"],
                "deal_id": r["deal_id"],
                "winner_id": r["winner_id"],
                "carrier_id": r["carrier_id"],
                "driver_id": r["driver_id"],
                "picker_id": r["picker_id"],
                "auth_version": r["auth_version"],
            }
            for r in visible_rows
        ]
        return {
            "id": d["id"],
            "receipt_id": d["id"],
            "delivery_id": d["id"],
            "receipt_no": d["receipt_no"],
            "event_id": d["event_id"],
            "seller": seller,
            "seller_id": d["seller_id"],
            "seller_name": seller["name"] if seller else None,
            "picker": picker,
            "picker_id": d["picker_id"],
            "picker_name": picker["name"] if picker else None,
            "pickup_person": picker,
            "actual_picker": picker,
            "receiver": picker,
            "request_id": d["request_key"],
            "request_key": d["request_key"],
            "idempotency_key": d["request_key"],
            "created_at": d["created_at"],
            "delivered_at": d["created_at"],
            "delivery_time": d["created_at"],
            "confirmed_at": d["created_at"],
            "items": items,
            "lots": items,
            "lot_ids": lot_ids,
            "count": len(items),
            "item_count": len(items),
            "delegation_snapshot": snapshot,
            "delegations": snapshot,
            "authorization_snapshot": snapshot,
            "pickup_snapshot": snapshot,
            "winner_scoped": not full,
            "scoped_to_winner": not full,
        }
        # Nested aliases without a self-referential cycle.
        inner = {k: v for k, v in payload.items()}
        payload["receipt"] = inner
        payload["delivery"] = dict(inner)
        return payload

    def get_receipt(self, event_id: int, actor_id: int,
                    receipt_id: Any) -> dict:
        rid = as_int(receipt_id)
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            d = None
            if rid is not None:
                d = conn.execute(
                    "SELECT * FROM deliveries WHERE event_id=? AND id=?",
                    (event_id, rid),
                ).fetchone()
            if d is None and isinstance(receipt_id, str):
                d = conn.execute(
                    "SELECT * FROM deliveries WHERE event_id=? AND receipt_no=?",
                    (event_id, receipt_id),
                ).fetchone()
            if d is None:
                raise ApiError(404, "receipt_not_found", "Receipt not found")
            is_seller = actor["id"] == d["seller_id"]
            is_picker = actor["id"] == d["picker_id"]
            is_winner = conn.execute(
                "SELECT 1 FROM delivery_items WHERE delivery_id=? "
                "AND winner_id=? LIMIT 1",
                (d["id"], actor_id),
            ).fetchone() is not None
            if actor["role"] != "admin" and not (
                is_seller or is_picker or is_winner
            ):
                raise ApiError(403, "forbidden",
                               "You are not a seller, winner, or pickup "
                               "person for this receipt")
            full = actor["role"] == "admin" or is_seller or is_picker
            payload = self._receipt_payload(
                conn, d["id"], viewer_id=actor_id, full=full)
            return payload

    def list_receipts(self, event_id: int, actor_id: int) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            if actor["role"] == "admin":
                rows = conn.execute(
                    "SELECT id FROM deliveries WHERE event_id=? ORDER BY id",
                    (event_id,),
                ).fetchall()
                receipts = [
                    self._receipt_payload(
                        conn, r["id"], viewer_id=actor_id, full=True)
                    for r in rows
                ]
            else:
                # Visible if I am the seller, the actual picker, or the winner
                # of at least one item.  Independent of the user's role label
                # because any account may act as carrier/picker.
                rows = conn.execute(
                    "SELECT DISTINCT d.id AS id FROM deliveries d "
                    "LEFT JOIN delivery_items di ON di.delivery_id=d.id "
                    "WHERE d.event_id=? AND (d.seller_id=? OR d.picker_id=? "
                    "OR di.winner_id=?) ORDER BY d.id",
                    (event_id, actor_id, actor_id, actor_id),
                ).fetchall()
                receipts = []
                for r in rows:
                    d = conn.execute(
                        "SELECT seller_id, picker_id FROM deliveries WHERE id=?",
                        (r["id"],),
                    ).fetchone()
                    full = (d["seller_id"] == actor_id
                            or d["picker_id"] == actor_id)
                    receipts.append(self._receipt_payload(
                        conn, r["id"], viewer_id=actor_id, full=full))
            return {
                "event_id": event_id,
                "receipts": receipts,
                "deliveries": receipts,
                "items": receipts,
                "count": len(receipts),
            }

    def get_lot_receipt(self, event_id: int, actor_id: int,
                        lot_id: int) -> dict:
        with self.db.read() as conn:
            _event(conn, event_id)
            actor = _user(conn, event_id, actor_id)
            lot = self._load_batch_lots(conn, event_id, [lot_id])[0]
            deal = self._scoped_deal(conn, event_id, lot, None)
            delivered = self._delivered_row(
                conn, lot_id, deal["id"] if deal else None)
            if delivered is None:
                raise ApiError(404, "receipt_not_found",
                               "Lot has not been delivered yet")
        return self.get_receipt(event_id, actor_id, delivered["delivery_id"])
