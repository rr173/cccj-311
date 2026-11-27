"""Tests for post-sale pickup / carrier / driver / delivery / receipts.

These encode the behavioural contract given in the task:

* only sold lots, still available after the event closes;
* buyer batch delegate / re-delegate / recover;
* carrier batch assign / replace / withdraw drivers;
* exactly one effective picker per lot (driver > carrier > winner);
* whole-batch atomicity for every mutation and delivery;
* credential invalidation on any authorisation change (per-lot versioning);
* seller one-shot multi-lot delivery with per-lot credentials + request id;
* idempotent replay / conflicting reuse of a request id;
* receipt visibility and winner scoping in multi-winner batches;
* restart persistence and public-API privacy.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.errors import ApiError
from app.main import create_app
from app.service import ADMIN_KEY, Service


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def H(user):
    return {"Authorization": f"Bearer {user['token']}"}


@pytest.fixture()
def world(tmp_path):
    db_path = str(tmp_path / "pickup.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        eid = c.post("/events", json={"name": "E"}).json()["id"]
        admin = c.post(f"/events/{eid}/admin",
                       json={"name": "admin", "admin_key": ADMIN_KEY}).json()
        s1 = c.post(f"/events/{eid}/users",
                    json={"name": "s1", "role": "seller"}).json()
        s2 = c.post(f"/events/{eid}/users",
                    json={"name": "s2", "role": "seller"}).json()
        b1 = c.post(f"/events/{eid}/users",
                    json={"name": "b1", "role": "buyer"}).json()
        b2 = c.post(f"/events/{eid}/users",
                    json={"name": "b2", "role": "buyer"}).json()
        b3 = c.post(f"/events/{eid}/users",
                    json={"name": "b3", "role": "buyer"}).json()
        car = c.post(f"/events/{eid}/users",
                     json={"name": "car", "role": "carrier"}).json()
        car2 = c.post(f"/events/{eid}/users",
                      json={"name": "car2", "role": "buyer"}).json()
        drv = c.post(f"/events/{eid}/users",
                     json={"name": "drv", "role": "driver"}).json()
        drv2 = c.post(f"/events/{eid}/users",
                      json={"name": "drv2", "role": "buyer"}).json()
        for b in (b1, b2, b3):
            r = c.put(f"/events/{eid}/budget", json={"budget": 10_000},
                      headers=H(b))
            assert r.status_code == 200, r.text

        def win(buyer, seller=s1, reserve=10, bid=500):
            lid = c.post(f"/events/{eid}/lots",
                         json={"title": f"L{reserve}-{bid}", "reserve": reserve},
                         headers=H(seller)).json()["id"]
            r = c.post(f"/events/{eid}/lots/{lid}/bids",
                       json={"max_bid": bid}, headers=H(buyer))
            assert r.status_code == 201, r.text
            r = c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller))
            assert r.status_code == 200, r.text
            assert r.json()["winner_alias"] is not None
            return lid

        yield {
            "c": c, "eid": eid, "admin": admin, "s1": s1, "s2": s2,
            "b1": b1, "b2": b2, "b3": b3, "car": car, "car2": car2,
            "drv": drv, "drv2": drv2, "win": win, "db_path": db_path,
        }


def status(c, eid, lot, user, path="pickup"):
    return c.get(f"/events/{eid}/lots/{lot}/{path}", headers=H(user))


def cred(c, eid, lot, user):
    r = c.get(f"/events/{eid}/lots/{lot}/credential", headers=H(user))
    assert r.status_code == 200, r.text
    return r.json()["credential"]


def delegate(c, eid, user, lots, carrier, path="/pickups/delegate"):
    ref = carrier["user_id"] if isinstance(carrier, dict) else carrier
    return c.post(f"/events/{eid}{path}",
                  json={"lot_ids": lots, "carrier_id": ref}, headers=H(user))


def deliver(c, eid, seller, request_id, picker, pairs, path="/deliveries"):
    """pairs: list of (lot_id, credential)."""
    return c.post(
        f"/events/{eid}{path}",
        json={
            "request_id": request_id,
            "picker_id": picker["user_id"] if isinstance(picker, dict) else picker,
            "items": [{"lot_id": l, "credential": k} for l, k in pairs],
        },
        headers=H(seller),
    )


# ---------------------------------------------------------------------------
# Status / visibility / credentials
# ---------------------------------------------------------------------------
def test_winner_seller_can_view_pending_status_and_credential(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    r = status(c, eid, lot, world["b1"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivered"] is False
    assert body["status"] == "pending_pickup"
    assert body["authorized_picker_id"] == world["b1"]["user_id"]
    assert body["picker_kind"] == "winner"
    assert body["credential"].startswith("pck_")
    # seller may also obtain it
    assert status(c, eid, lot, world["s1"]).status_code == 200
    assert cred(c, eid, lot, world["s1"]) == body["credential"]
    # unrelated buyer may not
    assert status(c, eid, lot, world["b2"]).status_code == 403


def test_credential_is_stable_but_changes_after_authorisation_change(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    k1 = cred(c, eid, lot, world["b1"])
    assert cred(c, eid, lot, world["b1"]) == k1  # repeated obtain is stable

    assert delegate(c, eid, world["b1"], [lot], world["car"]).status_code == 200
    k2 = cred(c, eid, lot, world["car"])
    assert k2 != k1
    # delegating to the *same* carrier again still rotates the credential
    assert delegate(c, eid, world["b1"], [lot], world["car"]).status_code == 200
    k3 = cred(c, eid, lot, world["car"])
    assert k3 not in (k1, k2)

    r = c.post(f"/events/{eid}/pickups/revoke", json={"lot_ids": [lot]},
               headers=H(world["b1"]))
    assert r.status_code == 200
    k4 = cred(c, eid, lot, world["b1"])
    assert k4 not in (k1, k2, k3)
    # delegate to car once more: same picker as k2/k3 but a new credential
    delegate(c, eid, world["b1"], [lot], world["car"])
    k5 = cred(c, eid, lot, world["car"])
    assert k5 not in (k2, k3, k4)


def test_other_lots_credentials_survive_a_change_on_one_lot(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [l1, l2], world["car"])
    k1 = cred(c, eid, l1, world["car"])
    k2 = cred(c, eid, l2, world["car"])
    # re-delegate only l1
    assert delegate(c, eid, world["b1"], [l1], world["car2"]).status_code == 200
    assert cred(c, eid, l2, world["car"]) == k2
    assert cred(c, eid, l1, world["car2"]) != k1


# ---------------------------------------------------------------------------
# Buyer delegation rules
# ---------------------------------------------------------------------------
def test_only_winning_buyer_can_delegate(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    r = delegate(c, eid, world["b2"], [lot], world["car"])
    assert r.status_code == 403
    # seller cannot use the buyer endpoint either
    r = delegate(c, eid, world["s1"], [lot], world["car"])
    assert r.status_code == 403


@pytest.mark.parametrize("target", ["seller", "b1"])
def test_delegate_target_cannot_be_seller_winner_or_self(world, target):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    account = world["s1"] if target == "seller" else world["b1"]
    r = delegate(c, eid, world["b1"], [lot], account)
    assert r.status_code == 422
    # nothing changed: winner remains picker
    body = status(c, eid, lot, world["b1"]).json()
    assert body["carrier_id"] is None
    assert body["authorized_picker_id"] == world["b1"]["user_id"]


def test_delegate_target_must_be_same_event_account(world):
    c, eid = world["c"], world["eid"]
    other = c.post("/events", json={"name": "other"}).json()["id"]
    outsider = c.post(f"/events/{other}/users",
                      json={"name": "x", "role": "buyer"}).json()
    lot = world["win"](world["b1"])
    r = delegate(c, eid, world["b1"], [lot], outsider)
    assert r.status_code == 404
    # unknown name also 404
    r = c.post(f"/events/{eid}/pickups/delegate",
               json={"lot_ids": [lot], "carrier": "ghost"}, headers=H(world["b1"]))
    assert r.status_code == 404


def test_redelegate_strips_old_carrier_and_driver_access(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [l1], world["car"])
    r = c.post(f"/events/{eid}/pickups/assign-driver",
               json={"lot_ids": [l1], "driver_id": world["drv"]["user_id"]},
               headers=H(world["car"]))
    assert r.status_code == 200
    assert status(c, eid, l1, world["drv"]).status_code == 200

    # buyer re-delegates to car2: old carrier and its driver lose access
    delegate(c, eid, world["b1"], [l1], world["car2"])
    assert status(c, eid, l1, world["car"]).status_code == 403
    assert status(c, eid, l1, world["drv"]).status_code == 403
    body = status(c, eid, l1, world["car2"]).json()
    assert body["carrier_id"] == world["car2"]["user_id"]
    assert body["driver_id"] is None
    assert body["authorized_picker_id"] == world["car2"]["user_id"]


def test_recover_requires_current_delegation_and_is_batch_atomic(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [l1], world["car"])  # only l1 delegated

    # recovering [l1, l2] fails wholesale because l2 has no delegation
    r = c.post(f"/events/{eid}/pickups/revoke",
               json={"lot_ids": [l1, l2]}, headers=H(world["b1"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_active_delegation"
    # l1 delegation untouched
    assert status(c, eid, l1, world["car"]).status_code == 200

    # recovering an never-delegated lot alone also fails
    r = c.post(f"/events/{eid}/pickups/revoke",
               json={"lot_ids": [l2]}, headers=H(world["b1"]))
    assert r.status_code == 409

    # valid recovery of l1 only
    r = c.post(f"/events/{eid}/pickups/recover",
               json={"lot_ids": [l1]}, headers=H(world["b1"]))
    assert r.status_code == 200
    body = status(c, eid, l1, world["b1"]).json()
    assert body["carrier_id"] is None
    assert body["authorized_picker_id"] == world["b1"]["user_id"]
    assert status(c, eid, l1, world["car"]).status_code == 403


# ---------------------------------------------------------------------------
# Carrier -> driver rules
# ---------------------------------------------------------------------------
def test_driver_precedence_and_withdraw(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    assert status(c, eid, lot, world["car"]).json()["picker_kind"] == "carrier"

    r = c.post(f"/events/{eid}/pickups/assign-driver",
               json={"lot_ids": [lot], "driver": world["drv"]["name"]},
               headers=H(world["car"]))
    assert r.status_code == 200, r.text
    body = status(c, eid, lot, world["drv"]).json()
    assert body["picker_kind"] == "driver"
    assert body["authorized_picker_id"] == world["drv"]["user_id"]
    # carrier still has status access but is not the picker
    assert status(c, eid, lot, world["car"]).json()["viewer_can_pickup"] is False

    # replace driver
    r = c.post(f"/events/{eid}/pickups/replace-driver",
               json={"lot_ids": [lot], "driver_id": world["drv2"]["user_id"]},
               headers=H(world["car"]))
    assert r.status_code == 200
    assert status(c, eid, lot, world["drv"]).status_code == 403
    assert status(c, eid, lot, world["drv2"]).json()[
        "authorized_picker_id"] == world["drv2"]["user_id"]

    # withdraw without any driver on a second lot -> batch atomic failure
    lot2 = world["win"](world["b2"])
    delegate(c, eid, world["b2"], [lot2], world["car"])
    r = c.post(f"/events/{eid}/pickups/revoke-driver",
               json={"lot_ids": [lot, lot2]}, headers=H(world["car"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_active_driver"
    assert status(c, eid, lot, world["drv2"]).status_code == 200

    # valid withdraw -> picker is carrier again
    r = c.post(f"/events/{eid}/pickups/withdraw-driver",
               json={"lot_ids": [lot]}, headers=H(world["car"]))
    assert r.status_code == 200
    body = status(c, eid, lot, world["car"]).json()
    assert body["driver_id"] is None
    assert body["authorized_picker_id"] == world["car"]["user_id"]
    assert status(c, eid, lot, world["drv2"]).status_code == 403


def test_only_current_carrier_can_assign_driver(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    # car2 is not the carrier
    r = c.post(f"/events/{eid}/driver-assignments",
               json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
               headers=H(world["car2"]))
    assert r.status_code == 403
    # the buyer cannot assign drivers either
    r = c.post(f"/events/{eid}/pickups/assign-driver",
               json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
               headers=H(world["b1"]))
    assert r.status_code == 403


def test_driver_target_cannot_be_seller_winner_or_carrier_self(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    for forbidden in (world["s1"], world["b1"], world["car"]):
        r = c.post(f"/events/{eid}/pickups/assign-driver",
                   json={"lot_ids": [lot],
                         "driver_id": forbidden["user_id"]},
                   headers=H(world["car"]))
        assert r.status_code == 422, forbidden["name"]


# ---------------------------------------------------------------------------
# Delivery happy paths + receipts
# ---------------------------------------------------------------------------
def test_seller_delivers_batch_to_driver_and_gets_one_receipt(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [l1, l2], world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [l1, l2], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))
    k1, k2 = cred(c, eid, l1, world["drv"]), cred(c, eid, l2, world["drv"])

    r = deliver(c, eid, world["s1"], "req-1", world["drv"],
                [(l1, k1), (l2, k2)])
    assert r.status_code == 201, r.text
    rcpt = r.json()
    assert rcpt["id"] > 0
    assert sorted(rcpt["lot_ids"]) == sorted([l1, l2])
    assert rcpt["seller_id"] == world["s1"]["user_id"]
    assert rcpt["picker_id"] == world["drv"]["user_id"]
    assert rcpt["delivered_at"]
    # delegation relationship snapshotted per lot
    for item in rcpt["items"]:
        assert item["carrier_id"] == world["car"]["user_id"]
        assert item["driver_id"] == world["drv"]["user_id"]
        assert item["winner_id"] == world["b1"]["user_id"]
        assert item["auth_version"] >= 2
    # status is now delivered and no credential is issued
    body = status(c, eid, l1, world["b1"]).json()
    assert body["delivered"] is True and body["credential"] is None
    r = c.get(f"/events/{eid}/lots/{l1}/credential", headers=H(world["b1"]))
    assert r.status_code == 409


def test_deliver_to_carrier_directly_without_driver(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b2"])
    delegate(c, eid, world["b2"], [lot], world["car"])
    k = cred(c, eid, lot, world["car"])
    r = deliver(c, eid, world["s1"], "req-c", world["car"], [(lot, k)],
                path="/deliver")
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["picker_id"] == world["car"]["user_id"]
    # the carrier, as actual picker, sees the full receipt in detail and list
    got = c.get(f"/events/{eid}/receipts/{rid}", headers=H(world["car"]))
    assert got.status_code == 200 and len(got.json()["items"]) == 1
    listed = c.get(f"/events/{eid}/receipts", headers=H(world["car"])).json()
    assert any(x["id"] == rid for x in listed["receipts"])


def test_multi_winner_receipt_is_scoped_per_winner(world):
    c, eid = world["c"], world["eid"]
    a = world["win"](world["b1"])
    b = world["win"](world["b2"])
    # same carrier + driver for both winners
    delegate(c, eid, world["b1"], [a], world["car"])
    delegate(c, eid, world["b2"], [b], world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [a, b], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))
    ka, kb = cred(c, eid, a, world["drv"]), cred(c, eid, b, world["drv"])
    rcpt = deliver(c, eid, world["s1"], "multi-1", world["drv"],
                   [(a, ka), (b, kb)]).json()
    rid = rcpt["id"]

    # seller and actual picker see both lines
    assert len(c.get(f"/events/{eid}/receipts/{rid}",
                     headers=H(world["s1"])).json()["items"]) == 2
    drv_view = c.get(f"/events/{eid}/receipts/{rid}", headers=H(world["drv"]))
    assert len(drv_view.json()["items"]) == 2

    # each winner sees only their own lot detail / ids / snapshots
    v1 = c.get(f"/events/{eid}/receipts/{rid}", headers=H(world["b1"])).json()
    assert v1["lot_ids"] == [a]
    assert [i["lot_id"] for i in v1["items"]] == [a]
    assert all(s["lot_id"] == a for s in v1["delegation_snapshot"])
    v2 = c.get(f"/events/{eid}/receipts/{rid}", headers=H(world["b2"])).json()
    assert v2["lot_ids"] == [b]

    # unrelated buyer cannot open it
    assert c.get(f"/events/{eid}/receipts/{rid}",
                 headers=H(world["b3"])).status_code == 403

    # list endpoints agree
    listed = c.get(f"/events/{eid}/receipts", headers=H(world["b2"])).json()
    assert any(r["id"] == rid and r["lot_ids"] == [b]
               for r in listed["receipts"])


def test_lot_receipt_endpoint_and_visibility(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    k = cred(c, eid, lot, world["b1"])
    rcpt = deliver(c, eid, world["s1"], "r", world["b1"], [(lot, k)]).json()
    r = c.get(f"/events/{eid}/lots/{lot}/receipt", headers=H(world["b1"]))
    assert r.status_code == 200 and r.json()["id"] == rcpt["id"]
    assert c.get(f"/events/{eid}/lots/{lot}/receipt",
                 headers=H(world["b2"])).status_code == 403


# ---------------------------------------------------------------------------
# Delivery rejection: whole batch unchanged
# ---------------------------------------------------------------------------
def test_duplicate_lot_ids_rejected(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    k = cred(c, eid, lot, world["b1"])
    r = c.post(f"/events/{eid}/deliveries", json={
        "request_id": "dup", "picker_id": world["b1"]["user_id"],
        "items": [{"lot_id": lot, "credential": k},
                  {"lot_id": lot, "credential": k}],
    }, headers=H(world["s1"]))
    assert r.status_code == 422
    assert status(c, eid, lot, world["s1"]).json()["delivered"] is False


def test_stale_credential_fails_whole_group(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    k1_old = cred(c, eid, l1, world["b1"])
    k2 = cred(c, eid, l2, world["b1"])
    # rotate l1 only
    delegate(c, eid, world["b1"], [l1], world["car"])
    k1_new = cred(c, eid, l1, world["car"])

    # mix stale l1 credential with valid l2 winner credential but submit b1:
    # l1's stale cred fails -> whole group rejected, nothing delivered
    r = deliver(c, eid, world["s1"], "bad-1", world["b1"],
                [(l1, k1_old), (l2, k2)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_credential"
    assert status(c, eid, l2, world["b1"]).json()["delivered"] is False
    assert status(c, eid, l1, world["car"]).json()["delivered"] is False

    # correct credentials but wrong picker account -> rejected
    r = deliver(c, eid, world["s1"], "bad-2", world["b1"],
                [(l1, k1_new), (l2, k2)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] in {
        "invalid_credential", "picker_not_authorized"}


def test_already_delivered_lot_in_group_blocks_everything(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    k1 = cred(c, eid, l1, world["b1"])
    k2 = cred(c, eid, l2, world["b1"])
    assert deliver(c, eid, world["s1"], "one", world["b1"],
                   [(l1, k1)]).status_code == 201
    # second request includes delivered l1 and pending l2
    r = deliver(c, eid, world["s1"], "two", world["b1"],
                [(l1, k1), (l2, k2)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_delivered"
    assert status(c, eid, l2, world["b1"]).json()["delivered"] is False


def test_other_sellers_lot_blocks_whole_delivery(world):
    c, eid = world["c"], world["eid"]
    mine = world["win"](world["b1"], seller=world["s1"])
    theirs = world["win"](world["b2"], seller=world["s2"])
    km = cred(c, eid, mine, world["b1"])
    kt = cred(c, eid, theirs, world["b2"])
    r = deliver(c, eid, world["s1"], "cross", world["b1"],
                [(mine, km), (theirs, kt)])
    assert r.status_code == 403
    # my lot was not delivered either
    assert status(c, eid, mine, world["s1"]).json()["delivered"] is False
    # s2 can deliver their own lot fine
    assert deliver(c, eid, world["s2"], "own", world["b2"],
                   [(theirs, kt)]).status_code == 201


def test_delivered_lot_cannot_be_reassigned_or_revoked(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    k = cred(c, eid, lot, world["b1"])
    deliver(c, eid, world["s1"], "d", world["b1"], [(lot, k)])

    r = delegate(c, eid, world["b1"], [lot], world["car"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_delivered"
    r = c.post(f"/events/{eid}/pickups/revoke", json={"lot_ids": [lot]},
               headers=H(world["b1"]))
    assert r.status_code == 409
    # carrier cannot be assigned after delivery either
    delegate(c, eid, world["b1"], [lot], world["car"])  # blocked
    r = c.post(f"/events/{eid}/pickups/assign-driver",
               json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
               headers=H(world["car"]))
    assert r.status_code in (403, 409)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_same_request_id_same_content_returns_original_receipt(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    k1 = cred(c, eid, l1, world["b1"])
    k2 = cred(c, eid, l2, world["b1"])
    first = deliver(c, eid, world["s1"], "idem-1", world["b1"],
                    [(l1, k1), (l2, k2)])
    assert first.status_code == 201
    rid = first.json()["id"]

    # replay with reversed order and the flat payload shape
    r = c.post(f"/events/{eid}/deliveries", json={
        "request_id": "idem-1", "picker": world["b1"]["name"],
        "lot_ids": [l2, l1], "credentials": [k2, k1],
    }, headers=H(world["s1"]))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == rid
    assert r.json().get("replayed") is True
    # only one receipt exists
    receipts = c.get(f"/events/{eid}/receipts",
                     headers=H(world["s1"])).json()["receipts"]
    assert [x["id"] for x in receipts].count(rid) == 1


def test_same_request_id_different_content_conflicts(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    k1 = cred(c, eid, l1, world["b1"])
    k2 = cred(c, eid, l2, world["b1"])
    deliver(c, eid, world["s1"], "idem-2", world["b1"], [(l1, k1)])
    # same key, different lot set
    r = deliver(c, eid, world["s1"], "idem-2", world["b1"],
                [(l1, k1), (l2, k2)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    # same key, different picker
    r = deliver(c, eid, world["s1"], "idem-2", world["car"], [(l1, k1)])
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Event close / privacy
# ---------------------------------------------------------------------------
def test_pickup_works_after_whole_event_closes(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    r = c.post(f"/events/{eid}/close", headers=H(world["admin"]))
    assert r.status_code == 200
    # delegation + delivery still work
    assert delegate(c, eid, world["b1"], [lot], world["car"]).status_code == 200
    k = cred(c, eid, lot, world["car"])
    assert deliver(c, eid, world["s1"], "after-close", world["car"],
                   [(lot, k)]).status_code == 201


def test_public_auction_api_does_not_leak_credentials_or_proxy_identity(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    k = cred(c, eid, lot, world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))

    for url in (f"/events/{eid}/lots/{lot}", f"/events/{eid}/lots",
                f"/events/{eid}"):
        text = c.get(url).text
        assert "pck_" not in text and k not in text
        assert "credential" not in text
        assert "carrier" not in text and "driver" not in text
        assert world["car"]["name"] not in text
        assert world["drv"]["name"] not in text


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------
def test_restart_keeps_authorisation_receipt_and_credential(world):
    c, eid, path = world["c"], world["eid"], world["db_path"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    k_before = cred(c, eid, lot, world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))
    k_drv = cred(c, eid, lot, world["drv"])

    # simulate process restart: brand new app/service/database on same file
    app2 = create_app(path)
    with TestClient(app2) as c2:
        body = c2.get(f"/events/{eid}/lots/{lot}/pickup",
                      headers=H(world["drv"])).json()
        assert body["driver_id"] == world["drv"]["user_id"]
        assert body["authorized_picker_id"] == world["drv"]["user_id"]
        # persisted HMAC secret means the same credential survives restart
        assert c2.get(f"/events/{eid}/lots/{lot}/credential",
                      headers=H(world["drv"])).json()["credential"] == k_drv
        r = deliver_via(c2, eid, world["s1"], "restart-1", world["drv"],
                        [(lot, k_drv)])
        assert r.status_code == 201, r.text
        rid = r.json()["id"]

    app3 = create_app(path)
    with TestClient(app3) as c3:
        r = c3.get(f"/events/{eid}/receipts/{rid}", headers=H(world["s1"]))
        assert r.status_code == 200
        assert r.json()["items"][0]["lot_id"] == lot
        # old carrier credential predates nothing changed... delivery consumed lot
        assert c3.get(f"/events/{eid}/lots/{lot}/credential",
                      headers=H(world["drv"])).status_code == 409
    assert k_before != k_drv


def deliver_via(client, eid, seller, key, picker, pairs):
    return client.post(f"/events/{eid}/deliveries", json={
        "request_id": key, "picker_id": picker["user_id"],
        "items": [{"lot_id": l, "credential": k} for l, k in pairs],
    }, headers=H(seller))


# ---------------------------------------------------------------------------
# Concurrency: delivery vs authorisation change
# ---------------------------------------------------------------------------
def _service_world(tmp_path, name="race.db"):
    db = Database(str(tmp_path / name))
    db.init()
    svc = Service(db)
    eid = svc.create_event("E")["id"]
    seller = svc.register(eid, "s", "seller")
    b1 = svc.register(eid, "b1", "buyer")
    car = svc.register(eid, "car", "carrier")
    svc.set_budget(eid, b1["user_id"], 10_000)
    lot = svc.create_lot(eid, seller["user_id"], "L", "", 10)["id"]
    svc.place_bid(eid, b1["user_id"], lot, 500)
    svc.close_lot(eid, seller["user_id"], lot)
    svc.delegate_carrier(eid, b1["user_id"], [lot], car["user_id"])
    cred = svc.pickup_credential(eid, car["user_id"], lot)["credential"]
    return db, svc, eid, seller, b1, car, lot, cred


def _run_threads(fns):
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except ApiError as e:
            results[i] = ("err", e.code)
        except Exception as e:  # pragma: no cover - unexpected
            results[i] = ("boom", repr(e))

    ts = [threading.Thread(target=wrap, args=(i, f))
          for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return results


@pytest.mark.parametrize("rep", range(10))
def test_delivery_vs_revoke_race_has_one_effective_outcome(tmp_path, rep):
    db, svc, eid, seller, b1, car, lot, cred = _service_world(
        tmp_path, name=f"race{rep}.db")

    results = _run_threads([
        lambda: svc.confirm_delivery(
            eid, seller["user_id"], request_key=f"k{rep}",
            picker_ref=car["user_id"],
            items=[{"lot_id": lot, "credential": cred}]),
        lambda: svc.revoke_carrier(eid, b1["user_id"], [lot]),
    ])
    assert not any(r[0] == "boom" for r in results), results

    delivery, revoke = results
    if delivery[0] == "ok":
        # delivery committed first; the revoke must be refused as delivered
        assert revoke == ("err", "already_delivered"), results
        receipt, created = delivery[1]
        assert created is True
        assert svc.pickup_status(eid, b1["user_id"], lot)["delivered"] is True
    else:
        # revoke committed first; the credential/authorisation is stale
        assert delivery[0] == "err"
        assert delivery[1] in {"invalid_credential", "picker_not_authorized"}
        assert revoke[0] == "ok", results
        st = svc.pickup_status(eid, b1["user_id"], lot)
        assert st["delivered"] is False
        assert st["authorized_picker_id"] == b1["user_id"]
        # no receipt exists
        receipts = svc.list_receipts(eid, seller["user_id"])["receipts"]
        assert receipts == []


def test_concurrent_batch_delegations_never_mix_state(tmp_path):
    db, svc, eid, seller, b1, car, lot, cred = _service_world(
        tmp_path, "batch.db")
    car2 = svc.register(eid, "car2", "carrier")
    lot2 = svc.create_lot(eid, seller["user_id"], "L2", "", 10)["id"]
    svc.place_bid(eid, b1["user_id"], lot2, 500)
    svc.close_lot(eid, seller["user_id"], lot2)

    results = _run_threads([
        lambda: svc.delegate_carrier(eid, b1["user_id"], [lot2], car["user_id"]),
        lambda: svc.delegate_carrier(eid, b1["user_id"], [lot2], car2["user_id"]),
    ])
    assert all(r[0] == "ok" for r in results), results
    # serialised last-writer-wins: exactly one current carrier, version advanced
    st = svc.pickup_status(eid, b1["user_id"], lot2)
    assert st["carrier_id"] in {car["user_id"], car2["user_id"]}
    assert st["auth_version"] == 3  # implicit v1 -> two delegations -> v3


# ---------------------------------------------------------------------------
# Invariants / extra atomicity edges
# ---------------------------------------------------------------------------
def test_delivery_does_not_change_winner_price_or_budget(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"], reserve=100, bid=800)
    before_lot = c.get(f"/events/{eid}/lots/{lot}").json()
    before_budget = c.get(f"/events/{eid}/budget", headers=H(world["b1"])).json()

    delegate(c, eid, world["b1"], [lot], world["car"])
    k = cred(c, eid, lot, world["car"])
    assert deliver(c, eid, world["s1"], "inv", world["car"],
                   [(lot, k)]).status_code == 201

    after_lot = c.get(f"/events/{eid}/lots/{lot}").json()
    after_budget = c.get(f"/events/{eid}/budget", headers=H(world["b1"])).json()
    assert after_lot["status"] == "closed"
    assert after_lot["current_price"] == before_lot["current_price"] == 100
    assert after_budget == before_budget
    # winner's my-bids still records the same purchase
    mine = c.get(f"/events/{eid}/my-bids", headers=H(world["b1"])).json()["bids"]
    row = next(x for x in mine if x["lot_id"] == lot)
    assert row["won"] is True and row["purchase_price"] == 100


def test_batch_delegate_to_seller_of_one_lot_fails_entire_batch(world):
    c, eid = world["c"], world["eid"]
    a = world["win"](world["b1"], seller=world["s1"])
    b = world["win"](world["b1"], seller=world["s2"])
    # s2 is the seller of b but not a: the whole delegation to s2 is refused
    r = delegate(c, eid, world["b1"], [a, b], world["s2"])
    assert r.status_code == 422
    assert status(c, eid, a, world["b1"]).json()["carrier_id"] is None
    assert status(c, eid, b, world["b1"]).json()["carrier_id"] is None
    # delegating both to a neutral carrier works
    assert delegate(c, eid, world["b1"], [a, b], world["car"]).status_code == 200
    assert status(c, eid, a, world["car"]).status_code == 200
    assert status(c, eid, b, world["car"]).status_code == 200


def test_same_request_id_can_be_reused_by_another_seller(world):
    c, eid = world["c"], world["eid"]
    a = world["win"](world["b1"], seller=world["s1"])
    b = world["win"](world["b2"], seller=world["s2"])
    ka = cred(c, eid, a, world["b1"])
    kb = cred(c, eid, b, world["b2"])
    r1 = deliver(c, eid, world["s1"], "SHARED-KEY", world["b1"], [(a, ka)])
    r2 = deliver(c, eid, world["s2"], "SHARED-KEY", world["b2"], [(b, kb)])
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


def test_unsold_lot_cannot_enter_pickup_flow(world):
    c, eid = world["c"], world["eid"]
    lot = c.post(f"/events/{eid}/lots",
                 json={"title": "unsold", "reserve": 500},
                 headers=H(world["s1"])).json()["id"]
    c.post(f"/events/{eid}/lots/{lot}/close", headers=H(world["s1"]))
    r = status(c, eid, lot, world["s1"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "lot_not_sold"
    r = delegate(c, eid, world["s1"], [lot], world["car"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "lot_not_sold"


def test_pickup_worklist_lists_only_related_lots(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b2"])
    delegate(c, eid, world["b1"], [l1], world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [l1], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))

    car_lots = [x["lot_id"] for x in c.get(
        f"/events/{eid}/pickups?as=carrier",
        headers=H(world["car"])).json()["pickups"]]
    assert car_lots == [l1]
    drv_lots = [x["lot_id"] for x in c.get(
        f"/events/{eid}/pickups?role=driver",
        headers=H(world["drv"])).json()["pickups"]]
    assert drv_lots == [l1]
    b2_lots = [x["lot_id"] for x in c.get(
        f"/events/{eid}/my-pickups", headers=H(world["b2"])).json()["pickups"]]
    assert b2_lots == [l2]


def test_delegate_batch_with_delivered_lot_rolls_back_entire_batch(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    k1 = cred(c, eid, l1, world["b1"])
    deliver(c, eid, world["s1"], "d1", world["b1"], [(l1, k1)])

    # [pending l2 (write first), delivered l1 (fails)]: refused and the
    # earlier write to l2 must roll back, leaving the whole batch unchanged.
    r = delegate(c, eid, world["b1"], [l2, l1], world["car"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_delivered"
    body = status(c, eid, l2, world["b1"]).json()
    assert body["carrier_id"] is None
    assert body["authorized_picker_id"] == world["b1"]["user_id"]


def test_carrier_credential_stops_working_once_driver_assigned(world):
    c, eid = world["c"], world["eid"]
    lot = world["win"](world["b1"])
    delegate(c, eid, world["b1"], [lot], world["car"])
    carrier_code = cred(c, eid, lot, world["car"])
    c.post(f"/events/{eid}/pickups/assign-driver",
           json={"lot_ids": [lot], "driver_id": world["drv"]["user_id"]},
           headers=H(world["car"]))
    # the carrier-era credential can no longer deliver, even presenting carrier
    r = deliver(c, eid, world["s1"], "late-carrier", world["car"],
                [(lot, carrier_code)])
    assert r.status_code == 409
    # the driver's current credential delivers fine
    driver_code = cred(c, eid, lot, world["drv"])
    assert deliver(c, eid, world["s1"], "driver-ok", world["drv"],
                   [(lot, driver_code)]).status_code == 201


def test_batch_credential_endpoint_returns_all_visible_codes(world):
    c, eid = world["c"], world["eid"]
    l1 = world["win"](world["b1"])
    l2 = world["win"](world["b1"])
    r = c.post(f"/events/{eid}/pickups/credentials",
               json={"lot_ids": [l1, l2]}, headers=H(world["b1"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["credential_by_lot"]) == {str(l1), str(l2)}
    # an outsider cannot batch-fetch either
    r = c.post(f"/events/{eid}/pickups/credentials",
               json={"lot_ids": [l1, l2]}, headers=H(world["b2"]))
    assert r.status_code == 403


def test_parallel_same_request_id_delivery_creates_one_receipt(tmp_path):
    db, svc, eid, seller, b1, car, lot, cred = _service_world(
        tmp_path, "idem-race.db")
    results = _run_threads([
        lambda: svc.confirm_delivery(
            eid, seller["user_id"], request_key="same",
            picker_ref=car["user_id"],
            items=[{"lot_id": lot, "credential": cred}]),
        lambda: svc.confirm_delivery(
            eid, seller["user_id"], request_key="same",
            picker_ref=car["user_id"],
            items=[{"lot_id": lot, "credential": cred}]),
    ])
    assert all(r[0] == "ok" for r in results), results
    ids = {r[1][0]["id"] for r in results}
    assert len(ids) == 1
    receipts = svc.list_receipts(eid, seller["user_id"])["receipts"]
    assert len(receipts) == 1
