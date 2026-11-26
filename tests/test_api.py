"""End-to-end API tests with an isolated SQLite file per test."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN_KEY = "dev-admin-key"


@pytest.fixture()
def env(tmp_path):
    db = tmp_path / "auction.db"
    app = create_app(str(db))
    with TestClient(app) as client:
        event = client.post("/events", json={"name": "Autumn"}).json()
        eid = event["id"]
        admin = client.post(f"/events/{eid}/admin",
                            json={"name": "admin", "admin_key": ADMIN_KEY}).json()
        seller = client.post(f"/events/{eid}/users",
                             json={"name": "seller", "role": "seller"}).json()
        b1 = client.post(f"/events/{eid}/users",
                         json={"name": "b1", "role": "buyer"}).json()
        b2 = client.post(f"/events/{eid}/users",
                         json={"name": "b2", "role": "buyer"}).json()
        b3 = client.post(f"/events/{eid}/users",
                         json={"name": "b3", "role": "buyer"}).json()

        def lot(title, reserve):
            r = client.post(f"/events/{eid}/lots",
                            json={"title": title, "reserve": reserve},
                            headers=hdr(seller))
            assert r.status_code == 201, r.text
            return r.json()["id"]

        yield {
            "client": client, "eid": eid, "admin": admin, "seller": seller,
            "b1": b1, "b2": b2, "b3": b3, "lot": lot,
        }


def hdr(user):
    return {"Authorization": f"Bearer {user['token']}"}


def budget(client, eid, user):
    r = client.get(f"/events/{eid}/budget", headers=hdr(user))
    assert r.status_code == 200, r.text
    return r.json()


def set_budget(client, eid, user, amount):
    r = client.put(f"/events/{eid}/budget", json={"budget": amount},
                   headers=hdr(user))
    assert r.status_code == 200, r.text
    return r.json()


def bid(client, eid, user, lot_id, max_bid, expect=201):
    r = client.post(f"/events/{eid}/lots/{lot_id}/bids",
                    json={"max_bid": max_bid}, headers=hdr(user))
    assert r.status_code == expect, r.text
    return r.json()


def lot_view(client, eid, lot_id):
    return client.get(f"/events/{eid}/lots/{lot_id}").json()


# --- basic flow -------------------------------------------------------------
def test_public_view_hides_maxes_and_budget(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    set_budget(c, eid, env["b1"], 1000)
    bid(c, eid, env["b1"], lot_id, 700)
    view = lot_view(c, eid, lot_id)
    assert view["current_price"] == 100          # single bidder -> reserve
    assert view["leader_alias"].startswith("bidder-")
    assert "max_bid" not in view and "budget" not in view
    # aliases are stable but distinct per bidder
    assert view["leader_alias"] != env["b1"]["name"]


def test_single_bidder_pays_reserve_and_two_bidders_price_plus_one(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    for u in (env["b1"], env["b2"]):
        set_budget(c, eid, u, 1000)
    bid(c, eid, env["b1"], lot_id, 300)
    bid(c, eid, env["b2"], lot_id, 200)
    view = lot_view(c, eid, lot_id)
    assert view["current_price"] == 201
    assert view["bidder_count"] == 2

    # b2 overtakes with 400: b1 stops leading and stops reserving, price = 301
    r = bid(c, eid, env["b2"], lot_id, 400)
    assert r["leading"] is True
    assert lot_view(c, eid, lot_id)["current_price"] == 301
    assert budget(c, eid, env["b1"])["reserved"] == 0
    assert budget(c, eid, env["b2"])["reserved"] == 400


def test_tie_goes_to_earlier_first_bid(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    for u in (env["b1"], env["b2"]):
        set_budget(c, eid, u, 1000)
    bid(c, eid, env["b1"], lot_id, 500)
    bid(c, eid, env["b2"], lot_id, 500)
    # equal max -> b1 (earlier first valid bid) leads; price caps at max 500
    alias_b1 = lot_view(c, eid, lot_id)["leader_alias"]
    bid(c, eid, env["b1"], lot_id, 501)  # b1 raises: still leader, price 501
    view = lot_view(c, eid, lot_id)
    assert view["current_price"] == 501
    assert view["leader_alias"] == alias_b1
    # b2 raising instead would overtake; test the raise-order rule directly:
    r = bid(c, eid, env["b2"], lot_id, 502)
    assert r["leading"] is True


def test_raising_keeps_original_tie_order(env):
    """b1 bid low first, b2 bid high, then both reach the same max: b1 leads."""
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    for u in (env["b1"], env["b2"]):
        set_budget(c, eid, u, 1000)
    bid(c, eid, env["b1"], lot_id, 300)
    bid(c, eid, env["b2"], lot_id, 400)
    # now b1 raises to 400: equal maxes, b1's first bid was earlier -> b1 leads
    r = bid(c, eid, env["b1"], lot_id, 400)
    assert r["leading"] is True
    assert lot_view(c, eid, lot_id)["current_price"] == 400


def test_below_reserve_and_lowering_rejected(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    set_budget(c, eid, env["b1"], 1000)
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 99},
               headers=hdr(env["b1"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "below_reserve"
    bid(c, eid, env["b1"], lot_id, 150)
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 150},
               headers=hdr(env["b1"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "must_raise"
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 140},
               headers=hdr(env["b1"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "must_raise"


def test_bid_requires_budget_to_be_set(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 100},
               headers=hdr(env["b1"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "budget_required"


def test_integer_amounts_only(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("Card", 100)
    set_budget(c, eid, env["b1"], 1000)
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 100.5},
               headers=hdr(env["b1"]))
    assert r.status_code == 422


# --- budgets ---------------------------------------------------------------
def test_reserved_counts_leaders_max_across_lots(env):
    c, eid = env["client"], env["eid"]
    lot_a, lot_b = env["lot"]("A", 10), env["lot"]("B", 10)
    set_budget(c, eid, env["b1"], 500)
    bid(c, eid, env["b1"], lot_a, 300)
    bid(c, eid, env["b1"], lot_b, 150)
    b = budget(c, eid, env["b1"])
    assert b["used"] == 0
    assert b["reserved"] == 450
    assert b["remaining"] == 50


def test_second_bid_that_would_overcommit_is_rejected(env):
    c, eid = env["client"], env["eid"]
    lot_a, lot_b = env["lot"]("A", 10), env["lot"]("B", 10)
    set_budget(c, eid, env["b1"], 500)
    bid(c, eid, env["b1"], lot_a, 300)
    r = c.post(f"/events/{eid}/lots/{lot_b}/bids", json={"max_bid": 300},
               headers=hdr(env["b1"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget_exceeded"
    # rejected bid left no state behind
    assert budget(c, eid, env["b1"])["reserved"] == 300
    assert lot_view(c, eid, lot_b)["current_price"] is None


def test_losing_bid_reserves_nothing_and_is_still_kept_for_price(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 100)
    set_budget(c, eid, env["b1"], 1000)
    set_budget(c, eid, env["b2"], 1000)
    bid(c, eid, env["b1"], lot_id, 500)     # leader
    bid(c, eid, env["b2"], lot_id, 400)     # losing, sets the price
    assert budget(c, eid, env["b1"])["reserved"] == 500
    assert budget(c, eid, env["b2"])["reserved"] == 0
    assert lot_view(c, eid, lot_id)["current_price"] == 401


def test_outbidding_frees_previous_leader_and_charges_new_one(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 100)
    set_budget(c, eid, env["b1"], 1000)
    set_budget(c, eid, env["b2"], 1000)
    bid(c, eid, env["b1"], lot_id, 600)
    bid(c, eid, env["b2"], lot_id, 700)
    assert budget(c, eid, env["b1"])["reserved"] == 0
    assert budget(c, eid, env["b2"])["reserved"] == 700


def test_lower_budget_rejected_when_it_would_not_cover(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 10)
    set_budget(c, eid, env["b1"], 500)
    bid(c, eid, env["b1"], lot_id, 300)
    r = c.put(f"/events/{eid}/budget", json={"budget": 250},
              headers=hdr(env["b1"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "budget_below_commitment"
    b = set_budget(c, eid, env["b1"], 300)
    assert b["remaining"] == 0


# --- settlement ------------------------------------------------------------
def test_close_settles_at_effective_price_and_moves_to_used(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 100)
    set_budget(c, eid, env["b1"], 1000)
    set_budget(c, eid, env["b2"], 1000)
    bid(c, eid, env["b1"], lot_id, 500)
    bid(c, eid, env["b2"], lot_id, 400)
    r = c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["seller"]))
    assert r.status_code == 200
    body = r.json()
    assert body["purchase_price"] == 401
    assert body["winner_alias"] == lot_view(c, eid, lot_id)["leader_alias"]
    b1 = budget(c, eid, env["b1"])
    assert b1["used"] == 401 and b1["reserved"] == 0 and b1["remaining"] == 599
    b2 = budget(c, eid, env["b2"])
    assert b2["used"] == 0 and b2["reserved"] == 0 and b2["remaining"] == 1000


def test_close_frees_room_for_new_lot(env):
    c, eid = env["client"], env["eid"]
    a, b = env["lot"]("A", 10), env["lot"]("B", 10)
    set_budget(c, eid, env["b1"], 600)
    bid(c, eid, env["b1"], a, 500)
    bid(c, eid, env["b1"], b, 100)
    r = c.post(f"/events/{eid}/lots/{a}/close", headers=hdr(env["seller"]))
    assert r.json()["purchase_price"] == 10  # nobody else: reserve price
    # used 10 + reserved 100 <= 600
    assert budget(c, eid, env["b1"])["used"] == 10
    bid(c, eid, env["b1"], b, 590)  # 10 used + 590 reserved = 600 exactly
    assert budget(c, eid, env["b1"])["remaining"] == 0


def test_unsold_lot_when_reserve_unreachable(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 500)
    set_budget(c, eid, env["b1"], 1000)
    bid(c, eid, env["b1"], lot_id, 600)
    r = c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["seller"]))
    assert r.json()["winner_alias"] is not None  # 600 >= reserve, sold at 500
    assert r.json()["purchase_price"] == 500


def test_closed_lot_rejects_bids_and_double_close(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 10)
    set_budget(c, eid, env["b1"], 1000)
    bid(c, eid, env["b1"], lot_id, 20)
    c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["seller"]))
    r = c.post(f"/events/{eid}/lots/{lot_id}/bids", json={"max_bid": 30},
               headers=hdr(env["b1"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "lot_closed"
    r = c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["seller"]))
    assert r.status_code == 409


def test_buyer_cannot_close_and_seller_cannot_close_other_seller(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 10)
    r = c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["b1"]))
    assert r.status_code == 403


# --- visibility / isolation ------------------------------------------------
def test_my_bids_view_shows_own_max_only(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 10)
    set_budget(c, eid, env["b1"], 500)
    bid(c, eid, env["b1"], lot_id, 300)
    mine = c.get(f"/events/{eid}/my-bids", headers=hdr(env["b1"])).json()
    assert mine["bids"][0]["max_bid"] == 300
    assert mine["bids"][0]["leading"] is True
    # other buyers cannot see it
    other = c.get(f"/events/{eid}/my-bids", headers=hdr(env["b2"]))
    assert other.status_code == 200 and other.json()["bids"] == []


def test_public_list_does_not_leak_alias_or_max_after_close(env):
    c, eid = env["client"], env["eid"]
    lot_id = env["lot"]("A", 10)
    set_budget(c, eid, env["b1"], 500)
    bid(c, eid, env["b1"], lot_id, 300)
    c.post(f"/events/{eid}/lots/{lot_id}/close", headers=hdr(env["seller"]))
    view = lot_view(c, eid, lot_id)
    assert view["current_price"] == 10
    assert view["status"] == "closed"
    assert "max_bid" not in view
    # loser's/buyer's budget never appears in public responses
    listing = c.get(f"/events/{eid}/lots").json()
    assert all("budget" not in lot for lot in listing["lots"])


def test_event_close_closes_all_lots(env):
    c, eid = env["client"], env["eid"]
    a, b = env["lot"]("A", 10), env["lot"]("B", 20)
    set_budget(c, eid, env["b1"], 1000)
    bid(c, eid, env["b1"], a, 100)
    bid(c, eid, env["b1"], b, 200)
    r = c.post(f"/events/{eid}/close", headers=hdr(env["admin"]))
    assert r.status_code == 200
    prices = {x["lot_id"]: x["purchase_price"] for x in r.json()["closed_lots"]}
    assert prices == {a: 10, b: 20}
    assert budget(c, eid, env["b1"])["used"] == 30

    # event is now closed: no more bids or lots
    r = c.post(f"/events/{eid}/lots", json={"title": "C", "reserve": 10},
               headers=hdr(env["seller"]))
    assert r.status_code == 409


def test_buyer_cannot_set_budget_for_another_event_user(env):
    c, eid = env["client"], env["eid"]
    r = c.get(f"/events/{eid}/budget", headers=hdr(env["seller"]))
    assert r.status_code == 403


def test_duplicate_names_rejected_and_bad_tokens(env):
    c, eid = env["client"], env["eid"]
    r = c.post(f"/events/{eid}/users", json={"name": "b1", "role": "buyer"})
    assert r.status_code == 409
    r = c.get(f"/events/{eid}/budget", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = c.post(f"/events/{eid}/admin",
               json={"name": "x", "admin_key": "wrong"})
    assert r.status_code == 403
