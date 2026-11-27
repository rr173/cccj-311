"""Real HTTP parallelism for the delivery vs authorisation-change race."""
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
    app = create_app(str(tmp_path / "pickup-http.db"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError("uvicorn did not start")
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=10)


def H(token):
    return {"Authorization": f"Bearer {token}"}


def test_http_delivery_vs_revoke_race(server):
    c = httpx.Client(base_url=server, timeout=30)
    eid = c.post("/events", json={"name": "E"}).json()["id"]
    seller = c.post(f"/events/{eid}/users",
                    json={"name": "s", "role": "seller"}).json()
    buyer = c.post(f"/events/{eid}/users",
                   json={"name": "b", "role": "buyer"}).json()
    carrier = c.post(f"/events/{eid}/users",
                     json={"name": "c", "role": "carrier"}).json()
    c.put(f"/events/{eid}/budget", json={"budget": 10000},
          headers=H(buyer["token"]))

    outcomes = []
    for round_no in range(8):
        lot = c.post(f"/events/{eid}/lots",
                     json={"title": f"L{round_no}", "reserve": 10},
                     headers=H(seller["token"])).json()["id"]
        c.post(f"/events/{eid}/lots/{lot}/bids", json={"max_bid": 500},
               headers=H(buyer["token"]))
        c.post(f"/events/{eid}/lots/{lot}/close", headers=H(seller["token"]))
        c.post(f"/events/{eid}/pickups/delegate",
               json={"lot_ids": [lot], "carrier_id": carrier["user_id"]},
               headers=H(buyer["token"]))
        cred = c.get(f"/events/{eid}/lots/{lot}/credential",
                     headers=H(carrier["token"])).json()["credential"]

        barrier = threading.Barrier(2)
        codes = {}

        def deliver():
            barrier.wait()
            with httpx.Client(base_url=server, timeout=30) as cc:
                r = cc.post(f"/events/{eid}/deliver", json={
                    "request_id": f"race-{round_no}",
                    "picker_id": carrier["user_id"],
                    "items": [{"lot_id": lot, "credential": cred}],
                }, headers=H(seller["token"]))
                codes["deliver"] = r.status_code
                codes["deliver_body"] = r.json() if r.headers.get(
                    "content-type", "").startswith("application/json") else {}

        def revoke():
            barrier.wait()
            with httpx.Client(base_url=server, timeout=30) as cc:
                r = cc.post(f"/events/{eid}/pickups/revoke",
                            json={"lot_ids": [lot]},
                            headers=H(buyer["token"]))
                codes["revoke"] = r.status_code

        ts = [threading.Thread(target=deliver), threading.Thread(target=revoke)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)

        assert codes["deliver"] < 500, codes
        assert codes["revoke"] < 500, codes
        d_ok = codes["deliver"] == 200
        r_ok = codes["revoke"] == 200
        # Serialization means exactly one of the two commits wins.
        assert d_ok != r_ok, codes
        state = c.get(f"/events/{eid}/lots/{lot}/pickup",
                      headers=H(buyer["token"])).json()
        if d_ok:
            assert state["delivered"] is True
        else:
            assert state["delivered"] is False
            assert state["authorized_picker_id"] == buyer["user_id"]
        outcomes.append((d_ok, r_ok))

    # Across rounds both orderings should be plausible; at minimum the loop ran.
    assert len(outcomes) == 8
