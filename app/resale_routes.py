"""HTTP routes for revoking a sale and directed re-sale ("撤销 / 补售").

One canonical REST-ish surface plus a handful of natural aliases.  Payloads
are parsed leniently (many field names for deal / buyer / price); the service
layer stays strict.

Deliberately absent: any public endpoint.  Offers and deals are only reachable
by admins, the lot's seller, or the buyers they name.  The public auction views
(``/events/{id}/lots`` etc.) keep showing the original auction result.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Depends, Request
from fastapi.responses import JSONResponse

from .errors import ApiError
from .pickups import as_int, extract_lot_ids, first_present

DEAL_REF_KEYS = (
    "deal_ref", "deal_id", "deal_no", "deal", "sale_id", "sale_no",
    "transaction_id", "transaction_no", "purchase_id",
)
OFFER_ID_KEYS = (
    "offer_id", "offer", "resale_offer_id", "resale_id", "reservation_id",
    "id",
)
BUYER_REF_KEYS = (
    "buyer_id", "buyer", "buyer_account", "buyer_account_id", "buyer_user",
    "buyer_user_id", "new_buyer_id", "new_buyer", "designated_buyer_id",
    "designated_buyer", "recipient_id", "recipient", "to_buyer_id",
    "to_buyer", "target_buyer_id", "target_buyer", "account", "user_id",
    "username", "user",
)
PRICE_KEYS = (
    "price", "offer_price", "resale_price", "amount", "agreed_price",
    "negotiated_price", "sale_price", "value", "ask_price", "ask",
)
REASON_KEYS = ("reason", "note", "memo", "comment", "revoke_reason",
               "cancellation_reason")


def _merged(payload: dict | None, request: Request) -> dict:
    data = dict(payload or {})
    for key, value in request.query_params.multi_items():
        data.setdefault(key, value)
    return data


def _deal_ref(data: dict, path_ref=None):
    if path_ref is not None:
        return path_ref
    return first_present(data, DEAL_REF_KEYS)


def _buyer_ref(data: dict):
    return first_present(data, BUYER_REF_KEYS)


def _price(data: dict):
    return first_present(data, PRICE_KEYS)


def _offer_ref(data: dict, path_ref=None):
    if path_ref is not None:
        return path_ref
    return first_present(data, OFFER_ID_KEYS)


def register_resale_routes(app, service, current_user) -> None:
    Auth = Depends(current_user)
    P = "/events/{event_id}"

    # ------------------------------------------------------------- revoke
    def revoke_endpoint(event_id: int, request: Request,
                        payload: Optional[dict] = Body(default=None),
                        user=Auth):
        data = _merged(payload, request)
        reason = first_present(data, REASON_KEYS) or ""
        body, created = service.revoke_deal(
            event_id, user["id"], _deal_ref(data), reason=str(reason))
        return JSONResponse(status_code=200, content=body)

    def revoke_path_endpoint(event_id: int, deal_ref: str, request: Request,
                             payload: Optional[dict] = Body(default=None),
                             user=Auth):
        data = _merged(payload, request)
        reason = first_present(data, REASON_KEYS) or ""
        body, _created = service.revoke_deal(
            event_id, user["id"], deal_ref, reason=str(reason))
        return JSONResponse(status_code=200, content=body)

    # -------------------------------------------------------------- offers
    def offer_endpoint(event_id: int, request: Request,
                       payload: Optional[dict] = Body(default=None),
                       user=Auth):
        data = _merged(payload, request)
        lot_ids = extract_lot_ids(data)
        lot_id = as_int(first_present(data, ("lot_id", "lotId", "lot")))
        if lot_id is None and lot_ids:
            lot_id = lot_ids[0]
        if lot_id is None:
            raise ApiError(422, "invalid_request", "A lot id is required")
        buyer_ref = _buyer_ref(data)
        if buyer_ref is None:
            raise ApiError(422, "invalid_request",
                           "A designated buyer is required")
        note = first_present(data, ("note", "memo", "comment")) or ""
        body, created = service.create_offer(
            event_id, user["id"], lot_id, buyer_ref, _price(data),
            note=str(note))
        return JSONResponse(status_code=201 if created else 200, content=body)

    def offer_path_endpoint(event_id: int, lot_id: int, request: Request,
                            payload: Optional[dict] = Body(default=None),
                            user=Auth):
        data = _merged(payload, request)
        body, created = service.create_offer(
            event_id, user["id"], lot_id, _buyer_ref(data), _price(data),
            note=str(first_present(data, ("note", "memo", "comment")) or ""))
        return JSONResponse(status_code=201 if created else 200, content=body)

    def offer_withdraw_endpoint(event_id: int, request: Request,
                                payload: Optional[dict] = Body(default=None),
                                user=Auth):
        data = _merged(payload, request)
        body, _created = service.withdraw_offer(
            event_id, user["id"], _offer_ref(data))
        return JSONResponse(status_code=200, content=body)

    def offer_withdraw_path(event_id: int, offer_ref: str, user=Auth):
        body, _created = service.withdraw_offer(
            event_id, user["id"], offer_ref)
        return JSONResponse(status_code=200, content=body)

    def offer_accept_endpoint(event_id: int, request: Request,
                              payload: Optional[dict] = Body(default=None),
                              user=Auth):
        data = _merged(payload, request)
        body, created = service.accept_offer(
            event_id, user["id"], _offer_ref(data))
        return JSONResponse(status_code=201 if created else 200, content=body)

    def offer_accept_path(event_id: int, offer_ref: str, user=Auth):
        body, created = service.accept_offer(event_id, user["id"], offer_ref)
        return JSONResponse(status_code=201 if created else 200, content=body)

    # -------------------------------------------------------------- views
    def deal_list_endpoint(event_id: int, request: Request, user=Auth):
        qp = request.query_params
        return service.list_deals(
            event_id, user["id"],
            lot_id=as_int(qp.get("lot_id") or qp.get("lot")),
            status_filter=qp.get("status") or qp.get("state"))

    def deal_detail_event(event_id: int, deal_ref: str, user=Auth):
        return service.get_deal(event_id, user["id"], deal_ref)

    def offer_list_endpoint(event_id: int, request: Request, user=Auth):
        qp = request.query_params
        return service.list_offers(
            event_id, user["id"],
            lot_id=as_int(qp.get("lot_id") or qp.get("lot")),
            status_filter=qp.get("status") or qp.get("state"))

    def offer_detail_event(event_id: int, offer_ref: str, user=Auth):
        return service.get_offer(event_id, user["id"], offer_ref)

    def lot_deals_endpoint(event_id: int, lot_id: int, user=Auth):
        return service.lot_deals_view(event_id, user["id"], lot_id)

    def deal_for_lot_endpoint(event_id: int, lot_id: int, user=Auth):
        return service.lot_deals_view(event_id, user["id"], lot_id)

    # ----------------------------------------------------------- mounting
    post = lambda path, ep, code=None: app.add_api_route(
        path, ep, methods=["POST"], tags=["resale"])
    get = lambda path, ep: app.add_api_route(
        path, ep, methods=["GET"], tags=["resale"])

    # Admin: revoke a sale (deal-scoped).
    for path in (
        f"{P}/deals/revoke",
        f"{P}/deals/revocation",
        f"{P}/deals/cancel",
        f"{P}/deals/void",
        f"{P}/sales/revoke",
        f"{P}/sales/cancel",
        f"{P}/resale/revoke",
    ):
        post(path, revoke_endpoint)
    for path in (
        f"{P}/deals/{{deal_ref}}/revoke",
        f"{P}/deals/{{deal_ref}}/cancel",
        f"{P}/sales/{{deal_ref}}/revoke",
        f"{P}/sales/{{deal_ref}}/cancel",
        f"{P}/deals/{{deal_ref}}/revocation",
    ):
        post(path, revoke_path_endpoint)

    # Admin: issue / replace a directed resale offer.
    for path in (
        f"{P}/offers",
        f"{P}/resale-offers",
        f"{P}/resale/offers",
        f"{P}/sales/offers",
        f"{P}/deals/offers",
    ):
        post(path, offer_endpoint)
        get(path, offer_list_endpoint)
    for path in (
        f"{P}/offers/create",
        f"{P}/offers/issue",
        f"{P}/resale/offer",
        f"{P}/resale-offers/create",
    ):
        post(path, offer_endpoint)
    for path in (
        f"{P}/lots/{{lot_id}}/offer",
        f"{P}/lots/{{lot_id}}/offers",
        f"{P}/lots/{{lot_id}}/resale-offer",
        f"{P}/lots/{{lot_id}}/resale",
    ):
        post(path, offer_path_endpoint)
        get(path, lot_deals_endpoint)

    # Admin: withdraw an offer.
    for path in (
        f"{P}/offers/withdraw",
        f"{P}/offers/cancel",
        f"{P}/offers/revoke",
        f"{P}/resale/withdraw",
    ):
        post(path, offer_withdraw_endpoint)
    for path in (
        f"{P}/offers/{{offer_ref}}/withdraw",
        f"{P}/offers/{{offer_ref}}/cancel",
        f"{P}/offers/{{offer_ref}}/revoke",
    ):
        post(path, offer_withdraw_path)

    # Buyer: accept a directed offer (the buyer is the caller -- never from
    # the payload, so one buyer cannot accept another buyer's offer).
    for path in (
        f"{P}/offers/accept",
        f"{P}/resale/accept",
        f"{P}/resale-offers/accept",
        f"{P}/sales/accept",
    ):
        post(path, offer_accept_endpoint)
    for path in (
        f"{P}/offers/{{offer_ref}}/accept",
        f"{P}/resale-offers/{{offer_ref}}/accept",
        f"{P}/sales/{{offer_ref}}/accept",
    ):
        post(path, offer_accept_path)

    # Views: deal history / offers, visibility-filtered by the service.
    for path in (
        f"{P}/deals",
        f"{P}/sales",
        f"{P}/resale/deals",
    ):
        get(path, deal_list_endpoint)
    for path in (
        f"{P}/deals/{{deal_ref}}",
        f"{P}/sales/{{deal_ref}}",
    ):
        get(path, deal_detail_event)
    for path in (
        f"{P}/offers/{{offer_ref}}",
        f"{P}/resale-offers/{{offer_ref}}",
    ):
        get(path, offer_detail_event)
    get(f"{P}/offers", offer_list_endpoint)

    for path in (
        f"{P}/lots/{{lot_id}}/deals",
        f"{P}/lots/{{lot_id}}/sales",
        f"{P}/lots/{{lot_id}}/deal",
    ):
        get(path, lot_deals_endpoint)
    get(f"{P}/lots/{{lot_id}}/deal-history", deal_for_lot_endpoint)


def resale_keyword_dispatch(service, event_id: int, rest: str,
                            method: str, data: dict, user: dict):
    """Best-effort handling for resale wordings missed by explicit routes."""
    segs = [s.lower() for s in (rest or "").split("/") if s]
    tokens: list[str] = []
    for s in segs:
        tokens.extend(t for t in s.replace("_", "-").split("-") if t)
    compact = "".join(tokens)
    strong = ("deal", "deals", "sale", "sales", "resale", "offer", "offers",
              "revoke", "revocation", "补售", "撤销")
    if not any(w in compact or w in tokens for w in strong):
        return None
    if method == "GET":
        if "offer" in compact:
            ref = _last_int_ref(segs)
            if ref is not None:
                return service.get_offer(event_id, user["id"], ref)
            return service.list_offers(event_id, user["id"])
        if any(w in compact for w in ("deal", "sale")):
            ref = _last_int_ref(segs)
            if ref is not None:
                return service.get_deal(event_id, user["id"], ref)
            return service.list_deals(event_id, user["id"])
        return None
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        if any(w in compact for w in ("accept", "take")):
            return service.accept_offer(
                event_id, user["id"], _offer_ref(data))[0]
        if "offer" in compact:
            if any(w in compact for w in ("withdraw", "cancel", "revoke")):
                return service.withdraw_offer(
                    event_id, user["id"], _offer_ref(data))[0]
            lot_ids = extract_lot_ids(data)
            if lot_ids:
                return service.create_offer(
                    event_id, user["id"], lot_ids[0], _buyer_ref(data),
                    _price(data))[0]
        if any(w in compact for w in ("revoke", "cancel", "void")):
            return service.revoke_deal(
                event_id, user["id"], _deal_ref(data))[0]
    return None


def _last_int_ref(segs: list[str]):
    for s in reversed(segs):
        v = as_int(s)
        if v is not None:
            return v
        if s.upper().startswith("DL-"):
            return s.upper()
    return None
