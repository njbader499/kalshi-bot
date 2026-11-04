"""V2 order requests, rate-limit cost table, idempotent create and bot-only cancels."""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from kalshi_maker.auth import Signer
from kalshi_maker.money import PriceRange, TickGrid
from kalshi_maker.order_types import (
    BookSide,
    OrderError,
    OrderRequest,
    Outcome,
    is_bot_order,
    new_client_order_id,
    to_book,
)
from kalshi_maker.rest import (
    CostTable,
    KalshiREST,
    OrderOutcomeUnknown,
    is_write,
)
from kalshi_maker.settings import load_config

BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
NOW = 1_800_000_000
CENT_GRID = TickGrid([PriceRange(D(0), D(1), D("0.01"))])
TAPERED = TickGrid(
    [
        PriceRange(D(0), D("0.10"), D("0.001")),
        PriceRange(D("0.10"), D("0.90"), D("0.01")),
        PriceRange(D("0.90"), D(1), D("0.001")),
    ]
)


@pytest.fixture
def limits(cfg_file: Path):
    return load_config(cfg_file).rate_limits


async def _noop_sleep(_: float) -> None:
    return None


def _client(handler, limits) -> KalshiREST:
    signer = Signer("kid", ed25519.Ed25519PrivateKey.generate())
    return KalshiREST(
        BASE, limits, signer=signer, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )


def _order(**kw) -> OrderRequest:
    base = dict(
        ticker="KXHIGHNY-26NOV05-T60",
        side=BookSide.BID,
        price=D("0.05"),
        count=D(1),
        client_order_id=new_client_order_id(),
        expiration_time=NOW + 600,
    )
    base.update(kw)
    return OrderRequest(**base)  # type: ignore[arg-type]


# ---------- order request shape (verified against CreateOrderV2Request) ----------


def test_order_json_is_post_only_gtc_with_expiry() -> None:
    o = _order()
    o.validate(CENT_GRID, NOW)
    j = o.to_json()
    assert j == {
        "ticker": "KXHIGHNY-26NOV05-T60",
        "client_order_id": o.client_order_id,
        "side": "bid",
        "count": "1.00",
        "price": "0.0500",
        "time_in_force": "good_till_canceled",
        "expiration_time": NOW + 600,
        "post_only": True,
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
        "reduce_only": False,
    }
    for banned in ("type", "action", "yes_price", "no_price", "buy_max_cost"):
        assert banned not in j  # legacy v1 fields must not leak into V2 bodies


def test_buy_no_maps_to_yes_ask() -> None:
    assert to_book(Outcome.NO, D("0.30")) == (BookSide.ASK, D("0.70"))
    assert to_book(Outcome.YES, D("0.30")) == (BookSide.BID, D("0.30"))


@pytest.mark.parametrize(
    "kw,grid,msg",
    [
        ({"price": D("0.055")}, CENT_GRID, "tick grid"),
        ({"price": D("0.0555")}, TAPERED, "tick grid"),
        ({"price": D(0)}, CENT_GRID, "outside"),
        ({"price": D(1)}, CENT_GRID, "outside"),
        ({"count": D(0)}, CENT_GRID, "positive"),
        ({"count": D("1.005")}, CENT_GRID, "2 decimals"),
        ({"expiration_time": NOW}, CENT_GRID, "future"),
        ({"client_order_id": "manual-1"}, CENT_GRID, "client_order_id"),
        ({}, None, "tick grid"),
    ],
)
def test_order_validation_refuses(kw, grid, msg) -> None:
    with pytest.raises(OrderError, match=msg):
        _order(**kw).validate(grid, NOW)


def test_subpenny_price_ok_on_tapered_grid() -> None:
    o = _order(price=D("0.055"))
    o.validate(TAPERED, NOW)
    assert o.to_json()["price"] == "0.0550"


def test_bot_coid_prefix() -> None:
    c = new_client_order_id()
    assert is_bot_order(c)
    assert not is_bot_order(None)
    assert not is_bot_order("my-manual-order")
    assert not is_bot_order(c + "x")


# ---------- costs & buckets (verified against /account/endpoint_costs + rate_limits) ----------


