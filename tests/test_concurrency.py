"""Concurrency tests: bids and closes must behave as if fully serialised.

We drive the service from threads, and in one test from two separate
Service/Database instances on the same file, exercising SQLite's own write-lock
serialisation as well as the in-process lock.
"""
from __future__ import annotations

import threading

import pytest

from app.db import Database
from app.errors import ApiError
from app.service import ADMIN_KEY, Service


def world(tmp_path, name="c.db", buyers=2):
    db = Database(str(tmp_path / name))
    db.init()
    svc = Service(db)
    eid = svc.create_event("E")["id"]
    admin = svc.create_admin(eid, "admin", ADMIN_KEY)
    seller = svc.register(eid, "seller", "seller")
    bs = [svc.register(eid, f"b{i}", "buyer") for i in range(buyers)]
    return db, svc, eid, admin, seller, bs


def add_lot(svc, eid, seller, reserve=10, title="L"):
    return svc.create_lot(eid, seller["user_id"], title, "", reserve)["id"]


def set_budget(svc, eid, buyer, amount):
    svc.set_budget(eid, buyer["user_id"], amount)


def bid(svc, eid, buyer, lot_id, maximum):
    return svc.place_bid(eid, buyer["user_id"], lot_id, maximum)


def commitment(svc, eid, buyer):
    v = svc.budget_view(eid, buyer["user_id"])
    return v["used"] + v["reserved"]


def run_threads(fns):
    barrier = threading.Barrier(len(fns))
    results: list = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except ApiError as e:
            results[i] = ("err", e.code)
        except Exception as e:  # pragma: no cover - unexpected
            results[i] = ("boom", repr(e))

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(r[0] == "boom" for r in results), results
    return results


def test_concurrent_bids_from_one_buyer_cannot_jointly_overcommit(tmp_path):
    _, svc, eid, _, _, bs = world(tmp_path, buyers=1)
    b = bs[0]
    seller = svc.register(eid, "s2", "seller")
    set_budget(svc, eid, b, 1000)
    lot_a = add_lot(svc, eid, seller, title="A")
    lot_b = add_lot(svc, eid, seller, title="B")

    # Four simultaneous 800-maxes on two lots: at most one can fit in 1000.
    results = run_threads([
        lambda: bid(svc, eid, b, lot_a, 800),
        lambda: bid(svc, eid, b, lot_b, 800),
        lambda: bid(svc, eid, b, lot_a, 800),
        lambda: bid(svc, eid, b, lot_b, 800),
    ])
    accepted = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "err"]
    assert len(accepted) == 1, results
    assert all(r[1] in {"must_raise", "budget_exceeded"} for r in rejected)
    assert commitment(svc, eid, b) == 800


def test_concurrent_raises_serialise_and_keep_the_highest(tmp_path):
    _, svc, eid, _, _, bs = world(tmp_path, buyers=1)
    b = bs[0]
    seller = svc.register(eid, "s2", "seller")
    lot = add_lot(svc, eid, seller)
    set_budget(svc, eid, b, 5000)
    bid(svc, eid, b, lot, 100)

    run_threads([
        lambda: bid(svc, eid, b, lot, 500),
        lambda: bid(svc, eid, b, lot, 600),
        lambda: bid(svc, eid, b, lot, 550),
    ])
    # Serialised raises: the standing max is the largest one applied.
    assert commitment(svc, eid, b) == 600
    assert svc.get_lot(eid, lot)["current_price"] == 10  # single bidder


