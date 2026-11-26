"""API request/response schemas."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PublicRole = Literal["buyer", "seller"]
Role = Literal["buyer", "seller", "admin"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests ---------------------------------------------------------------
class CreateEvent(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class RegisterUser(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    role: PublicRole


class CreateAdmin(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    admin_key: str = Field(min_length=1, max_length=200)


class SetBudget(StrictModel):
    budget: int = Field(ge=0, strict=True)


class CreateLot(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    reserve: int = Field(ge=1, strict=True)


class UpdateLot(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    reserve: Optional[int] = Field(default=None, ge=1, strict=True)


class PlaceBid(StrictModel):
    max_bid: int = Field(ge=1, strict=True)


# --- responses --------------------------------------------------------------
class EventOut(BaseModel):
    id: int
    name: str
    status: str
    created_at: str


class AuthOut(BaseModel):
    user_id: int
    event_id: int
    name: str
    role: Role
    token: str


class LotOut(BaseModel):
    """Public lot view: never carries max bids or budgets."""
    id: int
    event_id: int
    title: str
    description: str
    reserve: int
    status: str
    current_price: Optional[int]
    leader_alias: Optional[str]
    bidder_count: int
    created_at: str


class LotListOut(BaseModel):
    event: EventOut
    lots: list[LotOut]


class BidReceiptOut(BaseModel):
    bid_id: int
    lot_id: int
    max_bid: int
    current_price: int
    leading: bool


class CloseOut(BaseModel):
    lot_id: int
    status: str
    winner_alias: Optional[str]
    purchase_price: Optional[int]


class EventCloseOut(BaseModel):
    event: EventOut
    closed_lots: list[CloseOut]


class BudgetOut(BaseModel):
    budget: Optional[int]
    used: int
    reserved: int
    total_committed: int
    remaining: Optional[int]


class MyBidOut(BaseModel):
    lot_id: int
    lot_title: str
    max_bid: int
    lot_status: str
    leading: bool
    won: bool
    purchase_price: Optional[int]
    submitted_at: str
    updated_at: str


class MyBidsOut(BaseModel):
    bids: list[MyBidOut]


class MeOut(BaseModel):
    user_id: int
    event_id: int
    name: str
    role: Role
