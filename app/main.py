"""FastAPI application factory.

Auth: ``Authorization: Bearer <token>`` (token handed out once at registration).

Route groups
  * public      - browse events/lots: current price, anonymous leader alias,
                  seller's reserve.  Never max bids or budgets.
  * buyer       - own budget (used / reserved / remaining), own bids, place bid
  * seller      - create/edit own lots, close own lot
  * admin       - create events, close lots/event
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import Database
from .errors import ApiError
from .models import (
    AuthOut,
    BidReceiptOut,
    BudgetOut,
    CloseOut,
    CreateAdmin,
    CreateEvent,
    CreateLot,
    EventCloseOut,
    EventOut,
    LotListOut,
    LotOut,
    MeOut,
    MyBidsOut,
    PlaceBid,
    RegisterUser,
    SetBudget,
    UpdateLot,
)
from .service import Service
from .pickup_routes import register_pickup_routes

DB_PATH = os.environ.get(
    "AUCTION_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auction.db"),
)


def create_app(db_path: str | None = None) -> FastAPI:
    database = Database(db_path or DB_PATH)
    service = Service(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init()
        yield

    app = FastAPI(
        title="Collectibles Online Auction", version="1.0.0", lifespan=lifespan
    )

    def get_service() -> Service:
        return service

    # --- error rendering ----------------------------------------------------
    @app.exception_handler(ApiError)
    async def api_error_handler(request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc: RequestValidationError):
        # exc.errors() can contain non-JSON-serialisable "input" values (bytes,
        # Decimal, ...); stringify the message only so the body always renders.
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request",
                               "message": "Request body is invalid",
                               "details": [
                                   {"loc": [str(p) for p in e.get("loc", ())],
                                    "msg": e.get("msg", ""),
                                    "type": e.get("type", "")}
                                   for e in exc.errors()
                               ]}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail)}},
        )

    # --- dependencies -------------------------------------------------------
    def current_user(
        authorization: str | None = Header(default=None),
        svc: Service = Depends(get_service),
    ):
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "unauthenticated",
                           "Send 'Authorization: Bearer <token>'")
        return svc.authenticate(authorization[len("Bearer "):])

    def buyer_user(user=Depends(current_user)):
        if user["role"] != "buyer":
            raise ApiError(403, "forbidden", "This endpoint is for buyers")
        return user

    def seller_user(user=Depends(current_user)):
        if user["role"] != "seller":
            raise ApiError(403, "forbidden", "This endpoint is for sellers")
        return user

    def admin_user(user=Depends(current_user)):
        if user["role"] != "admin":
            raise ApiError(403, "forbidden", "This endpoint is for admins")
        return user

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # --- events -------------------------------------------------------------
    @app.post("/events", response_model=EventOut, status_code=201)
    def create_event(body: CreateEvent, svc: Service = Depends(get_service)):
        return dict(svc.create_event(body.name))

    @app.get("/events/{event_id}", response_model=EventOut)
    def get_event(event_id: int, svc: Service = Depends(get_service)):
        return dict(svc.get_event(event_id))

    @app.post("/events/{event_id}/admin", response_model=AuthOut, status_code=201)
    def create_admin(event_id: int, body: CreateAdmin,
                     svc: Service = Depends(get_service)):
        return svc.create_admin(event_id, body.name, body.admin_key)

    @app.post("/events/{event_id}/users", response_model=AuthOut, status_code=201)
    def register(event_id: int, body: RegisterUser,
                 svc: Service = Depends(get_service)):
        return svc.register(event_id, body.name, body.role)

    @app.get("/me", response_model=MeOut)
    def me(user=Depends(current_user), svc: Service = Depends(get_service)):
        return svc.me(user)

    @app.post("/events/{event_id}/close", response_model=EventCloseOut)
    def close_event(event_id: int, user=Depends(admin_user),
                    svc: Service = Depends(get_service)):
        event, closed = svc.close_event(event_id, user["id"])
        return {"event": dict(event), "closed_lots": closed}

    # --- lots (public) ------------------------------------------------------
    @app.get("/events/{event_id}/lots", response_model=LotListOut)
    def list_lots(event_id: int, svc: Service = Depends(get_service)):
        return svc.list_lots(event_id)

    @app.get("/events/{event_id}/lots/{lot_id}", response_model=LotOut)
    def get_lot(event_id: int, lot_id: int, svc: Service = Depends(get_service)):
        return svc.get_lot(event_id, lot_id)

    # --- lots (seller) ------------------------------------------------------
    @app.post("/events/{event_id}/lots", response_model=LotOut, status_code=201)
    def create_lot(event_id: int, body: CreateLot, user=Depends(seller_user),
                   svc: Service = Depends(get_service)):
        lot = svc.create_lot(event_id, user["id"], body.title, body.description,
                             body.reserve)
        return svc.get_lot(event_id, lot["id"])

    @app.patch("/events/{event_id}/lots/{lot_id}", response_model=LotOut)
    def update_lot(event_id: int, lot_id: int, body: UpdateLot,
                   user=Depends(seller_user), svc: Service = Depends(get_service)):
        svc.update_lot(event_id, user["id"], lot_id, title=body.title,
                       description=body.description, reserve=body.reserve)
        return svc.get_lot(event_id, lot_id)

    @app.post("/events/{event_id}/lots/{lot_id}/close", response_model=CloseOut)
    def close_lot(event_id: int, lot_id: int, user=Depends(current_user),
                  svc: Service = Depends(get_service)):
        return svc.close_lot(event_id, user["id"], lot_id)

    # --- bidding (buyer) ----------------------------------------------------
    @app.post("/events/{event_id}/lots/{lot_id}/bids",
              response_model=BidReceiptOut, status_code=201)
    def place_bid(event_id: int, lot_id: int, body: PlaceBid,
                  user=Depends(buyer_user), svc: Service = Depends(get_service)):
        return svc.place_bid(event_id, user["id"], lot_id, body.max_bid)

    @app.get("/events/{event_id}/budget", response_model=BudgetOut)
    def my_budget(event_id: int, user=Depends(buyer_user),
                  svc: Service = Depends(get_service)):
        return svc.budget_view(event_id, user["id"])

    @app.put("/events/{event_id}/budget", response_model=BudgetOut)
    def set_budget(event_id: int, body: SetBudget, user=Depends(buyer_user),
                   svc: Service = Depends(get_service)):
        svc.set_budget(event_id, user["id"], body.budget)
        return svc.budget_view(event_id, user["id"])

    @app.get("/events/{event_id}/my-bids", response_model=MyBidsOut)
    def my_bids(event_id: int, user=Depends(buyer_user),
                svc: Service = Depends(get_service)):
        return svc.my_bids(event_id, user["id"])

    # --- post-sale pickup / delivery ("交割 / 领货") ------------------------
    register_pickup_routes(app, service, current_user)

    return app


app = create_app()
