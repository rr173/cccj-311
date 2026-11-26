"""Unit tests for the pure auction rules."""
from __future__ import annotations

from app.domain import (
    Bid,
    Lot,
    budget_violations,
    compute_commitments,
    evaluate_lot,
)


def b(i, buyer, mx):
    return Bid(id=i, buyer_id=buyer, max_bid=mx)


# --- pricing / leader -------------------------------------------------------
def test_no_bids_has_no_price():
    out = evaluate_lot(100, [])
    assert out.leader_id is None
    assert out.price is None
    assert out.bidders == 0


def test_single_bidder_pays_reserve():
    out = evaluate_lot(100, [b(1, 7, 500)])
    assert out.leader_id == 7
    assert out.leader_max == 500
    assert out.second_max is None
    assert out.price == 100


def test_second_price_plus_one_when_second_above_reserve():
    out = evaluate_lot(100, [b(1, 1, 300), b(2, 2, 200)])
    assert out.leader_id == 1
    assert out.price == 201


def test_price_floored_at_reserve():
    out = evaluate_lot(150, [b(1, 1, 400), b(2, 2, 120)])
    assert out.price == 150
    assert out.leader_id == 1


def test_price_never_exceeds_leader_max():
    out = evaluate_lot(200, [b(1, 1, 150), b(2, 2, 99)])
    assert out.price == 150
    assert out.price <= out.leader_max


def test_tie_broken_by_earlier_first_bid():
    out = evaluate_lot(10, [b(1, 1, 500), b(2, 2, 500)])
    assert out.leader_id == 1
    assert out.second_max == 500
    assert out.price == 500  # capped at leader max, not 501


def test_three_bidders_price_from_second_highest():
    out = evaluate_lot(50, [b(1, 1, 900), b(2, 2, 400), b(3, 3, 300)])
    assert out.leader_id == 1
    assert out.second_max == 400
    assert out.price == 401


def test_raising_keeps_first_valid_order_for_ties():
    # buyer 1's row id 1 predates buyer 2's row id 2: on an equal max buyer 1 wins.
    out = evaluate_lot(10, [b(1, 1, 400), b(2, 2, 400)])
    assert out.leader_id == 1


# --- budgets ----------------------------------------------------------------
def test_leader_reserves_max_loser_reserves_nothing():
    lots = [Lot(1, 100, "open"), Lot(2, 50, "open")]
    bids = {1: [b(1, 10, 300), b(2, 20, 200)], 2: [b(3, 20, 250)]}
    commits = compute_commitments(lots, bids)
    assert commits[10].reserved == 300
    assert commits[10].used == 0
    assert commits[20].reserved == 250  # only the lot they lead
    assert commits[20].total == 250


def test_closed_lots_count_purchase_price_as_used():
    lots = [
        Lot(1, 100, "closed", winner_id=10, purchase_price=180),
        Lot(2, 50, "open"),
    ]
    bids = {2: [b(1, 10, 400), b(2, 11, 50)]}
    commits = compute_commitments(lots, bids)
    assert commits[10].used == 180
    assert commits[10].reserved == 400
    assert commits[10].total == 580


def test_budget_violation_detected_with_preview():
    lots = [Lot(1, 10, "open"), Lot(2, 10, "open")]
    bids = {1: [b(1, 1, 300)], 2: [b(2, 2, 200)]}
    budgets = {1: 400, 2: 400}
    preview = [b(1, 1, 300), b(3, 1, 400)]
    bad = budget_violations(lots, bids, budgets, preview_lot=2, preview_bids=preview)
    assert bad and bad[0][0] == 1
    assert bad[0][2].total == 700


def test_preview_that_fits_has_no_violation():
    lots = [Lot(1, 10, "open"), Lot(2, 10, "open")]
    bids = {1: [b(1, 1, 100)], 2: [b(2, 2, 200)]}
    budgets = {1: 300, 2: 300}
    preview = [b(1, 1, 250)]
    assert budget_violations(lots, bids, budgets, preview_lot=1,
                             preview_bids=preview) == []


def test_outbidding_leads_to_new_leader_reserving():
    # buyer 2 overtakes buyer 1; buyer 2's budget is the one at risk.
    lots = [Lot(1, 10, "open")]
    bids = {1: [b(1, 1, 300)]}
    budgets = {1: 300, 2: 300}
    preview = [b(1, 1, 300), b(2, 2, 350)]
    bad = budget_violations(lots, bids, budgets, preview_lot=1, preview_bids=preview)
    assert [x[0] for x in bad] == [2]


def test_budgets_not_set_are_ignored():
    lots = [Lot(1, 10, "open")]
    bids = {1: [b(1, 1, 999999)]}
    assert budget_violations(lots, bids, {}) == []
