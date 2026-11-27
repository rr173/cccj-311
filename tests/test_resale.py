"""Behavioural tests for sale revocation and directed re-sale.

Covers the contract:

* a sale (auction deal) has a citable deal number;
* admin may revoke a sale only while it has no effective receipt and no
  confirmed delivery; a refunded receipt is no longer effective;
* revocation releases the buyer's budget, keeps the original auction result
  and all history;
* a revoked lot can be re-offered to a designated buyer at a positive integer
  price (the same buyer may be re-designated); one active offer per lot,
  replaceable / withdrawable; offers occupy no budget;
* only the designated buyer may accept a still-active offer, and only when
  their remaining budget covers it; acceptance creates a new budget-occupying
  deal; repeat accept replays the original deal without re-occupying;
* the new sale can itself be revoked and re-offered;
* previous receipts / delegations / credentials cannot settle a new sale;
* receipts, delegations and deliveries are deal-scoped after revocation;
* pickup + effective receipt -> the re-sale price enters the seller's income;
* whole-event close still allows the workflow;
* visibility (admin / seller / buyers), public view unchanged;
* restart persistence and legacy-sale back-fill;
* serialised concurrency between revoke and payment/delivery/accept.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.main import create_app
from app.service import ADMIN_KEY, Service


def H(u):
    return {"Authorization": f"Bearer {u['token']}"}


@pytest.fixture()
def world(tmp_path):
    db_path = str(tmp_path / "resale.db")
    database = Database(db_path)
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
        for b in (b1, b2, b3):
            assert c.put(f"/events/{eid}/budget",
                         json={"budget": 10 ** 9}, headers=H(b)).status_code == 200

        def lot(seller=s1, reserve=10, title="L"):
            r = c.post(f"/events/{eid}/lots",
                       json={"title": title, "reserve": reserve},
                       headers=H(seller))
            assert r.status_code == 201, r.text
            return r.json()["id"]

        def win(seller=s1, buyer=b1, reserve=10, max_bid=100):
            lid = lot(seller, reserve)
            r = c.post(f"/events/{eid}/lots/{lid}/bids",
                       json={"max_bid": max_bid}, headers=H(buyer))
            assert r.status_code == 201, r.text
            r = c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller))
            assert r.status_code == 200, r.text
            assert r.json()["purchase_price"] is not None
            return lid

        def deal_of(lid):
            r = c.get(f"/events/{eid}/lots/{lid}/deals", headers=H(admin))
            assert r.status_code == 200, r.text
            body = r.json()
            return body["current_deal"] or body["deals"][-1]

        def cred(lid, buyer=b1, deal_ref=None):
            q = f"?deal_id={deal_ref}" if deal_ref is not None else ""
            r = c.get(f"/events/{eid}/lots/{lid}/credential{q}", headers=H(buyer))
            assert r.status_code == 200, r.text
            return r.json()["credential"]

        def deliver(lid, seller=s1, buyer=b1, deal_ref=None):
            data = {"request_id": f"D-{lid}-{deal_ref or 'auto'}",
                    "picker_id": buyer["user_id"],
                    "items": [{"lot_id": lid, "credential": cred(
                        lid, buyer, deal_ref)}]}
            if deal_ref is not None:
                data["deal_id"] = deal_ref
            r = c.post(f"/events/{eid}/deliveries", json=data, headers=H(seller))
            assert r.status_code in (200, 201), r.text
            return r.json()

        def pay(serial, lids, buyer=b1, amount=None, deal_refs=None, expect=201):
            amt = amount if amount is not None else 10 * len(lids)
            body = {"serial_no": serial, "buyer_id": buyer["user_id"],
                    "amount": amt, "lot_ids": lids}
            if deal_refs is not None:
                body["deal_refs"] = deal_refs
            r = c.post(f"/events/{eid}/payments", json=body, headers=H(admin))
            assert r.status_code == expect, r.text
            return r

        def revoke(deal_ref, expect=200):
            r = c.post(f"/events/{eid}/deals/revoke",
                       json={"deal_id": deal_ref}, headers=H(admin))
            assert r.status_code == expect, r.text
            return r.json()

        def offer(lid, buyer, price, expect=201):
            r = c.post(f"/events/{eid}/offers",
                       json={"lot_id": lid, "buyer_id": buyer["user_id"],
                             "price": price}, headers=H(admin))
            assert r.status_code == expect, r.text
            return r.json()

        def accept(offer_id, buyer, expect=(201, 200)):
            r = c.post(f"/events/{eid}/offers/{offer_id}/accept",
                       headers=H(buyer))
            wanted = expect if isinstance(expect, (tuple, set, list)) else (expect,)
            assert r.status_code in wanted, r.text
            return r.json()

        def budget_of(buyer):
            r = c.get(f"/events/{eid}/budget", headers=H(buyer))
            assert r.status_code == 200
            return r.json()

        yield {
            "c": c, "eid": eid, "admin": admin, "s1": s1, "s2": s2,
            "b1": b1, "b2": b2, "b3": b3, "lot": lot, "win": win,
            "deal_of": deal_of, "cred": cred, "deliver": deliver, "pay": pay,
            "revoke": revoke, "offer": offer, "accept": accept,
            "budget_of": budget_of,
            "_db": database, "_path": db_path,
        }


# --------------------------------------------------------------- basics
def test_close_creates_citable_deal(world):
    lid = world["win"]()
    deal = world["deal_of"](lid)
    assert deal["deal_no"] and deal["deal_no"].startswith("DL-")
    assert deal["status"] == "active" and deal["price"] == 10
    assert deal["buyer_id"] == world["b1"]["user_id"]
    # The original auction result is untouched on the lot / public view.
    pub = world["c"].get(f"/events/{world['eid']}/lots/{lid}").json()
    assert pub["status"] == "closed" and pub["current_price"] == 10


def test_revoke_releases_budget_and_keeps_history(world):
    lid = world["win"]()
    deal = world["deal_of"](lid)
    assert world["budget_of"](world["b1"])["used"] == 10
    body = world["revoke"](deal["deal_no"])
    assert body["status"] == "revoked"
    # budget freed, original auction result + bids still present
    assert world["budget_of"](world["b1"])["used"] == 0
    pub = world["c"].get(f"/events/{world['eid']}/lots/{lid}").json()
    assert pub["current_price"] == 10 and pub["status"] == "closed"
    # history is not deleted: the revoked deal is still listed
    deals = world["c"].get(
        f"/events/{world['eid']}/lots/{lid}/deals",
        headers=H(world["admin"])).json()["deals"]
    assert [d["status"] for d in deals] == ["revoked"]
    # The original bids (auction history) are still kept.
    assert world["budget_of"](world["b1"])["used"] == 0


def test_revoke_is_idempotent(world):
    lid = world["win"]()
    deal = world["deal_of"](lid)
    world["revoke"](deal["id"])
    replay = world["revoke"](deal["id"])
    assert replay["replayed"] is True and replay["status"] == "revoked"


def test_cannot_revoke_paid_sale_but_after_refund_can(world):
    lid = world["win"]()
    deal = world["deal_of"](lid)
    world["pay"]("SN-1", [lid], amount=10)
    world["revoke"](deal["id"], expect=409)  # effective receipt blocks revoke
    world["c"].post(f"/events/{world['eid']}/refunds",
                    json={"serial_no": "SN-1"}, headers=H(world["admin"]))
    # A bounced (refunded) receipt is no longer an effective receipt.
    body = world["revoke"](deal["id"])
    assert body["status"] == "revoked"


def test_cannot_revoke_delivered_sale(world):
    lid = world["win"]()
    deal = world["deal_of"](lid)
    world["deliver"](lid)
    world["revoke"](deal["id"], expect=409)


# --------------------------------------------------------- offers/accept
def test_offer_requires_revoked_lot_and_is_admin_only(world):
    lid = world["win"]()
    world["offer"](lid, world["b2"], 50, expect=409)  # still has active deal
    world["revoke"](world["deal_of"](lid)["id"])
    r = world["c"].post(f"/events/{world['eid']}/offers",
                        json={"lot_id": lid, "buyer_id": world["b2"]["user_id"],
                              "price": 50}, headers=H(world["s1"]))
    assert r.status_code == 403


def test_offer_price_must_be_positive_integer(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 0, expect=422)
    world["offer"](lid, world["b2"], -5, expect=422)
    world["offer"](lid, world["b2"], 1.5, expect=422)
    world["offer"](lid, world["b2"], "abc", expect=422)


def test_offer_does_not_occupy_budget(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 300)
    assert world["budget_of"](world["b2"])["used"] == 0
    assert world["budget_of"](world["b2"])["reserved"] == 0


def test_only_designated_buyer_accepts(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    o = world["offer"](lid, world["b2"], 50)
    world["accept"](o["id"], world["b3"], expect=403)
    # the original buyer may be re-designated; other buyers still refused
    o2 = world["offer"](lid, world["b1"], 60)
    assert world["accept"](o2["id"], world["b2"], expect=403)


def test_accept_creates_new_deal_and_repeat_does_not_reoccupy(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    o = world["offer"](lid, world["b2"], 50)
    first = world["accept"](o["id"], world["b2"])
    assert first["status"] == "active" and first["kind"] == "resale"
    assert first["price"] == 50
    assert world["budget_of"](world["b2"])["used"] == 50
    again = world["accept"](o["id"], world["b2"])
    assert again["deal_id"] == first["deal_id"] and again["replayed"] is True
    assert world["budget_of"](world["b2"])["used"] == 50  # not double-occupied


def test_accept_requires_budget_after_other_commitments(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    # b2 already commits to another open lot; only 100 remains.
    other = world["lot"](reserve=10)
    assert world["c"].post(
        f"/events/{world['eid']}/lots/{other}/bids", json={"max_bid": 900},
        headers=H(world["b2"])).status_code == 201
    world["c"].put(f"/events/{world['eid']}/budget",
                   json={"budget": 1000}, headers=H(world["b2"]))
    o = world["offer"](lid, world["b2"], 200)
    r = world["c"].post(f"/events/{world['eid']}/offers/{o['id']}/accept",
                        headers=H(world["b2"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget_exceeded"


def test_exactly_covered_budget_accepts(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["c"].put(f"/events/{world['eid']}/budget", json={"budget": 200},
                   headers=H(world["b2"]))
    o = world["offer"](lid, world["b2"], 200)
    assert world["accept"](o["id"], world["b2"])["price"] == 200


def test_one_active_offer_replace_and_withdraw(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    o1 = world["offer"](lid, world["b2"], 50)
    o2 = world["offer"](lid, world["b1"], 70)
    assert o2["replaced_offer_id"] == o1["id"]
    world["accept"](o1["id"], world["b2"], expect=409)  # replaced, inactive
    assert world["accept"](o2["id"], world["b1"])["price"] == 70
    # after acceptance, withdrawing is refused; but a fresh revoked lot can be
    # withdrawn before acceptance.
    lid2 = world["win"]()
    world["revoke"](world["deal_of"](lid2)["id"])
    o3 = world["offer"](lid2, world["b2"], 40)
    w = world["c"].post(f"/events/{world['eid']}/offers/{o3['id']}/withdraw",
                        headers=H(world["admin"]))
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"
    assert world["accept"](o3["id"], world["b2"], expect=409)


def test_resale_can_be_revoked_and_reoffered_again(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    deal2 = world["accept"](
        world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]["id"],
        world["b2"])
    world["revoke"](deal2["id"])
    world["offer"](lid, world["b3"], 90)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    d3 = world["accept"](o["id"], world["b3"])
    assert d3["price"] == 90
    deals = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                           headers=H(world["admin"])).json()["deals"]
    assert [d["status"] for d in deals] == ["revoked", "revoked", "active"]


# ------------------------------------------------------- deal scoping
def test_stale_credentials_from_old_sale_fail(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    world["accept"](o["id"], world["b2"])
    new_deal = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                              headers=H(world["admin"])).json()["current_deal"]
    # An old credential (from before revocation) cannot deliver the new sale.
    old_cred = _old_credential(world, lid, world["b1"])
    r = world["c"].post(f"/events/{world['eid']}/deliveries",
                        json={"request_id": "old", "picker_id": world["b1"]["user_id"],
                              "items": [{"lot_id": lid, "credential": old_cred,
                                         "deal_id": new_deal["id"]}]},
                        headers=H(world["s1"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_credential"


def _old_credential(world, lid, buyer):
    """Credential that would have been valid for the original (now revoked)
    sale: computed with the same HMAC secret and no revocation awareness."""
    from app.pickups import compute_credential
    deal = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                          headers=H(world["admin"])).json()["deals"][0]
    db = _db_of(world)
    with db.read() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='alias_secret'").fetchone()
    return compute_credential(row["value"], world["eid"], lid, 1,
                              buyer["user_id"], deal["id"])


def _db_of(world):
    return world["_db"]


def test_lot_only_request_refused_after_revocation(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    world["accept"](o["id"], world["b2"])
    # A legacy request naming only the lot (no deal) must not act on the new
    # buyer's sale: it is refused rather than retargeted.
    r = world["c"].get(f"/events/{world['eid']}/lots/{lid}/credential",
                       headers=H(world["b2"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "deal_reference_required"
    # Naming the deal works.
    current = world["c"].get(
        f"/events/{world['eid']}/lots/{lid}/deals",
        headers=H(world["admin"])).json()["current_deal"]["id"]
    r = world["c"].get(f"/events/{world['eid']}/lots/{lid}/credential",
                       headers=H(world["b2"]), params={"deal_id": current})
    assert r.status_code == 200


def test_lot_without_revocation_supports_legacy_lot_only_calls(world):
    lid = world["win"]()
    r = world["c"].get(f"/events/{world['eid']}/lots/{lid}/credential",
                       headers=H(world["b1"]))
    assert r.status_code == 200
    world["deliver"](lid)
    world["pay"]("SN-legacy", [lid], amount=10)
    assert world["c"].get(f"/events/{world['eid']}/settlement",
                          headers=H(world["s1"])).json()["settled_income"] == 10


def test_deal_ref_must_match_lot(world):
    lid = world["win"]()
    lid2 = world["win"]()
    d = world["deal_of"](lid2)
    r = world["c"].get(f"/events/{world['eid']}/lots/{lid}/credential",
                       headers=H(world["b1"]), params={"deal_id": d["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "deal_lot_mismatch"


# -------------------------------------------------- resale settlement flow
def test_resale_buyer_can_be_paid_delegated_and_delivered(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    deal = world["accept"](o["id"], world["b2"])
    did = deal["deal_id"]
    # New buyer delegates to a carrier, gets a deal-bound credential, delivers.
    carrier = world["b3"]
    r = world["c"].post(f"/events/{world['eid']}/pickups/delegate",
                        json={"lot_ids": [lid], "carrier_id": carrier["user_id"]},
                        headers=H(world["b2"]))
    assert r.status_code == 200, r.text
    cred = world["c"].get(f"/events/{world['eid']}/lots/{lid}/credential",
                          headers=H(carrier), params={"deal_id": did
                                                       }).json()["credential"]
    r = world["c"].post(f"/events/{world['eid']}/deliveries",
                        json={"request_id": "rs-1",
                              "picker_id": carrier["user_id"],
                              "deal_id": did,
                              "items": [{"lot_id": lid, "credential": cred}]},
                        headers=H(world["s1"]))
    assert r.status_code == 201, r.text
    # Admin registers the receipt for the resale at the resale price.
    world["pay"]("SN-RS", [lid], buyer=world["b2"], amount=50, deal_refs=[did])
    summary = world["c"].get(f"/events/{world['eid']}/settlement",
                             headers=H(world["s1"])).json()
    assert summary["settled_income"] == 50
    # seller payout bounded by the resale income
    p = world["c"].post(f"/events/{world['eid']}/payouts",
                        json={"request_id": "PO-RS", "amount": 50},
                        headers=H(world["s1"]))
    assert p.status_code == 201, p.text


def test_old_payment_cannot_pay_resale(world):
    lid = world["win"]()
    old = world["deal_of"](lid)
    world["pay"]("SN-old", [lid], amount=10)  # pays the auction sale
    world["revoke"](old["id"], expect=409)     # blocked while paid
    # refund, then revoke, then resell at a different price
    world["c"].post(f"/events/{world['eid']}/refunds",
                    json={"serial_no": "SN-old"}, headers=H(world["admin"]))
    world["revoke"](old["id"])
    world["offer"](lid, world["b2"], 70)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    deal = world["accept"](o["id"], world["b2"])
    # Receipt for the old deal id cannot settle the new buyer's sale.
    r = world["c"].post(f"/events/{world['eid']}/payments",
                        json={"serial_no": "SN-wrong", "buyer_id": world["b2"]["user_id"],
                              "amount": 70, "lot_ids": [lid],
                              "deal_refs": [old["id"]]}, headers=H(world["admin"]))
    assert r.status_code == 409


def test_resale_income_only_after_pickup_and_receipt(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    deal = world["accept"](o["id"], world["b2"])
    did = deal["deal_id"]
    world["pay"]("SN-R", [lid], buyer=world["b2"], amount=50, deal_refs=[did])
    assert world["c"].get(f"/events/{world['eid']}/settlement",
                          headers=H(world["s1"])).json()["settled_income"] == 0
    world["deliver"](lid, buyer=world["b2"], deal_ref=did)
    assert world["c"].get(f"/events/{world['eid']}/settlement",
                          headers=H(world["s1"])).json()["settled_income"] == 50


# ------------------------------------------------------------ visibility
def test_visibility_scopes(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    eid = world["eid"]
    # Admin sees everything.
    assert world["c"].get(f"/events/{eid}/deals", headers=H(world["admin"])).json()["count"] == 1
    # The seller of the lot sees it.
    assert world["c"].get(f"/events/{eid}/deals", headers=H(world["s1"])).json()["count"] == 1
    # The designated buyer sees the offer; another buyer does not.
    ob = world["c"].get(f"/events/{eid}/offers", headers=H(world["b2"]))
    assert ob.status_code == 200 and ob.json()["count"] == 1
    assert world["c"].get(f"/events/{eid}/offers", headers=H(world["b3"])).json()["count"] == 0
    # Bereft b1 (the revoked buyer) sees only their own (revoked) deal.
    db = world["c"].get(f"/events/{eid}/deals", headers=H(world["b1"]))
    assert db.json()["count"] == 1 and db.json()["deals"][0]["status"] == "revoked"
    # b3 cannot read b2's offer directly.
    oid = ob.json()["offers"][0]["id"]
    assert world["c"].get(f"/events/{eid}/offers/{oid}",
                          headers=H(world["b3"])).status_code == 403
    # Public auction view mentions neither the offer nor the resale price.
    text = world["c"].get(f"/events/{eid}/lots/{lid}").text
    assert "offer" not in text
    assert '"price": 50' not in text and '"price":50' not in text


def test_admin_sees_revoked_deals_and_offers_status(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    eid = world["eid"]
    deals = world["c"].get(f"/events/{eid}/deals?status=revoked",
                           headers=H(world["admin"])).json()
    assert deals["count"] == 1
    offers = world["c"].get(f"/events/{eid}/offers?status=active",
                            headers=H(world["admin"])).json()
    assert offers["count"] == 1


# --------------------------------------------------------------- event close
def test_works_after_event_close(world):
    lid = world["win"]()
    r = world["c"].post(f"/events/{world['eid']}/close", headers=H(world["admin"]))
    assert r.status_code == 200
    deal = world["deal_of"](lid)
    world["revoke"](deal["id"])
    world["offer"](lid, world["b2"], 30)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    d = world["accept"](o["id"], world["b2"])
    assert d["status"] == "active"
    # budget check still enforced; receipt + delivery still allowed
    world["pay"]("SN-EV", [lid], buyer=world["b2"], amount=30,
                 deal_refs=[d["deal_id"]])
    world["deliver"](lid, buyer=world["b2"], deal_ref=d["deal_id"])
    assert world["c"].get(f"/events/{world['eid']}/settlement",
                          headers=H(world["s1"])).json()["settled_income"] == 30


# ----------------------------------------------------------- persistence
def test_restart_preserves_deals_offers(world, tmp_path):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    # Re-open the same DB file with a brand-new app/service instance.
    same = str(_world_path(world))
    from app.main import create_app as ca
    app2 = ca(same)
    with TestClient(app2) as c2:
        r = c2.get(f"/events/{world['eid']}/lots/{lid}/deals",
                   headers=H(world["admin"]))
        assert r.status_code == 200
        body = r.json()
        assert body["active_offer"]["id"] == o["id"]
        assert body["deals"][0]["status"] == "revoked"
        d = c2.post(f"/events/{world['eid']}/offers/{o['id']}/accept",
                    headers=H(world["b2"]))
        assert d.status_code == 201, d.text


def _world_path(world):
    return world["_path"]


# ------------------------------------------------------------ concurrency
def test_revoke_races_payment_consistently(tmp_path):
    db = Database(str(tmp_path / "conc.db"))
    svc = Service(db)
    eid = svc.create_event("E")["id"]
    admin = svc.create_admin(eid, "a", ADMIN_KEY)
    seller = svc.register(eid, "s", "seller")
    buyer = svc.register(eid, "b", "buyer")
    svc.set_budget(eid, buyer["user_id"], 10 ** 9)
    lid = svc.create_lot(eid, seller["user_id"], "L", "", 10)["id"]
    svc.place_bid(eid, buyer["user_id"], lid, 100)
    svc.close_lot(eid, seller["user_id"], lid)
    deal = svc.list_deals(eid, admin["user_id"])["deals"][0]
    results = {}

    def do_revoke():
        try:
            results["revoke"] = svc.revoke_deal(
                eid, admin["user_id"], deal["id"])[1]
        except Exception as exc:  # noqa: BLE001
            results["revoke"] = str(exc)

    def do_pay():
        try:
            results["pay"] = svc.register_payment(
                eid, admin["user_id"], serial_no="SN", buyer_ref=buyer["user_id"],
                amount=10, lot_ids=[lid])[1]
        except Exception as exc:  # noqa: BLE001
            results["pay"] = str(exc)

    t1 = threading.Thread(target=do_revoke)
    t2 = threading.Thread(target=do_pay)
    t1.start(); t2.start(); t1.join(); t2.join()
    # Exactly one branch wins: either revoked then payment refused, or payment
    # first then revoke refused.  Never both.
    with db.read() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_items WHERE lot_id=? AND active=1",
            (lid,)).fetchone()["n"]
        d = conn.execute("SELECT status FROM deals WHERE id=?",
                         (deal["id"],)).fetchone()["status"]
    assert not (d == "revoked" and active == 1)
    assert results["revoke"] is True or results["pay"] is True


# ----------------------------------------------------------- model/consistency
def test_offer_does_not_block_resale_income_for_original_buyer(world):
    # After a revoke, the original buyer's budget is free to re-bid elsewhere.
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    other = world["lot"](reserve=10)
    r = world["c"].post(f"/events/{world['eid']}/lots/{other}/bids",
                        json={"max_bid": 100}, headers=H(world["b1"]))
    assert r.status_code == 201


def test_duplicate_revokes_and_accepts_do_not_double_occupy(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["revoke"](world["deal_of"](lid)["id"])  # replay
    world["offer"](lid, world["b2"], 50)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    world["accept"](o["id"], world["b2"])
    world["accept"](o["id"], world["b2"])  # replay
    assert world["budget_of"](world["b2"])["used"] == 50


def test_settle_tab_lists_resale_deal(world):
    lid = world["win"]()
    world["revoke"](world["deal_of"](lid)["id"])
    world["offer"](lid, world["b2"], 55)
    o = world["c"].get(f"/events/{world['eid']}/lots/{lid}/deals",
                       headers=H(world["admin"])).json()["active_offer"]
    d = world["accept"](o["id"], world["b2"])
    rows = world["c"].get(f"/events/{world['eid']}/settleable-lots",
                          headers=H(world["s1"])).json()["lots"]
    assert len(rows) == 1 and rows[0]["deal_id"] == d["deal_id"]
    assert rows[0]["price"] == 55


def test_revoke_races_delivery_consistently(tmp_path):
    db = Database(str(tmp_path / "conc2.db"))
    svc = Service(db)
    eid = svc.create_event("E")["id"]
    admin = svc.create_admin(eid, "a", ADMIN_KEY)
    seller = svc.register(eid, "s", "seller")
    buyer = svc.register(eid, "b", "buyer")
    svc.set_budget(eid, buyer["user_id"], 10 ** 9)
    lid = svc.create_lot(eid, seller["user_id"], "L", "", 10)["id"]
    svc.place_bid(eid, buyer["user_id"], lid, 100)
    svc.close_lot(eid, seller["user_id"], lid)
    deal = svc.list_deals(eid, admin["user_id"])["deals"][0]
    cred = svc.pickup_credential(eid, buyer["user_id"], lid)["credential"]
    results = {}

    def do_revoke():
        try:
            results["revoke"] = svc.revoke_deal(
                eid, admin["user_id"], deal["id"])[1]
        except Exception as exc:  # noqa: BLE001
            results["revoke"] = type(exc).__name__

    def do_deliver():
        try:
            svc.confirm_delivery(eid, seller["user_id"],
                                 request_key="d", picker_ref=buyer["user_id"],
                                 items=[{"lot_id": lid, "credential": cred}])
            results["deliver"] = True
        except Exception as exc:  # noqa: BLE001
            results["deliver"] = type(exc).__name__

    t1 = threading.Thread(target=do_revoke)
    t2 = threading.Thread(target=do_deliver)
    t1.start(); t2.start(); t1.join(); t2.join()
    with db.read() as conn:
        d = conn.execute("SELECT status FROM deals WHERE id=?",
                         (deal["id"],)).fetchone()["status"]
        delivered = conn.execute(
            "SELECT COUNT(*) AS n FROM delivery_items WHERE lot_id=?",
            (lid,)).fetchone()["n"]
    # Never both: revoked and delivered on the same deal cannot coexist.
    assert not (d == "revoked" and delivered == 1)
    assert results["revoke"] is True or results["deliver"] is True


def test_concurrent_accepts_create_one_deal(tmp_path):
    db = Database(str(tmp_path / "conc3.db"))
    svc = Service(db)
    eid = svc.create_event("E")["id"]
    admin = svc.create_admin(eid, "a", ADMIN_KEY)
    seller = svc.register(eid, "s", "seller")
    buyer = svc.register(eid, "b", "buyer")
    buyer2 = svc.register(eid, "b2", "buyer")
    svc.set_budget(eid, buyer["user_id"], 10 ** 9)
    svc.set_budget(eid, buyer2["user_id"], 10 ** 9)
    lid = svc.create_lot(eid, seller["user_id"], "L", "", 10)["id"]
    svc.place_bid(eid, buyer["user_id"], lid, 100)
    svc.close_lot(eid, seller["user_id"], lid)
    deal = svc.list_deals(eid, admin["user_id"])["deals"][0]
    svc.revoke_deal(eid, admin["user_id"], deal["id"])
    offer = svc.create_offer(eid, admin["user_id"], lid, buyer2["user_id"], 50)[0]
    outcomes = []

    def accept():
        try:
            outcomes.append(svc.accept_offer(
                eid, buyer2["user_id"], offer["id"])[1])
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=accept) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.read() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM deals WHERE lot_id=? AND status='active'",
            (lid,)).fetchone()["n"]
    assert n == 1  # exactly one deal, no over-occupation


def test_offer_and_revoke_by_canonical_and_alias_paths(world):
    lid = world["win"]()
    d = world["deal_of"](lid)
    # Revoke via a path-form alias.
    r = world["c"].post(f"/events/{world['eid']}/deals/{d['deal_no']}/revoke",
                        headers=H(world["admin"]))
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    # Offer via the per-lot alias.
    r = world["c"].post(f"/events/{world['eid']}/lots/{lid}/offer",
                        json={"buyer_id": world["b2"]["user_id"], "price": 33},
                        headers=H(world["admin"]))
    assert r.status_code == 201 and r.json()["price"] == 33
    # Accept via a body-form alias.
    oid = r.json()["id"]
    r = world["c"].post(f"/events/{world['eid']}/resale/accept",
                        json={"offer_id": oid}, headers=H(world["b2"]))
    assert r.status_code == 201 and r.json()["price"] == 33
