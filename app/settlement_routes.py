"""HTTP routes for offline settlement (bank receipts / refunds / payouts).

The behavioural contract does not fix URLs or field names, so -- exactly like
:mod:`app.pickup_routes` -- this module exposes one canonical REST-ish surface
and mounts the same strict operations at many natural path/field aliases.
All payloads are parsed leniently; the service layer stays strict.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import Body, Depends, Request
from fastapi.responses import JSONResponse

from .errors import ApiError
from .pickups import (
    as_int,
    extract_lot_ids,
    extract_request_key,
    first_present,
)
from .settlements import PAYER_KEYS, SERIAL_KEYS, SELLER_REF_KEYS

DEAL_REF_KEYS = (
    "deal_ref", "deal_id", "deal_no", "deal", "sale_id", "sale_no",
    "transaction_id", "purchase_id",
)


def _deal_refs(data: dict, lot_ids: list[int]) -> list:
    """Per-lot deal references from an item list, or a single top-level ref."""
    for container_key in ("items", "payment_items", "allocations", "lines",
                          "records"):
        container = _np(data, container_key)
        if isinstance(container, list) and container and all(
                isinstance(x, dict) for x in container):
            refs = []
            for element in container:
                refs.append(_np(element, *DEAL_REF_KEYS))
            if any(r is not None for r in refs):
                return refs
    single = _np(data, *DEAL_REF_KEYS, "deal_refs", "deals")
    if single is None:
        return []
    if isinstance(single, (list, tuple)):
        return list(single)
    return [single]


def _norm_key(k: str) -> str:
    return re.sub(r"[_\-\s]", "", str(k).lower())


def _np(data: dict, *names: str):
    """first_present exact match, then separator/case-insensitive fallback."""
    value = first_present(data, names)
    if value is not None:
        return value
    if not isinstance(data, dict):
        return None
    wanted = {_norm_key(n) for n in names}
    for k, v in data.items():
        if isinstance(k, str) and _norm_key(k) in wanted and v is not None:
            return v
    return None


def _merged(payload: dict | None, request: Request) -> dict:
    data = dict(payload or {})
    for key, value in request.query_params.multi_items():
        data.setdefault(key, value)
    # Tolerate literal Chinese field names a caller might use.
    zh_map = {
        "银行流水号": "serial_no", "流水号": "serial_no",
        "交易流水号": "serial_no", "回单号": "serial_no",
        "付款买家": "buyer_id", "买家": "buyer_id",
        "买家ID": "buyer_id", "付款人": "buyer_id",
        "金额": "amount", "收款金额": "amount", "打款金额": "amount",
        "拍品": "lot_ids", "拍品IDs": "lot_ids", "拍品编号": "lot_ids",
        "请求号": "request_id", "申请号": "request_id",
        "卖家": "seller_id", "卖家ID": "seller_id",
        "原因": "reason", "备注": "reason",
    }
    for zh, en in zh_map.items():
        if zh in data and en not in data:
            data[en] = data[zh]
    return data


def _lot_ids(data: dict) -> list[int]:
    """Lot ids from lot_ids/lots/items([{lot_id}])/... shapes."""
    # A container of dict objects first (items/lines/records).
    for container_key in ("items", "payment_items", "lots", "lines",
                          "records", "lot_items", "allocations"):
        container = _np(data, container_key)
        if isinstance(container, list) and container and all(
                isinstance(x, dict) for x in container):
            ids: list[int] = []
            for element in container:
                v = first_present(
                    element, ("lot_id", "id", "lotId", "lot", "lotID",
                              "LotId", "lotid"))
                if v is None:
                    for ek, ev in element.items():
                        if isinstance(ek, str) and _norm_key(ek) == "lotid":
                            v = ev
                            break
                lot_id = as_int(v)
                if lot_id is not None:
                    ids.append(lot_id)
            if ids:
                return ids
    ids = extract_lot_ids(data)
    if not ids:
        raw = _np(data, "lot_ids", "lotids", "ids")
        if isinstance(raw, list):
            ids = [as_int(x) for x in raw if as_int(x) is not None]
        elif raw is not None and as_int(raw) is not None:
            ids = [as_int(raw)]
    if not ids:
        single = as_int(_np(data, "lot_id", "lotid"))
        if single is not None:
            ids = [single]
    return ids


def _serial(data: dict) -> str | None:
    value = _np(data, *SERIAL_KEYS, "reference_number",
                "transaction_reference", "bank_transaction_id",
                "bank_statement_no", "statement_number")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _payer_ref(data: dict):
    return _np(data, *PAYER_KEYS, "winner_id", "winning_buyer_id",
               "account", "user", "user_id", "username", "name")


def _seller_ref(data: dict):
    return _np(data, *SELLER_REF_KEYS,
               "account", "username", "user_name", "name")


def _amount(data: dict):
    return _np(data, "amount", "total_amount", "payment_amount", "sum",
               "value", "money", "payout_amount", "requested_amount", "amt",
               "received_amount", "receipt_amount", "bank_amount",
               "total_sum", "money_amount")


def _payout_ref(data: dict, path_ref: str | int | None = None):
    if path_ref is not None:
        return path_ref
    return _np(data, "payout_id", "application_id", "withdrawal_id", "id",
               "payout", "payout_ref", "request_id", "request_key",
               "request_no", "idempotency_key", "req_id")


def register_settlement_routes(app, service, current_user) -> None:
    Auth = Depends(current_user)
    P = "/events/{event_id}"

    # ------------------------------------------------------------- payments
    def _do_register(event_id: int, data: dict, user, request: Request = None):
        serial = _serial(data)
        if serial is None and request is not None:
            serial = request.headers.get("idempotency-key") \
                or request.headers.get("x-request-id") \
                or request.headers.get("request-id")
        buyer_ref = _payer_ref(data)
        amount = _amount(data)
        ids = _lot_ids(data)
        deal_refs = _deal_refs(data, ids)
        return service.register_payment(
            event_id, user["id"], serial_no=serial, buyer_ref=buyer_ref,
            amount=amount, lot_ids=ids, deal_refs=deal_refs)

    def payment_resource(event_id: int, request: Request,
                         payload: Optional[dict] = Body(default=None),
                         user=Auth):
        data = _merged(payload, request)
        body, created = _do_register(event_id, data, user, request)
        return JSONResponse(status_code=201 if created else 200, content=body)

    def payment_action(event_id: int, request: Request,
                       payload: Optional[dict] = Body(default=None),
                       user=Auth):
        data = _merged(payload, request)
        body, _created = _do_register(event_id, data, user, request)
        return JSONResponse(status_code=200, content=body)

    def payment_list(event_id: int, request: Request, user=Auth):
        status = request.query_params.get("status") or \
            request.query_params.get("state")
        return service.list_payments(event_id, user["id"], status)

    def payment_detail(event_id: int, payment_ref: str, user=Auth):
        return service.get_payment(event_id, user["id"], payment_ref)

    # -------------------------------------------------------------- refunds
    def _do_refund(event_id: int, data: dict, user, path_serial=None):
        serial = path_serial or _serial(data) or first_present(
            data, ("payment_ref", "payment_id", "id"))
        reason = first_present(data, ("reason", "note", "memo", "comment")) \
            or ""
        return service.refund_payment(
            event_id, user["id"], serial_no=serial, reason=str(reason))

    def refund_resource(event_id: int, request: Request,
                        payload: Optional[dict] = Body(default=None),
                        user=Auth):
        data = _merged(payload, request)
        body, created = _do_refund(event_id, data, user)
        return JSONResponse(status_code=201 if created else 200, content=body)

    def refund_action(event_id: int, request: Request,
                      payload: Optional[dict] = Body(default=None),
                      user=Auth):
        data = _merged(payload, request)
        body, _created = _do_refund(event_id, data, user)
        return JSONResponse(status_code=200, content=body)

    def refund_for_serial(event_id: int, serial: str, request: Request,
                         payload: Optional[dict] = Body(default=None),
                         user=Auth):
        data = _merged(payload, request)
        body, _created = _do_refund(event_id, data, user, serial)
        return JSONResponse(status_code=200, content=body)

    def refund_list(event_id: int, user=Auth):
        return service.list_refunds(event_id, user["id"])

    # -------------------------------------------------------------- payouts
    def _do_request_payout(event_id: int, data: dict, user, headers=None):
        amount = _amount(data)
        key = extract_request_key(data, headers)
        if key is None:
            key = first_present(data, (
                "request_id", "request_no", "payout_request_id",
                "application_no", "apply_no"))
        seller_ref = _seller_ref(data)
        kwargs = {}
        if seller_ref is not None:
            kwargs["seller_ref"] = seller_ref
        return service.request_payout(
            event_id, user["id"], amount=amount, request_key=key, **kwargs)

    def payout_resource(event_id: int, request: Request,
                        payload: Optional[dict] = Body(default=None),
                        user=Auth):
        data = _merged(payload, request)
        body, created = _do_request_payout(
            event_id, data, user, request.headers)
        return JSONResponse(status_code=201 if created else 200, content=body)

    def payout_action(event_id: int, request: Request,
                      payload: Optional[dict] = Body(default=None),
                      user=Auth):
        data = _merged(payload, request)
        body, _created = _do_request_payout(
            event_id, data, user, request.headers)
        return JSONResponse(status_code=200, content=body)

    def payout_list(event_id: int, request: Request, user=Auth):
        qp = request.query_params
        seller_ref = qp.get("seller_id") or qp.get("seller")
        req_key = qp.get("request_id") or qp.get("request_no") \
            or qp.get("request_key") or qp.get("idempotency_key")
        return service.list_payouts(
            event_id, user["id"],
            status_filter=qp.get("status") or qp.get("state"),
            seller_ref=seller_ref if seller_ref is not None else ...,
            request_key=req_key,
        )

    def payout_detail(event_id: int, payout_ref: str, user=Auth):
        return service.get_payout(event_id, user["id"], payout_ref)

    def payout_status_by_query(event_id: int, request: Request, user=Auth):
        qp = request.query_params
        ref = qp.get("payout_id") or qp.get("id") or qp.get("request_id") \
            or qp.get("request_no") or qp.get("application_id") \
            or qp.get("ref")
        if ref is None:
            data = dict(qp)
            ref = _payout_ref(data)
        if ref is None:
            # No reference -> fall back to the scoped list/summary.
            return service.list_payouts(event_id, user["id"])
        return service.get_payout(event_id, user["id"], ref)

    def settleable_lots(event_id: int, request: Request, user=Auth):
        target = None
        if user["role"] == "admin":
            target = request.query_params.get("seller_id") \
                or request.query_params.get("seller")
        if target is not None:
            summary = service.settlement_summary(
                event_id, user["id"], seller_ref=target)
        else:
            summary = service.settlement_summary(event_id, user["id"])
        lots = summary.get("lots", [])
        return {
            "event_id": event_id,
            "lots": lots,
            "items": lots,
            "settleable_lots": lots,
            "count": len(lots),
        }

    def combined_records(event_id: int, user=Auth):
        # Admin-only reconciliation bundle; the service calls enforce role.
        payments = service.list_payments(event_id, user["id"])
        refunds = service.list_refunds(event_id, user["id"])
        payouts = service.list_payouts(event_id, user["id"])
        return {
            "event_id": event_id,
            "payments": payments["payments"],
            "refunds": refunds["refunds"],
            "payouts": payouts["payouts"],
            "items": {
                "payments": payments["payments"],
                "refunds": refunds["refunds"],
                "payouts": payouts["payouts"],
            },
            "counts": {
                "payments": payments["count"],
                "refunds": refunds["count"],
                "payouts": payouts["count"],
            },
        }

    def _do_decide(event_id: int, ref, action: str, data: dict, user):
        seller_ref = _seller_ref(data)
        kwargs = {}
        if seller_ref is not None:
            kwargs["seller_ref"] = seller_ref
        body, _changed = service.decide_payout(
            event_id, user["id"], ref, action=action, **kwargs)
        return body

    def payout_decide_path(action: str):
        def ep(event_id: int, payout_ref: str, request: Request,
               payload: Optional[dict] = Body(default=None), user=Auth):
            data = _merged(payload, request)
            ref = _payout_ref(data, payout_ref)
            return _do_decide(event_id, ref, action, data, user)
        return ep

    def payout_decide_body(action: str):
        def ep(event_id: int, request: Request,
               payload: Optional[dict] = Body(default=None), user=Auth):
            data = _merged(payload, request)
            ref = _payout_ref(data)
            return _do_decide(event_id, ref, action, data, user)
        return ep

    confirm_path = payout_decide_path("confirm")
    release_path = payout_decide_path("release")
    cancel_path = payout_decide_path("cancel")
    confirm_body = payout_decide_body("confirm")
    release_body = payout_decide_body("release")
    cancel_body = payout_decide_body("cancel")

    _STATUS_DECISION = {
        "paid": "confirm", "confirmed": "confirm", "complete": "confirm",
        "completed": "confirm", "success": "confirm", "done": "confirm",
        "remitted": "confirm", "transferred": "confirm", "approve": "confirm",
        "approved": "confirm", "confirm": "confirm",
        "released": "release", "release": "release",
        "unfrozen": "release",
        "cancelled": "cancel", "canceled": "cancel", "cancel": "cancel",
        "rejected": "cancel", "reject": "cancel",
    }

    def payout_update_endpoint(event_id: int, payout_ref: str, request: Request,
                               payload: Optional[dict] = Body(default=None),
                               user=Auth):
        data = _merged(payload, request)
        decision = first_present(data, (
            "action", "status", "decision", "op", "state", "new_status"))
        if decision is None:
            decision = request.query_params.get("action") \
                or request.query_params.get("status")
        key = str(decision or "").strip().lower().replace("-", "_")
        kind = _STATUS_DECISION.get(key)
        if kind is None and key in ("confirm_paid", "mark_paid", "pay"):
            kind = "confirm"
        if kind is None:
            raise ApiError(
                422, "invalid_request",
                "Provide action/status 'paid', 'released' or 'cancelled'")
        ref = _payout_ref(data, payout_ref)
        return _do_decide(event_id, ref, kind, data, user)

    # ------------------------------------------------------------ summaries
    def my_settlement(event_id: int, request: Request, user=Auth):
        qp_seller = request.query_params.get("seller_id") \
            or request.query_params.get("seller")
        if qp_seller is not None and user["role"] == "admin":
            return service.settlement_summary(
                event_id, user["id"], seller_ref=qp_seller)
        return service.settlement_summary(event_id, user["id"])

    def seller_settlement(event_id: int, seller_ref: str, user=Auth):
        return service.settlement_summary(
            event_id, user["id"], seller_ref=seller_ref)

    # -------------------------------------------------------- RPC dispatcher
    def rpc(event_id: int, request: Request,
            payload: Optional[dict] = Body(default=None), user=Auth):
        data = _merged(payload, request)
        action = str(first_present(data, (
            "action", "op", "operation", "type", "method", "command",
            "verb", "do")) or "").strip().lower().replace("-", "_")

        if action in ("register_payment", "payment", "receive_payment",
                      "record_payment", "add_payment", "bank_receipt",
                      "create_payment", "shoukuan"):
            body, _ = _do_register(event_id, data, user)
            return JSONResponse(status_code=200, content=body)
        if action in ("refund", "refund_payment", "bounce",
                      "chargeback", "return_payment", "tuipiao"):
            body, _ = _do_refund(event_id, data, user)
            return JSONResponse(status_code=200, content=body)
        if action in ("request_payout", "payout", "withdraw",
                      "withdrawal", "apply_payout", "dakuan"):
            body, _ = _do_request_payout(event_id, data, user)
            return JSONResponse(status_code=200, content=body)
        if action in ("confirm", "confirm_paid", "mark_paid",
                      "pay_payout", "complete_payout", "pay"):
            return _do_decide(event_id, _payout_ref(data), "confirm",
                              data, user)
        if action in ("release", "unfreeze", "release_payout"):
            return _do_decide(event_id, _payout_ref(data), "release",
                              data, user)
        if action in ("cancel", "cancel_payout", "reject_payout"):
            return _do_decide(event_id, _payout_ref(data), "cancel",
                              data, user)
        if action in ("settlement", "summary", "balance",
                      "get_settlement"):
            return service.settlement_summary(event_id, user["id"])
        raise ApiError(422, "invalid_request",
                       f"Unknown settlement action: {action!r}")

    # ------------------------------------------------------------- mounting
    post = lambda path, ep, code=None: app.add_api_route(
        path, ep, methods=["POST"], tags=["settlement"])
    get = lambda path, ep: app.add_api_route(
        path, ep, methods=["GET"], tags=["settlement"])
    put = lambda path, ep: app.add_api_route(
        path, ep, methods=["PUT", "PATCH"], tags=["settlement"])

    # ---- admin: register / list payments ----
    payment_collection_paths = (
        f"{P}/payments",
        f"{P}/payment",
        f"{P}/bank-payments",
        f"{P}/bank-payment",
        f"{P}/bank-receipts",
        f"{P}/bank-receipt",
        f"{P}/bank-records",
        f"{P}/remittances",
        f"{P}/remittance",
        f"{P}/payment-records",
        f"{P}/bank-transactions",
        f"{P}/transactions",
        f"{P}/settlement/payments",
        f"{P}/settlements/payments",
        f"{P}/admin/payments",
        f"{P}/admin/bank-receipts",
        f"{P}/shoukuan",
    )
    for path in payment_collection_paths:
        post(path, payment_resource)
        get(path, payment_list)

    for path in (
        f"{P}/payments/register",
        f"{P}/payments/create",
        f"{P}/payments/receive",
        f"{P}/payments/add",
        f"{P}/payments/record",
        f"{P}/payments/new",
        f"{P}/bank-receipts/register",
        f"{P}/bank-receipts/create",
        f"{P}/bank-payments/register",
    ):
        post(path, payment_action)

    get(f"{P}/payments/{{payment_ref}}", payment_detail)
    get(f"{P}/payment/{{payment_ref}}", payment_detail)

    # ---- admin: refunds ----
    for path in (
        f"{P}/refunds",
        f"{P}/refund-records",
        f"{P}/bounces",
        f"{P}/chargebacks",
        f"{P}/reversals",
        f"{P}/admin/refunds",
        f"{P}/tuipiao",
    ):
        post(path, refund_resource)
        get(path, refund_list)

    for path in (
        f"{P}/refund",
        f"{P}/refunds/register",
        f"{P}/refunds/create",
        f"{P}/payments/refund",
        f"{P}/payments/refund-by-serial",
        f"{P}/payments/refund-serial",
        f"{P}/payments/bounce",
        f"{P}/payments/chargeback",
        f"{P}/payments/return",
        f"{P}/payments/revoke",
        f"{P}/bank-receipts/refund",
        f"{P}/settlement/refund",
        f"{P}/admin/refund",
    ):
        post(path, refund_action)

    for path in (
        f"{P}/payments/{{serial}}/refund",
        f"{P}/payments/serial/{{serial}}/refund",
        f"{P}/payments/{{serial}}/bounce",
        f"{P}/payments/{{serial}}/chargeback",
        f"{P}/payments/{{serial}}/return",
        f"{P}/payments/{{serial}}/revoke",
        f"{P}/refunds/{{serial}}",
        f"{P}/refunds/serial/{{serial}}",
        f"{P}/tuipiao/{{serial}}",
    ):
        post(path, refund_for_serial)

    # REST-style DELETE on a payment/refund resource == whole-serial refund.
    for path in (
        f"{P}/payments/{{serial}}",
        f"{P}/bank-receipts/{{serial}}",
    ):
        app.add_api_route(path, refund_for_serial,
                          methods=["DELETE"], tags=["settlement"])

    # ---- seller: request payout; list (own/admin all) ----
    payout_collection_paths = (
        f"{P}/payouts",
        f"{P}/payout-requests",
        f"{P}/withdrawals",
        f"{P}/withdraw-requests",
        f"{P}/cash-outs",
        f"{P}/cashouts",
        f"{P}/seller/payouts",
        f"{P}/my-payouts",
        f"{P}/payout-records",
        f"{P}/transfers",
        f"{P}/transfer-requests",
        f"{P}/payment-requests",
        f"{P}/settlement/payouts",
        f"{P}/settlement/requests",
        f"{P}/settlements/payouts",
        f"{P}/admin/payouts",
        f"{P}/dakuan",
    )
    for path in payout_collection_paths:
        post(path, payout_resource)
        get(path, payout_list)

    for path in (
        f"{P}/payout",
        f"{P}/payouts/request",
        f"{P}/payouts/apply",
        f"{P}/payouts/new",
        f"{P}/payout-request",
        f"{P}/withdrawal",
        f"{P}/payout-requests/create",
        f"{P}/withdrawals/request",
        f"{P}/withdraw",
        f"{P}/request-payout",
        f"{P}/cash-out",
        f"{P}/seller/payout",
        f"{P}/seller/request-payout",
        f"{P}/seller/payouts/request",
        f"{P}/settlement/payout",
        f"{P}/settlement/request-payout",
    ):
        post(path, payout_action)
        if path in (f"{P}/payout", f"{P}/withdrawal",
                    f"{P}/payout-request"):
            get(path, payout_list)

    # Body-keyed decisions (static segments BEFORE the {payout_ref} route).
    for path in (
        f"{P}/payouts/confirm",
        f"{P}/payouts/mark-paid",
        f"{P}/payouts/pay",
        f"{P}/payouts/complete",
        f"{P}/payouts/approve",
        f"{P}/payouts/release",
        f"{P}/payouts/unfreeze",
        f"{P}/payouts/cancel",
        f"{P}/payouts/reject",
        f"{P}/payouts/decide",
        f"{P}/payouts/action",
        f"{P}/payouts/rpc",
        f"{P}/withdrawals/confirm",
        f"{P}/withdrawals/release",
        f"{P}/withdrawals/cancel",
    ):
        if path.endswith("confirm") or path.endswith("mark-paid") \
                or path.endswith("/pay") or path.endswith("complete") \
                or path.endswith("approve"):
            post(path, confirm_body)
        elif path.endswith("release") or path.endswith("unfreeze"):
            post(path, release_body)
        elif path.endswith("cancel") or path.endswith("reject"):
            post(path, cancel_body)
        else:
            post(path, rpc)

    # Static summary/status lookups MUST precede /payouts/{payout_ref}.
    for path in (
        f"{P}/payouts/summary",
        f"{P}/payouts/balance",
        f"{P}/payouts/quota",
        f"{P}/payouts/total",
        f"{P}/payout-status",
        f"{P}/payout-status/",
        f"{P}/payouts/status",
        f"{P}/withdrawals/summary",
    ):
        get(path, payout_status_by_query
            if path.endswith("payout-status") or path.endswith("/status")
            else my_settlement)

    def payout_by_request(event_id: int, request_key: str, user=Auth):
        return service.get_payout(event_id, user["id"], request_key)

    get(f"{P}/payouts/by-request/{{request_key}}", payout_by_request)
    get(f"{P}/payout-requests/{{payout_ref}}", payout_detail)
    get(f"{P}/withdraw-requests/{{payout_ref}}", payout_detail)
    get(f"{P}/payouts/request/{{payout_ref}}", payout_detail)
    get(f"{P}/payout-request/{{payout_ref}}", payout_detail)

    # Detail + path-keyed decisions.
    get(f"{P}/payouts/{{payout_ref}}", payout_detail)
    get(f"{P}/payouts/{{payout_ref}}/status", payout_detail)
    get(f"{P}/withdrawals/{{payout_ref}}", payout_detail)

    # REST-style generic update: PATCH/POST /payouts/{id} {"status":"paid"}.
    for method in ("post", "put", "patch"):
        app.add_api_route(
            f"{P}/payouts/{{payout_ref}}", payout_update_endpoint,
            methods=[method.upper()], tags=["settlement"])
        app.add_api_route(
            f"{P}/payouts/{{payout_ref}}/status", payout_update_endpoint,
            methods=[method.upper()], tags=["settlement"])
        app.add_api_route(
            f"{P}/withdrawals/{{payout_ref}}", payout_update_endpoint,
            methods=[method.upper()], tags=["settlement"])

    for suffix, ep in (
        ("confirm", confirm_path),
        ("confirm-paid", confirm_path),
        ("confirm_paid", confirm_path),
        ("confirm-payment", confirm_path),
        ("mark-paid", confirm_path),
        ("mark_paid", confirm_path),
        ("mark-as-paid", confirm_path),
        ("pay", confirm_path),
        ("paid", confirm_path),
        ("complete", confirm_path),
        ("approve", confirm_path),
        ("remit", confirm_path),
        ("send", confirm_path),
        ("release", release_path),
        ("release-hold", release_path),
        ("unfreeze", release_path),
        ("unhold", release_path),
        ("free", release_path),
        ("cancel", cancel_path),
        ("cancel-hold", cancel_path),
        ("reject", cancel_path),
        ("revoke", cancel_path),
    ):
        for pattern in (
            f"{P}/payouts/{{payout_ref}}/{suffix}",
            f"{P}/withdrawals/{{payout_ref}}/{suffix}",
            f"{P}/admin/payouts/{{payout_ref}}/{suffix}",
            f"{P}/settlement/payouts/{{payout_ref}}/{suffix}",
        ):
            post(pattern, ep)
            put(pattern, ep)

    # Prefix-style: /payouts/confirm/{ref}
    for word, ep in (("confirm", confirm_path), ("release", release_path),
                     ("cancel", cancel_path)):
        post(f"{P}/payouts/{word}/{{payout_ref}}", ep)
        post(f"{P}/admin/payouts/{word}/{{payout_ref}}", ep)

    # ---- seller balance / settlement view ----
    summary_get_paths = (
        f"{P}/settlement",
        f"{P}/settlement/summary",
        f"{P}/settlement/balance",
        f"{P}/settlement/quota",
        f"{P}/settlement/me",
        f"{P}/settlement/current",
        f"{P}/settlements",
        f"{P}/settlements/me",
        f"{P}/settlements/current",
        f"{P}/settlements/balance",
        f"{P}/settlements/summary",
        f"{P}/settlements/quota",
        f"{P}/my-settlement",
        f"{P}/my-settlements",
        f"{P}/my/settlement",
        f"{P}/seller/settlement",
        f"{P}/seller/settlement-summary",
        f"{P}/seller/balance",
        f"{P}/seller/earnings",
        f"{P}/earnings",
        f"{P}/income",
        f"{P}/wallet",
        f"{P}/balance",
        f"{P}/account/balance",
        f"{P}/settlement-balance",
        f"{P}/payout-balance",
        f"{P}/available-quota",
        f"{P}/jiesuan",
    )
    for path in summary_get_paths:
        get(path, my_settlement)

    # A few extra natural GET wordings.
    get(f"{P}/settle", my_settlement)
    get(f"{P}/me/settlement", my_settlement)
    get(f"{P}/me/balance", my_settlement)
    get(f"{P}/settlements/mine", my_settlement)
    get(f"{P}/settlements/{{seller_ref}}", seller_settlement)

    # POST /settlements acts as the seller payout-application collection.
    post(f"{P}/settlements", payout_resource)

    # Per-lot settlement eligibility for the calling seller.
    for path in (
        f"{P}/settlement/lots",
        f"{P}/settlement/items",
        f"{P}/settleable-lots",
        f"{P}/settlement-lots",
        f"{P}/my-settlement/lots",
    ):
        get(path, settleable_lots)

    # Admin combined reconciliation bundle + settlement-scoped record lists.
    get(f"{P}/settlement/records", combined_records)
    get(f"{P}/settlement-records", combined_records)
    get(f"{P}/admin/settlement/records", combined_records)
    get(f"{P}/settlement/refunds", refund_list)
    get(f"{P}/admin/settlement", combined_records)
    get(f"{P}/admin/settlement/", combined_records)

    for path in (
        f"{P}/sellers/{{seller_ref}}/settlement",
        f"{P}/sellers/{{seller_ref}}/balance",
        f"{P}/admin/sellers/{{seller_ref}}/settlement",
        f"{P}/admin/settlement/{{seller_ref}}",
        f"{P}/settlement/seller/{{seller_ref}}",
        f"{P}/settlements/seller/{{seller_ref}}",
    ):
        get(path, seller_settlement)

    # ---- RPC ----
    for path in (
        f"{P}/settlement/action",
        f"{P}/settlement/rpc",
        f"{P}/settlements/action",
        f"{P}/settlements/rpc",
        f"{P}/settle",
        f"{P}/settlement",
    ):
        post(path, rpc)


def settlement_keyword_dispatch(service, event_id: int, rest: str,
                                method: str, data: dict, user: dict):
    """Best-effort handling for settlement wordings missed by explicit routes.

    Invoked from the pickup keyword fallback (the final catch-all) right
    before it gives up with 404.  Returns a response payload/Response or
    ``None`` when the path is not settlement-related.
    """
    segs = [s.lower() for s in (rest or "").split("/") if s]
    tokens: list[str] = []
    for s in segs:
        tokens.extend(t for t in s.replace("_", "-").split("-") if t)
    compact = "".join(tokens)

    strong = ("payment", "payments", "payout", "payouts", "refund",
              "refunds", "settlement", "settlements", "withdraw",
              "withdrawal", "withdrawals", "shoukuan", "tuipiao", "dakuan",
              "jiesuan", "remittance", "remittances", "chargeback",
              "bounce", "bankreceipt", "bankpayment", "cashout")
    # POST /receipts with bank-receipt fields is a payment registration even
    # though delivery receipts own the GET /receipts namespace.
    receipt_as_payment = (
        method in ("POST", "PUT", "PATCH")
        and "receipt" in compact
        and (_serial(data) is not None or _payer_ref(data) is not None)
        and bool(_lot_ids(data))
    )
    if not receipt_as_payment and not any(
            w in compact or w in tokens for w in strong):
        return None

    path_ref = None
    for s in segs:
        v = as_int(s)
        if v is not None:
            path_ref = v
            break

    wants_refund = any(w in compact for w in (
        "refund", "bounce", "chargeback", "tuipiao", "returnpayment"))
    wants_payout = any(w in compact for w in (
        "payout", "withdraw", "dakuan", "cashout"))
    wants_payment = receipt_as_payment or any(w in compact for w in (
        "payment", "shoukuan", "remittance", "bankreceipt", "bankpayment"))
    wants_summary = any(w in compact for w in (
        "settlement", "balance", "jiesuan")) and not (
        wants_payment or wants_payout or wants_refund)

    if method == "GET":
        if wants_refund:
            return service.list_refunds(event_id, user["id"])
        if wants_payout:
            if path_ref is not None and any(
                    w in compact for w in ("status", "detail")):
                return service.get_payout(event_id, user["id"], path_ref)
            return service.list_payouts(event_id, user["id"])
        if wants_payment:
            if path_ref is not None:
                return service.get_payment(event_id, user["id"], path_ref)
            return service.list_payments(event_id, user["id"])
        if wants_summary:
            return service.settlement_summary(event_id, user["id"])
        return None

    if method in ("POST", "PUT", "PATCH"):
        if wants_refund:
            serial = _serial(data)
            return JSONResponse(status_code=200, content=(
                service.refund_payment(
                    event_id, user["id"],
                    serial_no=serial or path_ref or segs[0])[0]))
        if wants_payout:
            decision = None
            if any(w in compact for w in ("confirm", "paid", "markpaid",
                                          "complete", "approve", "pay")):
                decision = "confirm"
            elif any(w in compact for w in ("release", "unfreeze")):
                decision = "release"
            elif any(w in compact for w in ("cancel", "reject")):
                decision = "cancel"
            if decision:
                ref = _payout_ref(data, path_ref)
                return _keyword_decide(service, event_id, user, ref,
                                       decision, data)
            amount = _amount(data)
            key = extract_request_key(data) or first_present(
                data, ("request_id", "request_no"))
            body, _ = service.request_payout(
                event_id, user["id"], amount=amount, request_key=key)
            return JSONResponse(status_code=200, content=body)
        if wants_payment:
            body, _ = service.register_payment(
                event_id, user["id"], serial_no=_serial(data),
                buyer_ref=_payer_ref(data), amount=_amount(data),
                lot_ids=_lot_ids(data))
            return JSONResponse(status_code=200, content=body)
    return None


def _keyword_decide(service, event_id, user, ref, action, data):
    seller_ref = _seller_ref(data)
    kwargs = {"seller_ref": seller_ref} if seller_ref is not None else {}
    body, _ = service.decide_payout(
        event_id, user["id"], ref, action=action, **kwargs)
    return JSONResponse(status_code=200, content=body)
