"""Pure auction rules — no database, no HTTP.

Amounts are whole integers.

* Highest max wins.  Equal max -> the buyer whose *first* valid bid on the lot
  came earlier leads.  A buyer holds one standing max per lot and can only
  raise it, so the order of bids rows is the first-effective order.
* Current price = max(reserve, second-highest max + 1), capped at the leader's
  max.  With a single bidder the price is the reserve (no second max exists);
  with no bids the lot has no price and cannot be won.
* Budgets are event-wide: a buyer's commitment for the event is
      used     = sum of prices of the lots they currently hold an *effective*
                 sale ("成交") for
      reserved = sum of their max on every open lot they currently lead
  A buyer who is not leading a lot reserves nothing there, but their bid is
  still kept because it determines that lot's price.

The immutable auction result lives on :class:`Lot` (``winner_id`` /
``purchase_price``).  When a revoked sale is resold, the *lot* keeps the
original auction result while the effective sale moves to the new buyer.  The
optional ``active_deals`` mapping (lot id -> :class:`Sale`) therefore drives
"used"; without it the legacy lot fields are used, which keeps the pure rules
backwards compatible.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Bid:
    id: int            # insertion order == first valid bid order
    buyer_id: int
    max_bid: int


@dataclass(frozen=True)
class Lot:
    id: int
    reserve: int
    status: str                       # 'open' | 'closed'
    winner_id: int | None = None
    purchase_price: int | None = None


@dataclass(frozen=True)
class Sale:
    """One effective sale of a lot (the thing that occupies budget)."""
    lot_id: int
    buyer_id: int
    price: int


@dataclass(frozen=True)
class Outcome:
    leader_id: int | None
    leader_bid_id: int | None
    leader_max: int | None
    second_max: int | None
    price: int | None
    bidders: int

    @property
    def has_leader(self) -> bool:
        return self.leader_id is not None


@dataclass(frozen=True)
class Commitment:
    used: int = 0
    reserved: int = 0

    @property
    def total(self) -> int:
        return self.used + self.reserved


def evaluate_lot(reserve: int, bids: Sequence[Bid]) -> Outcome:
    """Leader, second max and current price for a single lot."""
    if not bids:
        return Outcome(None, None, None, None, None, 0)

    ranked = sorted(bids, key=lambda b: (-b.max_bid, b.id))
    top = ranked[0]
    second = ranked[1].max_bid if len(ranked) > 1 else None

    if second is None:
        price = reserve
    else:
        price = max(reserve, second + 1)
        if price > top.max_bid:
            price = top.max_bid   # reserve above leader max makes the top bid unsellable
    return Outcome(top.buyer_id, top.id, top.max_bid, second, price, len(ranked))


def evaluate_open_lots(
    lots: Iterable[Lot],
    bids_by_lot: Mapping[int, Sequence[Bid]],
    preview_lot: int | None = None,
    preview_bids: Sequence[Bid] | None = None,
) -> dict[int, Outcome]:
    """Outcomes for open lots, optionally with one lot's bid list replaced."""
    out: dict[int, Outcome] = {}
    for lot in lots:
        if lot.status != "open":
            continue
        bids = (
            preview_bids
            if lot.id == preview_lot and preview_bids is not None
            else bids_by_lot.get(lot.id, ())
        )
        out[lot.id] = evaluate_lot(lot.reserve, bids)
    return out


def compute_commitments(
    lots: Iterable[Lot],
    bids_by_lot: Mapping[int, Sequence[Bid]],
    outcomes: Mapping[int, Outcome] | None = None,
    active_deals: Mapping[int, "Sale"] | None = None,
) -> dict[int, Commitment]:
    """Per-buyer used + reserved across the whole event.

    ``active_deals`` maps a lot id to its currently effective sale.  When it is
    supplied it is authoritative: a lot whose sale was revoked simply has no
    entry and therefore occupies nobody's budget, while a resold lot occupies
    the *new* buyer's budget.  Without it, closed lots fall back to the
    immutable auction result stored on the lots (legacy behaviour).
    """
    used: dict[int, int] = defaultdict(int)
    reserved: dict[int, int] = defaultdict(int)

    for lot in lots:
        if lot.status == "closed":
            if active_deals is not None:
                sale = active_deals.get(lot.id)
                if sale is not None:
                    used[sale.buyer_id] += sale.price
            elif lot.winner_id is not None and lot.purchase_price is not None:
                used[lot.winner_id] += lot.purchase_price
            continue
        outcome = (
            outcomes[lot.id]
            if outcomes is not None
            else evaluate_lot(lot.reserve, bids_by_lot.get(lot.id, ()))
        )
        if outcome.has_leader:
            reserved[outcome.leader_id] += outcome.leader_max

    return {
        buyer_id: Commitment(used.get(buyer_id, 0), reserved.get(buyer_id, 0))
        for buyer_id in set(used) | set(reserved)
    }


def budget_violations(
    lots: Iterable[Lot],
    bids_by_lot: Mapping[int, Sequence[Bid]],
    budgets: Mapping[int, int],
    preview_lot: int | None = None,
    preview_bids: Sequence[Bid] | None = None,
    active_deals: Mapping[int, "Sale"] | None = None,
) -> list[tuple[int, int, Commitment]]:
    """Buyers whose budget would not cover their commitments.

    Only buyers that have actually set a budget are checked.  The list is
    ordered by buyer id so error messages are deterministic.
    """
    outcomes = evaluate_open_lots(lots, bids_by_lot, preview_lot, preview_bids)
    commitments = compute_commitments(lots, bids_by_lot, outcomes, active_deals)
    bad: list[tuple[int, int, Commitment]] = []
    for buyer_id in sorted(budgets):
        budget = budgets[buyer_id]
        c = commitments.get(buyer_id) or Commitment()
        if c.total > budget:
            bad.append((buyer_id, budget, c))
    return bad
