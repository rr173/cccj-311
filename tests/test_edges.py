"""Focused edge-case and disclosure tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN_KEY = "dev-admin-key"


@pytest.fixture()
def env(tmp_path):
    app = create_app(str(tmp_path / "edge.db"))
    with TestClient(app) as c:
        eid = c.post("/events", json={"name": "E"}).json()["id"]
        admin = c.post(f"/events/{eid}/admin",
                       json={"name": "admin", "admin_key": ADMIN_KEY}).json()
        seller = c.post(f"/events/{eid}/users",
                        json={"name": "s", "role": "seller"}).json()
        b1 = c.post(f"/events/{eid}/users",
                    json={"name": "b1", "role": "buyer"}).json()
        b2 = c.post(f"/events/{eid}/users",
                    json={"name": "b2", "role": "buyer"}).json()

        def lot(reserve=10, title="L"):
            return c.post(f"/events/{eid}/lots",
                          json={"title": title, "reserve": reserve},
                          headers=H(seller)).json()["id"]

        yield c, eid, admin, seller, b1, b2, lot


def H(u):
    return {"Authorization": f"Bearer {u['token']}"}


def put_budget(c, eid, u, n):
    return c.put(f"/events/{eid}/budget", json={"budget": n}, headers=H(u))


def post_bid(c, eid, u, lot, n):
    return c.post(f"/events/{eid}/lots/{lot}/bids", json={"max_bid": n},
                  headers=H(u))


def test_budget_required_before_first_bid(env):
    c, eid, _, _, b1, _, lot = env
    r = post_bid(c, eid, b1, lot(), 20)
    assert r.status_code == 409 and r.json()["error"]["code"] == "budget_required"


def test_budget_of_zero_allows_no_bid(env):
    c, eid, _, _, b1, _, lot = env
    put_budget(c, eid, b1, 0)
    r = post_bid(c, eid, b1, lot(), 20)
    assert r.status_code == 409 and r.json()["error"]["code"] == "budget_exceeded"


def test_exact_budget_is_accepted(env):
    c, eid, _, _, b1, _, lot = env
    put_budget(c, eid, b1, 250)
    r = post_bid(c, eid, b1, lot(), 250)
    assert r.status_code == 201
    b = c.get(f"/events/{eid}/budget", headers=H(b1)).json()
    assert b["reserved"] == 250 and b["remaining"] == 0


def test_reserve_change_locked_after_bid(env):
    c, eid, _, seller, b1, _, lot = env
    lid = lot(reserve=10)
    put_budget(c, eid, b1, 100)
    post_bid(c, eid, b1, lid, 20)
    r = c.patch(f"/events/{eid}/lots/{lid}", json={"reserve": 50},
                headers=H(seller))
    assert r.status_code == 409 and r.json()["error"]["code"] == "reserve_locked"


def test_reserve_change_allowed_before_any_bid(env):
    c, eid, _, seller, _, _, lot = env
    lid = lot(reserve=10)
    r = c.patch(f"/events/{eid}/lots/{lid}", json={"reserve": 50},
                headers=H(seller))
    assert r.status_code == 200 and r.json()["reserve"] == 50


def test_closing_with_no_bids_is_unsold(env):
    c, eid, _, seller, _, _, lot = env
    lid = lot(reserve=100)
    r = c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller))
    assert r.status_code == 200
    assert r.json()["winner_alias"] is None
    assert r.json()["purchase_price"] is None
    view = c.get(f"/events/{eid}/lots/{lid}").json()
    assert view["status"] == "closed" and view["current_price"] is None


def test_closing_single_bidder_charges_reserve(env):
    c, eid, _, seller, b1, _, lot = env
    lid = lot(reserve=100)
    put_budget(c, eid, b1, 900)
    post_bid(c, eid, b1, lid, 500)
    r = c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller)).json()
    assert r["purchase_price"] == 100


def test_winner_used_equals_price_loser_nothing(env):
    c, eid, _, seller, b1, b2, lot = env
    lid = lot(reserve=100)
    put_budget(c, eid, b1, 900)
    put_budget(c, eid, b2, 900)
    post_bid(c, eid, b1, lid, 600)
    post_bid(c, eid, b2, lid, 450)
    assert c.get(f"/events/{eid}/lots/{lid}").json()["current_price"] == 451
    c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller))
    assert c.get(f"/events/{eid}/budget", headers=H(b1)).json() == {
        "budget": 900, "used": 451, "reserved": 0,
        "total_committed": 451, "remaining": 449,
    }
    assert c.get(f"/events/{eid}/budget", headers=H(b2)).json() == {
        "budget": 900, "used": 0, "reserved": 0,
        "total_committed": 0, "remaining": 900,
    }


def test_public_lot_never_contains_max_or_budget(env):
    c, eid, _, _, b1, b2, lot = env
    lid = lot(reserve=10)
    put_budget(c, eid, b1, 500)
    put_budget(c, eid, b2, 500)
    post_bid(c, eid, b1, lid, 300)
    post_bid(c, eid, b2, lid, 200)
    for url in (f"/events/{eid}/lots", f"/events/{eid}/lots/{lid}",
                f"/events/{eid}"):
        text = c.get(url).text
        assert "300" not in text and "max_bid" not in text
        assert "budget" not in text
    # buyer's private budget is visible only to that buyer
    assert c.get(f"/events/{eid}/budget", headers=H(b2)).status_code == 200
    assert c.get(f"/events/{eid}/budget").status_code == 401


def test_aliases_stable_and_unique(env):
    c, eid, _, _, b1, b2, lot = env
    a, b = lot(title="A"), lot(title="B")
    put_budget(c, eid, b1, 500)
    put_budget(c, eid, b2, 500)
    post_bid(c, eid, b1, a, 100)
    post_bid(c, eid, b2, b, 100)
    alice_a = c.get(f"/events/{eid}/lots/{a}").json()["leader_alias"]
    bob_b = c.get(f"/events/{eid}/lots/{b}").json()["leader_alias"]
    assert alice_a != bob_b
    # b1 bids on B too and is still same alias
    post_bid(c, eid, b1, b, 200)
    assert c.get(f"/events/{eid}/lots/{b}").json()["leader_alias"] == alice_a


def test_my_bids_after_close_shows_won_and_lost(env):
    c, eid, _, seller, b1, b2, lot = env
    lid = lot(reserve=10)
    put_budget(c, eid, b1, 500)
    put_budget(c, eid, b2, 500)
    post_bid(c, eid, b1, lid, 300)
    post_bid(c, eid, b2, lid, 200)
    c.post(f"/events/{eid}/lots/{lid}/close", headers=H(seller))
    mine1 = c.get(f"/events/{eid}/my-bids", headers=H(b1)).json()["bids"][0]
    assert mine1["won"] is True and mine1["purchase_price"] == 201
    mine2 = c.get(f"/events/{eid}/my-bids", headers=H(b2)).json()["bids"][0]
    assert mine2["won"] is False and mine2["purchase_price"] is None


def test_after_event_close_everything_is_read_only(env):
    c, eid, admin, seller, b1, _, lot = env
    lid = lot(reserve=10)
    put_budget(c, eid, b1, 100)
    post_bid(c, eid, b1, lid, 20)
    r = c.post(f"/events/{eid}/close", headers=H(admin))
    assert r.status_code == 200
    assert c.put(f"/events/{eid}/budget", json={"budget": 50},
                 headers=H(b1)).status_code == 409
    assert c.post(f"/events/{eid}/lots", json={"title": "new", "reserve": 5},
                  headers=H(seller)).status_code == 409
    assert c.post(f"/events/{eid}/close", headers=H(admin)).status_code == 409


def test_validation_error_is_json_safe(env):
    c, eid, _, seller, b1, _, lot = env
    r = post_bid(c, eid, b1, lot(), 20)
    # invalid body first, no budget needed
    bad = c.post(f"/events/{eid}/lots/999999/bids", json={"max_bid": "10"},
                 headers=H(b1))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_request"


def test_unknown_lot_and_event_404(env):
    c, eid, _, _, b1, _, _ = env
    assert c.get("/events/999/lots").status_code == 404
    assert c.get(f"/events/{eid}/lots/999").status_code == 404


def test_admin_endpoint_rejects_buyer_and_seller(env):
    c, eid, _, seller, b1, _, _ = env
    assert c.post(f"/events/{eid}/close", headers=H(b1)).status_code == 403
    assert c.post(f"/events/{eid}/close", headers=H(seller)).status_code == 403


def test_seller_cannot_close_another_sellers_lot(env):
    c, eid, _, seller, _, _, lot = env
    s2 = c.post(f"/events/{eid}/users", json={"name": "s2", "role": "seller"}).json()
    lid = lot(reserve=10)
    r = c.post(f"/events/{eid}/lots/{lid}/close", headers=H(s2))
    assert r.status_code == 403


def test_buyer_cannot_create_lots(env):
    c, eid, _, _, b1, _, _ = env
    r = c.post(f"/events/{eid}/lots", json={"title": "x", "reserve": 5},
               headers=H(b1))
    assert r.status_code == 403


def test_tie_across_price_plus_one(env):
    """Equal maxes: price caps at the max (second+1 would exceed it)."""
    c, eid, _, _, b1, b2, lot = env
    lid = lot(reserve=10)
    put_budget(c, eid, b1, 1000)
    put_budget(c, eid, b2, 1000)
    post_bid(c, eid, b1, lid, 500)
    post_bid(c, eid, b2, lid, 500)
    view = c.get(f"/events/{eid}/lots/{lid}").json()
    assert view["current_price"] == 500
    assert view["bidder_count"] == 2


def test_three_bidders_price_from_second(env):
    c, eid, _, _, b1, b2, lot = env
    lid = lot(reserve=10)
    b3 = c.post(f"/events/{eid}/users", json={"name": "b3", "role": "buyer"}).json()
    for u, n in ((b1, 900), (b2, 400), (b3, 300)):
        c.put(f"/events/{eid}/budget", json={"budget": 1000}, headers=H(u))
        post_bid(c, eid, u, lid, n)
    assert c.get(f"/events/{eid}/lots/{lid}").json()["current_price"] == 401