def test_cost_table_templates(limits) -> None:
    ct = CostTable(10, limits.endpoint_costs)
    assert ct.cost("DELETE", "/portfolio/events/orders/abc-123") == 2
    assert ct.cost("DELETE", "/portfolio/events/orders/batched") == 2
    assert ct.cost("GET", "/portfolio/orders/abc") == 2
    assert ct.cost("GET", "/portfolio/orders") == 10
    assert ct.cost("POST", "/portfolio/events/orders") == 10
    assert ct.cost("POST", "/portfolio/events/orders/abc/amend") == 10
    ct.set("GET /trade-api/v2/cfbenchmarks/*endpoint", 50)
    assert ct.cost("GET", "/cfbenchmarks/a/b") == 50


def test_write_bucket_routing() -> None:
    assert is_write("POST", "/portfolio/events/orders")
    assert is_write("DELETE", "/portfolio/events/orders/batched")
    assert is_write("POST", "/portfolio/events/orders/x/amend")
    assert not is_write("GET", "/portfolio/orders")
    assert not is_write("GET", "/markets")


async def test_bucket_sizes_match_basic_tier(limits) -> None:
    async with _client(lambda r: httpx.Response(200, json={}), limits) as api:
        assert api.read_bucket.capacity == 600  # 200/s x 3 s
        assert api.write_bucket.capacity == 100  # 100/s x 1 s (Basic write: no banking)
        assert api.balance_bucket.capacity == 1
        # Basic: a create batch can hold at most 100/10 = 10 orders; cancels 100/2 = 50.
        assert api.max_batch_items("POST", "/portfolio/events/orders/batched") == 10
        assert api.max_batch_items("DELETE", "/portfolio/events/orders/batched") == 50


async def test_refresh_costs_and_account_limits(limits) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/account/endpoint_costs"):
            return httpx.Response(
                200,
                json={
                    "default_cost": 10,
                    "endpoint_costs": [
                        {"method": "GET", "path": "/trade-api/v2/markets", "cost": 7}
                    ],
                },
            )
        assert req.url.path.endswith("/account/limits")
        return httpx.Response(
            200,
            json={
                "usage_tier": "advanced",
                "read": {"refill_rate": 300, "bucket_capacity": 900},
                "write": {"refill_rate": 300, "bucket_capacity": 900},
                "grants": [],
            },
        )

    async with _client(handler, limits) as api:
        assert await api.refresh_endpoint_costs() == 1
        assert api.costs.cost("GET", "/markets") == 7
        assert await api.apply_account_limits() == "advanced"
        assert api.write_bucket.capacity == 900 and api.write_bucket.rate == 300


# ---------- V2 endpoints ----------


async def test_create_amend_cancel_paths_and_bodies(limits) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.method == "POST" and req.url.path.endswith("/amend"):
            return httpx.Response(200, json={"order_id": "o1", "ts_ms": 1})
        if req.method == "POST":
            return httpx.Response(
                201,
                json={
                    "order_id": "o1",
                    "fill_count": "0.00",
                    "remaining_count": "1.00",
                    "ts_ms": 1,
                },
            )
        return httpx.Response(200, json={"order_id": "o1", "reduced_by": "1.00", "ts_ms": 2})

    o = _order()
    async with _client(handler, limits) as api:
        r = await api.create_order(o, CENT_GRID, NOW)
        assert r["order_id"] == "o1"
        await api.amend_order(
            "o1", ticker=o.ticker, side="bid", price=D("0.05"), count=D(1),
            expiration_time=NOW + 10**9,
        )  # fmt: skip
        await api.cancel_order("o1", o.ticker)
    c, a, x = seen
    assert (c.method, c.url.path) == ("POST", "/trade-api/v2/portfolio/events/orders")
    assert json.loads(c.content)["post_only"] is True
    assert a.url.path == "/trade-api/v2/portfolio/events/orders/o1/amend"
    assert json.loads(a.content) == {
        "ticker": o.ticker, "side": "bid", "price": "0.0500", "count": "1.00",
        "expiration_time": NOW + 10**9,
    }  # fmt: skip
    assert (x.method, x.url.path) == ("DELETE", "/trade-api/v2/portfolio/events/orders/o1")
    assert x.url.params["market_ticker"] == o.ticker