def test_many_buyers_many_lots_invariant_holds(tmp_path):
    _, svc, eid, _, _, bs = world(tmp_path, buyers=6)
    seller = svc.register(eid, "s2", "seller")
    lots = [add_lot(svc, eid, seller, title=f"L{i}") for i in range(4)]
    for b in bs:
        set_budget(svc, eid, b, 3000)

    # 4 lots x 400 = 1600 <= 3000: every bid must be accepted.
    first = [lambda b=b, lot=lot: bid(svc, eid, b, lot, 400)
             for b in bs for lot in lots]
    assert all(r[0] == "ok" for r in run_threads(first))

    # Everyone then tries to raise every lot to 1100 (4 x 1100 = 4400 > 3000).
    # Some raises will be refused; whatever the interleaving, no budget is
    # ever exceeded and no public view is torn.
    second = [lambda b=b, lot=lot: bid(svc, eid, b, lot, 1100)
              for b in bs for lot in lots]
    run_threads(second)

    for b in bs:
        assert commitment(svc, eid, b) <= 3000
    for lot in lots:
        view = svc.get_lot(eid, lot)
        assert view["current_price"] is not None
        assert view["leader_alias"] is not None


def test_two_service_instances_share_one_file_safely(tmp_path):
    """Emulates two processes: independent locks, same SQLite file."""
    db1, svc1, eid, _, _, bs = world(tmp_path, buyers=1)
    db2 = Database(str(db1.path))
    db2.init()
    svc2 = Service(db2)
    b = bs[0]
    set_budget(svc1, eid, b, 1000)
    seller = svc1.register(eid, "s2", "seller")
    lot_a = add_lot(svc1, eid, seller, title="A")
    lot_b = add_lot(svc1, eid, seller, title="B")

    results = run_threads([
        lambda: svc1.place_bid(eid, b["user_id"], lot_a, 800),
        lambda: svc2.place_bid(eid, b["user_id"], lot_b, 800),
    ])
    assert sum(1 for r in results if r[0] == "ok") == 1, results
    assert commitment(svc1, eid, b) == 800


@pytest.mark.parametrize("repetition", range(20))
def test_close_and_bid_race_is_consistent(tmp_path, repetition):
    """Either the bid lands before the close (and sets the price) or it is
    rejected because the lot is closed — never a torn state."""
    _, svc, eid, _, _, bs = world(tmp_path, name=f"race{repetition}.db", buyers=2)
    seller = svc.register(eid, "s", "seller")
    lot = add_lot(svc, eid, seller, reserve=100, title="X")
    b1, b2 = bs
    set_budget(svc, eid, b1, 2000)
    set_budget(svc, eid, b2, 2000)
    bid(svc, eid, b1, lot, 300)  # leader at 300, price 100

    results = run_threads([
        lambda: svc.close_lot(eid, seller["user_id"], lot),
        lambda: bid(svc, eid, b2, lot, 400),
    ])
    close_res = [r for r in results if r[0] == "ok" and isinstance(r[1], dict)][0][1]
    bid_res = results[1]
    price = close_res["purchase_price"]
    assert price is not None

    if bid_res[0] == "ok":
        # b2's bid committed before the close: second max 300 -> price 301.
        assert price == 301, results
        assert close_res["winner_alias"] is not None
    else:
        assert bid_res[1] == "lot_closed", results
        assert price == 100, results

    view = svc.get_lot(eid, lot)
    assert view["status"] == "closed" and view["current_price"] == price
    for b in bs:
        assert commitment(svc, eid, b) <= 2000


def test_concurrent_closes_of_several_lots_never_overcharge(tmp_path):
    _, svc, eid, _, _, bs = world(tmp_path, buyers=3)
    seller = svc.register(eid, "s", "seller")
    lots = [add_lot(svc, eid, seller, reserve=100, title=f"L{i}") for i in range(6)]
    for b in bs:
        set_budget(svc, eid, b, 100_000)
    for i, lot in enumerate(lots):
        bid(svc, eid, bs[i % 3], lot, 500)
        bid(svc, eid, bs[(i + 1) % 3], lot, 700)

    results = run_threads([
        lambda lot=lot: svc.close_lot(eid, seller["user_id"], lot) for lot in lots
    ])
    assert all(r[0] == "ok" for r in results), results
    for r in results:
        assert r[1]["purchase_price"] == 501  # second max 500 + 1

    total_used = 0
    for b in bs:
        v = svc.budget_view(eid, b["user_id"])
        assert v["reserved"] == 0
        total_used += v["used"]
    assert total_used == 6 * 501
