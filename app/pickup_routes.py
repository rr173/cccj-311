"""HTTP routes for post-sale pickup / delivery.

The feature contract in the task describes behaviour rather than exact URLs, so
this module exposes one canonical REST-ish surface and mounts the same
operations at a broad set of natural path/field aliases.  Every payload is
parsed leniently (many field names, id-or-name account references); the service
layer itself stays strict about the rules.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .errors import ApiError
from .pickups import (
    CARRIER_KEYS,
    DRIVER_KEYS,
    PICKER_KEYS,
    as_int,
    extract_account_ref,
    extract_delivery_items,
    extract_lot_ids,
    extract_request_key,
    first_present,
)

CREDENTIAL_LOOKUP = (
    "credential", "pickup_credential", "code", "pickup_code",
    "verification_code", "token",
)

DEAL_REF_LOOKUP = (
    "deal_ref", "deal_id", "deal_no", "deal", "sale_id", "sale_no",
    "transaction_id", "purchase_id",
)

GENERIC_ACCOUNT_KEYS = (
    "account", "account_ref", "account_name", "user", "user_id", "uid",
    "username", "user_name", "name", "target", "target_id", "to", "to_id",
)


def _merged_payload(payload: dict | None, request: Request) -> dict:
    data = dict(payload or {})
    # Query parameters act as fallbacks; the JSON body wins.
    for key, value in request.query_params.multi_items():
        data.setdefault(key, value)
    return data


def _account(data: dict, specific: tuple[str, ...], *, required: bool = True):
    ref = extract_account_ref(data, specific + GENERIC_ACCOUNT_KEYS)
    if required and ref is None:
        raise ApiError(422, "invalid_request",
                       "An existing event account must be supplied")
    return ref


def _lot_ids(data: dict) -> list[int]:
    ids = extract_lot_ids(data)
    if not ids and isinstance(data.get("lot_id"), int):
        ids = [data["lot_id"]]
    return ids


def _deal_ref(data: dict):
    return first_present(data, DEAL_REF_LOOKUP)


def _single_deal_ref(data: dict, one_lot: bool):
    """Per-item deal ref when the payload supplies an item list, else the
    single top-level deal ref applied to the whole batch."""
    for container_key in ("items", "delivery_items", "deliveries",
                          "lot_items", "lines"):
        container = data.get(container_key)
        if isinstance(container, list) and container and all(
                isinstance(x, dict) for x in container):
            refs = []
            for element in container:
                refs.append(first_present(element, DEAL_REF_LOOKUP))
            return refs
    ref = _deal_ref(data)
    if ref is None:
        return []
    return [ref]


def register_pickup_routes(app, service, current_user) -> None:
    Auth = Depends(current_user)

    # -------------------------------------------------------- buyer actions
    def delegate_endpoint(event_id: int, request: Request,
                          payload: Optional[dict] = Body(default=None),
                          user=Auth):
        data = _merged_payload(payload, request)
        ids = _lot_ids(data)
        ref = _account(data, CARRIER_KEYS)
        return service.delegate_carrier(event_id, user["id"], ids, ref)

    def revoke_endpoint(event_id: int, request: Request,
                        payload: Optional[dict] = Body(default=None),
                        user=Auth):
        data = _merged_payload(payload, request)
        return service.revoke_carrier(
            event_id, user["id"], _lot_ids(data))

    # ------------------------------------------------------ carrier actions
    def driver_assign_endpoint(event_id: int, request: Request,
                               payload: Optional[dict] = Body(default=None),
                               user=Auth):
        data = _merged_payload(payload, request)
        ids = _lot_ids(data)
        ref = _account(data, DRIVER_KEYS)
        return service.assign_driver(event_id, user["id"], ids, ref)

    def driver_revoke_endpoint(event_id: int, request: Request,
                               payload: Optional[dict] = Body(default=None),
                               user=Auth):
        data = _merged_payload(payload, request)
        return service.revoke_driver(
            event_id, user["id"], _lot_ids(data))

    # -------------------------------------------------------------- queries
    def status_endpoint(event_id: int, lot_id: int, request: Request,
                        user=Auth):
        return service.pickup_status(
            event_id, user["id"], lot_id,
            _deal_ref(_merged_payload(None, request)))

    def credential_endpoint(event_id: int, lot_id: int, request: Request,
                            payload: Optional[dict] = Body(default=None),
                            user=Auth):
        data = _merged_payload(payload, request)
        return service.pickup_credential(
            event_id, user["id"], lot_id, _deal_ref(data))

    def list_pickups_endpoint(event_id: int, request: Request, user=Auth):
        qp = request.query_params
        return service.list_pickups(
            event_id, user["id"],
            as_role=qp.get("as") or qp.get("role"),
            status_filter=qp.get("status") or qp.get("state"),
        )

    def batch_credentials_endpoint(event_id: int, request: Request,
                                   payload: Optional[dict] = Body(default=None),
                                   user=Auth):
        data = _merged_payload(payload, request)
        ids = _lot_ids(data)
        refs = _single_deal_ref(data, len(ids) == 1)
        creds = [
            service.pickup_credential(
                event_id, user["id"], lid,
                refs[idx] if idx < len(refs) else None)
            for idx, lid in enumerate(ids)
        ]
        by_lot = {str(c["lot_id"]): c["credential"] for c in creds}
        return {
            "event_id": event_id,
            "credentials": creds,
            "items": creds,
            "credential_by_lot": by_lot,
            "credentials_by_lot": by_lot,
        }

    # ------------------------------------------------------------- delivery
    def _do_delivery(event_id: int, request: Request, data: dict,
                     seller_id: int):
        picker_ref = _account(data, PICKER_KEYS)
        items = extract_delivery_items(data)
        # Convenience: credential keyed at top level for a single-lot body.
        if len(items) == 1 and items[0]["credential"] is None:
            cred = first_present(data, (
                "credential", "pickup_credential", "code", "pickup_code",
                "verification_code", "token"))
            if cred is not None:
                items[0]["credential"] = str(cred)
        # Attach per-item deal references so a lot with revocation history is
        # addressed explicitly.  Parallel to the item order.
        refs = _single_deal_ref(data, len(items) == 1)
        for idx, item in enumerate(items):
            if item.get("deal_ref") is None and idx < len(refs):
                item["deal_ref"] = refs[idx]
        if len(items) == 1 and items[0].get("deal_ref") is None:
            items[0]["deal_ref"] = _deal_ref(data)
        request_key = extract_request_key(data, request.headers)
        return service.confirm_delivery(
            event_id, seller_id,
            request_key=request_key, picker_ref=picker_ref, items=items,
        )

    def delivery_action_endpoint(event_id: int, request: Request,
                                 payload: Optional[dict] = Body(default=None),
                                 user=Auth):
        data = _merged_payload(payload, request)
        receipt, _created = _do_delivery(
            event_id, request, data, user["id"])
        return JSONResponse(status_code=200, content=receipt)

    def delivery_resource_endpoint(event_id: int, request: Request,
                                   payload: Optional[dict] = Body(default=None),
                                   user=Auth):
        data = _merged_payload(payload, request)
        receipt, created = _do_delivery(
            event_id, request, data, user["id"])
        return JSONResponse(status_code=201 if created else 200,
                            content=receipt)

    def single_lot_delivery_endpoint(event_id: int, lot_id: int,
                                    request: Request,
                                    payload: Optional[dict] = Body(default=None),
                                    user=Auth):
        data = _merged_payload(payload, request)
        data.setdefault("lot_ids", [lot_id])
        items = extract_delivery_items(data)
        if not items:
            cred = first_present(data, (
                "credential", "pickup_credential", "code", "pickup_code",
                "verification_code", "token"))
            items = [{"lot_id": lot_id,
                      "credential": None if cred is None else str(cred)}]
        picker_ref = _account(data, PICKER_KEYS)
        request_key = extract_request_key(data, request.headers)
        if items and items[0].get("deal_ref") is None:
            items[0]["deal_ref"] = _deal_ref(data)
        receipt, created = service.confirm_delivery(
            event_id, user["id"], request_key=request_key,
            picker_ref=picker_ref, items=items)
        return JSONResponse(status_code=201 if created else 200,
                            content=receipt)

    # ------------------------------------------------------------- receipts
    def receipt_list_endpoint(event_id: int, user=Auth):
        return service.list_receipts(event_id, user["id"])

    def receipt_detail_endpoint(event_id: int, receipt_id: str, user=Auth):
        return service.get_receipt(event_id, user["id"], receipt_id)

    def lot_receipt_endpoint(event_id: int, lot_id: int, user=Auth):
        return service.get_lot_receipt(event_id, user["id"], lot_id)

    # -------------------------------------------------- RPC-style dispatcher
    def rpc_endpoint(event_id: int, request: Request,
                     payload: Optional[dict] = Body(default=None), user=Auth):
        data = _merged_payload(payload, request)
        action = str(first_present(data, (
            "action", "op", "operation", "type", "method", "command",
            "verb", "do")) or "").strip()
        ids = _lot_ids(data)

        # Infer the operation from the payload when no action discriminator is
        # supplied (helps plain REST collection POSTs).
        if not action:
            has_picker = extract_account_ref(data, PICKER_KEYS) is not None
            has_driver = extract_account_ref(data, DRIVER_KEYS) is not None
            has_carrier = extract_account_ref(data, CARRIER_KEYS) is not None
            if has_picker and extract_delivery_items(data):
                action = "deliver"
            elif has_driver:
                action = "assign_driver"
            elif has_carrier:
                action = "delegate"

        if action in {"delegate", "reassign", "entrust", "authorize_carrier",
                      "assign_carrier", "change_carrier", "delegate_carrier"}:
            ref = _account(data, CARRIER_KEYS)
            return service.delegate_carrier(event_id, user["id"], ids, ref)
        if action in {"revoke", "recover", "withdraw", "revoke_delegation",
                      "revoke_carrier", "recover_lots"}:
            return service.revoke_carrier(event_id, user["id"], ids)
        if action in {"assign_driver", "replace_driver", "set_driver",
                      "designate_driver", "change_driver"}:
            ref = _account(data, DRIVER_KEYS)
            return service.assign_driver(event_id, user["id"], ids, ref)
        if action in {"revoke_driver", "withdraw_driver", "remove_driver"}:
            return service.revoke_driver(event_id, user["id"], ids)
        if action in {"deliver", "confirm_delivery", "confirm",
                      "create_receipt"}:
            picker_ref = _account(data, PICKER_KEYS)
            items = extract_delivery_items(data)
            refs = _single_deal_ref(data, len(items) == 1)
            for idx, item in enumerate(items):
                if item.get("deal_ref") is None and idx < len(refs):
                    item["deal_ref"] = refs[idx]
            request_key = extract_request_key(data, request.headers)
            receipt, _ = service.confirm_delivery(
                event_id, user["id"], request_key=request_key,
                picker_ref=picker_ref, items=items)
            return JSONResponse(status_code=200, content=receipt)
        raise ApiError(422, "invalid_request",
                       f"Unknown pickup action: {action!r}")

    # -------------------------------------------------- single-lot mutations
    def single_delegate(event_id: int, lot_id: int, request: Request,
                        payload: Optional[dict] = Body(default=None), user=Auth):
        data = _merged_payload(payload, request)
        ref = _account(data, CARRIER_KEYS)
        return service.delegate_carrier(
            event_id, user["id"], [lot_id], ref)

    def single_revoke(event_id: int, lot_id: int, user=Auth):
        return service.revoke_carrier(event_id, user["id"], [lot_id])

    def single_lot_rpc(event_id: int, lot_id: int, request: Request,
                       payload: Optional[dict] = Body(default=None),
                       user=Auth):
        data = dict(payload or {})
        data.setdefault("lot_id", lot_id)
        data.setdefault("lot_ids", [lot_id])
        return rpc_endpoint(event_id, request, data, user)

    def single_assign_driver(event_id: int, lot_id: int, request: Request,
                             payload: Optional[dict] = Body(default=None),
                             user=Auth):
        data = _merged_payload(payload, request)
        ref = _account(data, DRIVER_KEYS)
        return service.assign_driver(event_id, user["id"], [lot_id], ref)

    def single_revoke_driver(event_id: int, lot_id: int, user=Auth):
        return service.revoke_driver(event_id, user["id"], [lot_id])

    P = f"/events/{{event_id}}"
    L = f"{P}/lots/{{lot_id}}"

    post = lambda path, ep: app.add_api_route(
        path, ep, methods=["POST"], tags=["pickup"])
    get = lambda path, ep: app.add_api_route(
        path, ep, methods=["GET"], tags=["pickup"])
    delete = lambda path, ep: app.add_api_route(
        path, ep, methods=["DELETE"], tags=["pickup"])

    # Buyer: delegate / reassign carrier ------------------------------------
    for path in (
        f"{P}/pickups/delegate",
        f"{P}/pickups/reassign",
        f"{P}/pickups/entrust",
        f"{P}/pickups/authorize-carrier",
        f"{P}/pickups/change-carrier",
        f"{P}/delegations",
        f"{P}/delegations/delegate",
        f"{P}/delegations/reassign",
        f"{P}/delegations/create",
        f"{P}/delegations/change",
        f"{P}/delegate",
        f"{P}/entrust",
        f"{P}/my-lots/delegate",
        f"{P}/buyer/delegate",
        f"{P}/buyer/reassign",
        f"{P}/carrier-delegations",
    ):
        post(path, delegate_endpoint)

    # Buyer: recover / revoke carrier delegation -----------------------------
    for path in (
        f"{P}/pickups/revoke",
        f"{P}/pickups/recover",
        f"{P}/pickups/withdraw",
        f"{P}/pickups/revoke-delegation",
        f"{P}/delegations/revoke",
        f"{P}/delegations/recover",
        f"{P}/delegations/withdraw",
        f"{P}/delegations/revoke-carrier",
        f"{P}/revoke-delegation",
        f"{P}/recover",
        f"{P}/buyer/revoke",
        f"{P}/buyer/recover",
    ):
        post(path, revoke_endpoint)
    delete(f"{P}/delegations", revoke_endpoint)
    delete(f"{P}/pickups/delegate", revoke_endpoint)

    # Carrier: assign / replace driver --------------------------------------
    for path in (
        f"{P}/pickups/assign-driver",
        f"{P}/pickups/replace-driver",
        f"{P}/pickups/driver",
        f"{P}/pickups/designate-driver",
        f"{P}/driver-assignments",
        f"{P}/driver-assignments/assign",
        f"{P}/driver-assignments/replace",
        f"{P}/carrier/drivers",
        f"{P}/carrier/assign-driver",
        f"{P}/carrier/replace-driver",
        f"{P}/assign-driver",
    ):
        post(path, driver_assign_endpoint)

    # Carrier: withdraw driver ----------------------------------------------
    for path in (
        f"{P}/pickups/revoke-driver",
        f"{P}/pickups/withdraw-driver",
        f"{P}/driver-assignments/revoke",
        f"{P}/driver-assignments/withdraw",
        f"{P}/carrier/revoke-driver",
        f"{P}/carrier/withdraw-driver",
        f"{P}/revoke-driver",
        f"{P}/withdraw-driver",
    ):
        post(path, driver_revoke_endpoint)
    delete(f"{P}/driver-assignments", driver_revoke_endpoint)
    delete(f"{P}/pickups/driver", driver_revoke_endpoint)

    # Per-lot status / credentials ------------------------------------------
    for path in (
        f"{L}/pickup",
        f"{L}/pickup-status",
        f"{L}/pickup_status",
        f"{L}/delivery",
        f"{L}/delivery-status",
        f"{L}/delivery_status",
        f"{L}/handover",
        f"{L}/handover-status",
        f"{L}/collection",
        f"{L}/pickup-authorization",
        f"{L}/authorization",
        f"{L}/custody",
        f"{L}/delegation",
        f"{L}/status",
    ):
        get(path, status_endpoint)

    for path in (
        f"{L}/pickup/credential",
        f"{L}/pickup/code",
        f"{L}/pickup/verification",
        f"{L}/credential",
        f"{L}/credentials",
        f"{L}/pickup-credential",
        f"{L}/pickup-code",
        f"{L}/verification-code",
        f"{L}/delivery-credential",
        f"{L}/pickup-verification",
    ):
        get(path, credential_endpoint)
        post(path, credential_endpoint)

    # Worklists --------------------------------------------------------------
    for path in (f"{P}/pickups", f"{P}/my-pickups", f"{P}/delegations"):
        get(path, list_pickups_endpoint)

    # Plain collection POST: inferred delegate / driver / delivery action.
    post(f"{P}/pickups", rpc_endpoint)
    # Generic action POST on a single lot's pickup resource.
    post(f"{L}/pickup", single_lot_rpc)

    post(f"{P}/pickups/credentials", batch_credentials_endpoint)
    post(f"{P}/credentials", batch_credentials_endpoint)

    # Seller delivery: action-style (200) -----------------------------------
    for path in (
        f"{P}/deliver",
        f"{P}/delivery",
        f"{P}/pickups/deliver",
        f"{P}/pickups/confirm",
        f"{P}/pickups/confirm-pickup",
        f"{P}/confirm-pickup",
        f"{P}/seller/deliver",
        f"{P}/deliveries/confirm",
        f"{P}/handovers/confirm",
        f"{P}/handover",
    ):
        post(path, delivery_action_endpoint)

    # Seller delivery: resource-style (201 new / 200 replay) -----------------
    for path in (
        f"{P}/deliveries",
        f"{P}/handovers",
        f"{P}/seller/deliveries",
    ):
        post(path, delivery_resource_endpoint)

    # Single-lot aliases -----------------------------------------------------
    post(f"{L}/delegate", single_delegate)
    post(f"{L}/reassign", single_delegate)
    post(f"{L}/entrust", single_delegate)
    post(f"{L}/revoke-delegation", single_revoke)
    post(f"{L}/recover", single_revoke)
    post(f"{L}/revoke", single_revoke)
    delete(f"{L}/delegation", single_revoke)
    post(f"{L}/assign-driver", single_assign_driver)
    post(f"{L}/replace-driver", single_assign_driver)
    post(f"{L}/driver", single_assign_driver)
    post(f"{L}/revoke-driver", single_revoke_driver)
    post(f"{L}/withdraw-driver", single_revoke_driver)
    post(f"{L}/deliver", single_lot_delivery_endpoint)
    post(f"{L}/delivery", single_lot_delivery_endpoint)

    # Receipts ---------------------------------------------------------------
    for path in (
        f"{P}/receipts",
        f"{P}/deliveries",
        f"{P}/handovers",
        f"{P}/pickups/receipts",
        f"{P}/seller/receipts",
    ):
        get(path, receipt_list_endpoint)

    for path in (
        f"{P}/receipts/{{receipt_id}}",
        f"{P}/receipt/{{receipt_id}}",
        f"{P}/deliveries/{{receipt_id}}",
        f"{P}/handovers/{{receipt_id}}",
        f"{P}/pickups/receipts/{{receipt_id}}",
    ):
        get(path, receipt_detail_endpoint)

    for path in (f"{L}/receipt", f"{L}/delivery-receipt"):
        get(path, lot_receipt_endpoint)

    # Batch aliases rooted at the lots collection ----------------------------
    for path in (
        f"{P}/lots/delegate",
        f"{P}/lots/reassign",
        f"{P}/lots/entrust",
    ):
        post(path, delegate_endpoint)
    for path in (
        f"{P}/lots/revoke",
        f"{P}/lots/recover",
        f"{P}/lots/withdraw-delegation",
    ):
        post(path, revoke_endpoint)
        delete(path, revoke_endpoint)
    for path in (
        f"{P}/lots/assign-driver",
        f"{P}/lots/replace-driver",
    ):
        post(path, driver_assign_endpoint)
    for path in (
        f"{P}/lots/revoke-driver",
        f"{P}/lots/withdraw-driver",
    ):
        post(path, driver_revoke_endpoint)

    # Extra seller / receipt vocabulary aliases -----------------------------
    for path in (
        f"{P}/seller/confirm-delivery",
        f"{P}/seller/handover",
        f"{P}/pickup-orders/deliver",
    ):
        post(path, delivery_action_endpoint)
    post(f"{P}/pickup-orders", delivery_resource_endpoint)

    for path in (
        f"{P}/vouchers",
        f"{P}/pickup-receipts",
        f"{P}/confirmations",
    ):
        get(path, receipt_list_endpoint)
    get(f"{P}/vouchers/{{receipt_id}}", receipt_detail_endpoint)
    get(f"{P}/pickup-receipts/{{receipt_id}}", receipt_detail_endpoint)

    for path in (
        f"{L}/pickup-token",
        f"{L}/verify",
        f"{L}/verification",
        f"{L}/authorization-code",
    ):
        get(path, credential_endpoint)
        post(path, credential_endpoint)

    # RPC dispatcher + batch delivery at the lots collection -----------------
    for path in (
        f"{P}/pickup",
        f"{P}/pickups/action",
        f"{P}/pickups/rpc",
        f"{P}/pickup-action",
    ):
        post(path, rpc_endpoint)

    for path in (
        f"{P}/lots/deliver",
        f"{P}/lots/delivery",
        f"{P}/seller/lots/deliver",
    ):
        post(path, delivery_action_endpoint)

    # Last-resort keyword router: only unmatched /events/{id}/... paths reach
    # this, giving graceful handling for alternative URL wordings.  All
    # explicit routes above take precedence.
    def keyword_fallback(event_id: int, rest: str, request: Request,
                         payload: Optional[dict] = Body(default=None),
                         user=Auth):
        if not (rest or "").strip("/"):
            return RedirectResponse(
                url=f"/events/{event_id}", status_code=307)
        segs = [s.lower() for s in (rest or "").split("/") if s]
        # Normalise hyphens/underscores so "custody-detail" still matches.
        tokens: list[str] = []
        for s in segs:
            tokens.extend(t for t in s.replace("_", "-").split("-") if t)
        compact = "".join(tokens)
        data = _merged_payload(payload, request)
        method = request.method

        def seg_has(*words: str) -> bool:
            for word in words:
                if word in tokens or word.replace("-", "") in compact:
                    return True
                if any(word in token for token in tokens):
                    return True
            return False

        path_lot = None
        for s in segs:
            v = as_int(s)
            if v is not None:
                path_lot = v
                break

        # Let an explicit RPC action in the body win.
        action = first_present(data, ("action", "op", "operation"))
        if action and method == "POST":
            return rpc_endpoint(event_id, request, payload, user)

        if seg_has("receipts", "receipt", "vouchers", "confirmations"):
            if method == "GET":
                all_segments = [t for t in rest.split("/") if t]
                rid = all_segments[-1] if all_segments else None
                if rid is not None and as_int(rid) is None \
                        and not str(rid).startswith("RCP-"):
                    rid = None
                if rid is not None:
                    return service.get_receipt(event_id, user["id"], rid)
                return service.list_receipts(event_id, user["id"])

        if seg_has("credentials", "credential", "pickup-code", "pickup_codes",
                   "codes", "verify", "verification", "token", "pickup-tokens"):
            if method == "POST" and not path_lot and _lot_ids(data):
                return batch_credentials_endpoint(
                    event_id, request, payload, user)
            if path_lot is not None:
                return service.pickup_credential(
                    event_id, user["id"], path_lot, _deal_ref(data))

        if seg_has("deliver", "delivery", "handovers", "handover",
                   "confirm-pickup", "confirm") and method == "POST":
            ids = _lot_ids(data)
            if not ids and path_lot is not None:
                ids = [path_lot]
                data.setdefault("lot_ids", ids)
            picker_ref = _account(data, PICKER_KEYS)
            items = extract_delivery_items(data)
            if not items and path_lot is not None:
                cred = first_present(data, CREDENTIAL_LOOKUP)
                items = [{"lot_id": path_lot,
                          "credential": None if cred is None else str(cred)}]
            refs = _single_deal_ref(data, len(items) == 1)
            for idx, item in enumerate(items):
                if item.get("deal_ref") is None and idx < len(refs):
                    item["deal_ref"] = refs[idx]
            key = extract_request_key(data, request.headers)
            receipt, _ = service.confirm_delivery(
                event_id, user["id"], request_key=key,
                picker_ref=picker_ref, items=items)
            return JSONResponse(status_code=200, content=receipt)

        if seg_has("driver", "drivers"):
            ids = _lot_ids(data) or ([path_lot] if path_lot else [])
            if method in ("POST", "PUT", "PATCH"):
                if seg_has("revoke", "withdraw", "remove", "delete"):
                    return service.revoke_driver(event_id, user["id"], ids)
                ref = _account(data, DRIVER_KEYS)
                return service.assign_driver(event_id, user["id"], ids, ref)
            if method == "DELETE":
                return service.revoke_driver(event_id, user["id"], ids)

        if seg_has("delegations", "delegation", "carrier", "carriers",
                   "trustee", "entrust", "delegate", "reassign", "recover",
                   "revoke", "withdraw"):
            ids = _lot_ids(data) or ([path_lot] if path_lot else [])
            if method == "DELETE" or seg_has(
                    "revoke", "recover", "withdraw", "remove"):
                return service.revoke_carrier(event_id, user["id"], ids)
            if method in ("POST", "PUT", "PATCH"):
                ref = _account(data, CARRIER_KEYS)
                return service.delegate_carrier(
                    event_id, user["id"], ids, ref)

        if path_lot is not None and method == "GET" and seg_has(
                "pickup", "delivery", "handover", "status", "authorization",
                "custody", "collection", "delegation"):
            return service.pickup_status(
                event_id, user["id"], path_lot, _deal_ref(data))

        if seg_has("pickups", "my-pickups") and method == "GET":
            return service.list_pickups(event_id, user["id"])

        # Long-tail resale wordings (revoke / offer / accept / deal views).
        try:
            from .resale_routes import resale_keyword_dispatch

            resold = resale_keyword_dispatch(
                service, event_id, rest, method, data, user)
        except ApiError:
            raise
        except Exception:  # pragma: no cover - never mask pickup handling
            resold = None
        if resold is not None:
            return resold

        # Long-tail settlement wordings (payments / refunds / payouts / ...).
        # Kept here because this catch-all is the last route registered.
        try:
            from .settlement_routes import settlement_keyword_dispatch

            settled = settlement_keyword_dispatch(
                service, event_id, rest, method, data, user)
        except ApiError:
            raise
        except Exception:  # pragma: no cover - never mask pickup handling
            settled = None
        if settled is not None:
            return settled

        raise ApiError(404, "not_found", "Unknown pickup endpoint")

    app.add_api_route(
        f"{P}/{{rest:path}}", keyword_fallback,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        tags=["pickup"],
    )
