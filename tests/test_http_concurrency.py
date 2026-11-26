"""HTTP-level concurrency: a real uvicorn server, real parallel requests."""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.main import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    port = free_port()
    app = create_app(str(tmp_path / "http.db"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("uvicorn did not start")
    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        srv.should_exit = True
        thread.join(timeout=10)


def H(token):
    return {"Authorization": f"Bearer {token}"}


def setup(base):
    c = httpx.Client(base_url=base, timeout=30)
    eid = c.post("/events", json={"name": "E"}).json()["id"]
    admin = c.post(f"/events/{eid}/admin",
                   json={"name": "a", "admin_key": "dev-admin-key"}).json()
    seller = c.post(f"/events/{eid}/users",
                    json={"name": "s", "role": "seller"}).json()
    buyers = [c.post(f"/events/{eid}/users",
                     json={"name": f"b{i}", "role": "buyer"}).json()
              for i in range(3)]
    return c, eid, admin, seller, buyers


def run_parallel(base, fns):
    barrier = threading.Barrier(len(fns))
    out: list = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        with httpx.Client(base_url=base, timeout=30) as c:
            out[i] = fn(c)

    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def test_http_concurrent_bids_from_one_buyer_cannot_overcommit(server):
    c, eid, _, seller, buyers = setup(server)
    b = buyers[0]
    lots = [c.post(f"/events/{eid}/lots", json={"title": f"L{i}", "reserve": 10},
                   headers=H(seller["token"])).json()["id"] for i in range(3)]
    c.put(f"/events/{eid}/budget", json={"budget": 1000}, headers=H(b["token"]))

    fns = []
    fns = [
        (lambda cc, lot=lot: cc.post(
            f"/events/{eid}/lots/{lot}/bids", json={"max_bid": 600},
            headers=H(b["token"])))
        for _ in range(6) for lot in lots
    ]
    results = run_parallel(server, fns)
    ok = [r for r in results if r.status_code == 201]
    bad = [r for r in results if r.status_code != 201]
    assert len(ok) == 1
    assert all(r.status_code in (409, 422) for r in bad), [r.text for r in bad[:3]]
    budget = c.get(f"/events/{eid}/budget", headers=H(b["token"])).json()
    assert budget["reserved"] == 600 and budget["remaining"] == 400


def test_http_concurrent_bids_from_many_buyers_keep_invariants(server):
    c, eid, _, seller, buyers = setup(server)
    lots = [c.post(f"/events/{eid}/lots", json={"title": f"L{i}", "reserve": 10},
                   headers=H(seller["token"])).json()["id"] for i in range(4)]
    for b in buyers:
        c.put(f"/events/{eid}/budget", json={"budget": 2000}, headers=H(b["token"]))

    fns = [(lambda cc, b=b, lot=lot: cc.post(
        f"/events/{eid}/lots/{lot}/bids", json={"max_bid": 400},
        headers=H(b["token"])))
           for b in buyers for lot in lots]
    results = run_parallel(server, fns)
    assert all(r.status_code == 201 for r in results), [r.text for r in results]

    # 12 lots*4 = 1600 <= 2000, all fit.  Now push raises to 900 each:
    # 4 lots x 900 = 3600 > 2000, so some must be refused.
    fns = [(lambda cc, b=b, lot=lot: cc.post(
        f"/events/{eid}/lots/{lot}/bids", json={"max_bid": 900},
        headers=H(b["token"])))
           for b in buyers for lot in lots]
    results = run_parallel(server, fns)
    assert not any(r.status_code >= 500 for r in results), [r.text for r in results]
    for b in buyers:
        budget = c.get(f"/events/{eid}/budget", headers=H(b["token"])).json()
        assert budget["used"] + budget["reserved"] <= 2000
    for lot in lots:
        view = c.get(f"/events/{eid}/lots/{lot}").json()
        assert view["leader_alias"] is not None
        assert view["current_price"] is not None


def test_http_close_racing_bid(server):
    c, eid, _, seller, buyers = setup(server)
    b1, b2 = buyers[0], buyers[1]
    for b in (b1, b2):
        c.put(f"/events/{eid}/budget", json={"budget": 1000}, headers=H(b["token"]))
    lot = c.post(f"/events/{eid}/lots", json={"title": "X", "reserve": 100},
                 headers=H(seller["token"])).json()["id"]
    c.post(f"/events/{eid}/lots/{lot}/bids", json={"max_bid": 300},
           headers=H(b1["token"]))

    results = run_parallel(server, [
        lambda cc: cc.post(f"/events/{eid}/lots/{lot}/close",
                           headers=H(seller["token"])),
        lambda cc: cc.post(f"/events/{eid}/lots/{lot}/bids",
                           json={"max_bid": 400}, headers=H(b2["token"])),
    ])
    close, place = results
    assert close.status_code == 200
    price = close.json()["purchase_price"]
    if place.status_code == 201:
        assert price == 301
    else:
        assert place.status_code == 409
        assert price == 100

    view = c.get(f"/events/{eid}/lots/{lot}").json()
    assert view["status"] == "closed" and view["current_price"] == price