async def test_idempotent_create_recovers_by_client_order_id(limits) -> None:
    o = _order()
    posts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posts.append(1)
            return httpx.Response(502, text="bad gateway")
        assert req.url.path.endswith("/portfolio/orders")
        assert req.url.params["ticker"] == o.ticker
        return httpx.Response(
            200,
            json={
                "orders": [
                    {"order_id": "other", "client_order_id": "x"},
                    {"order_id": "o9", "client_order_id": o.client_order_id},
                ],
                "cursor": "",
            },
        )

    async with _client(handler, limits) as api:
        r = await api.create_order_idempotent(o, CENT_GRID, NOW)
    assert r["recovered"] and r["order_id"] == "o9"
    assert len(posts) == 1  # never re-sent


async def test_idempotent_create_unknown_is_raised_not_resent(limits) -> None:
    posts = []
    lookups = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posts.append(1)
            raise httpx.ReadTimeout("timeout", request=req)
        lookups.append(1)
        return httpx.Response(200, json={"orders": [], "cursor": ""})

    async with _client(handler, limits) as api:
        with pytest.raises(OrderOutcomeUnknown):
            await api.create_order_idempotent(_order(), CENT_GRID, NOW)
    assert len(posts) == 1
    assert len(lookups) == 4  # read lag: retried lookups, never a re-send


async def test_idempotent_lookup_survives_read_lag(limits) -> None:
    o = _order()
    lookups = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(504)
        lookups.append(1)
        orders = (
            [] if len(lookups) < 3 else [{"order_id": "o5", "client_order_id": o.client_order_id}]
        )
        return httpx.Response(200, json={"orders": orders, "cursor": ""})

    async with _client(handler, limits) as api:
        r = await api.create_order_idempotent(o, CENT_GRID, NOW)
    assert r["order_id"] == "o5" and len(lookups) == 3


async def test_definite_rejection_is_not_looked_up(limits) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST", "a 400 must not trigger a lookup"
        return httpx.Response(400, json={"code": "post_only_cross"})

    from kalshi_maker.rest import KalshiAPIError

    async with _client(handler, limits) as api:
        with pytest.raises(KalshiAPIError):
            await api.create_order_idempotent(_order(), CENT_GRID, NOW)


async def test_cancel_all_bot_orders_never_touches_manual_orders(limits) -> None:
    bot1, bot2 = new_client_order_id(), new_client_order_id()
    cancelled: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            assert req.url.params["status"] == "resting"
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {"order_id": "b1", "ticker": "T1", "client_order_id": bot1},
                        {"order_id": "m1", "ticker": "T1", "client_order_id": "my-manual"},
                        {"order_id": "m2", "ticker": "T2", "client_order_id": ""},
                        {"order_id": "b2", "ticker": "T2", "client_order_id": bot2},
                    ],
                    "cursor": "",
                },
            )
        assert (req.method, req.url.path) == (
            "DELETE",
            "/trade-api/v2/portfolio/events/orders/batched",
        )
        cancelled.extend(json.loads(req.content)["orders"])
        return httpx.Response(200, json={"orders": [{"order_id": "b1"}, {"order_id": "b2"}]})

    async with _client(handler, limits) as api:
        await api.cancel_all_bot_orders()
    assert cancelled == [
        {"order_id": "b1", "market_ticker": "T1"},
        {"order_id": "b2", "market_ticker": "T2"},
    ]


async def test_batch_create_too_big_refused(limits) -> None:
    async with _client(lambda r: httpx.Response(201, json={"orders": []}), limits) as api:
        orders = [_order() for _ in range(11)]
        with pytest.raises(ValueError, match="split"):
            await api.batch_create_orders(orders, {orders[0].ticker: CENT_GRID}, NOW)


async def test_clock_check_from_date_header(limits) -> None:
    import email.utils
    import time

    def handler(req: httpx.Request) -> httpx.Response:
        fake = email.utils.formatdate(time.time() + 120, usegmt=True)  # exchange 2 min ahead
        return httpx.Response(200, json={"trading_active": True}, headers={"Date": fake})

    async with _client(handler, limits) as api:
        chk = await api.check_clock(samples=3, spacing=0)
    assert chk is not None and chk.exceeds(1.0) and 118 < chk.estimate < 122
