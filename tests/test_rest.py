from __future__ import annotations

import base64
import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_maker.auth import HDR_SIG, HDR_TS, Signer
from kalshi_maker.rest import Balance, KalshiAPIError, KalshiREST, OrderbookLevels, TokenBucket
from kalshi_maker.settings import load_config

BASE = "https://external-api.demo.kalshi.co/trade-api/v2"


@pytest.fixture
def limits(cfg_file: Path):
    return load_config(cfg_file).rate_limits


async def _noop_sleep(_: float) -> None:
    return None


def _client(handler, limits, signer=None) -> KalshiREST:
    return KalshiREST(
        BASE, limits, signer=signer, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )


async def test_auth_headers_sign_full_path_without_query(limits) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["req"] = req
        return httpx.Response(200, json={"orders": [], "cursor": ""})

    async with _client(handler, limits, Signer("kid", key)) as api:
        _ = [o async for o in api.iter_orders(status="resting")]
    req = seen["req"]
    assert req.url.path == "/trade-api/v2/portfolio/orders"
    assert req.url.params["status"] == "resting"
    msg = req.headers[HDR_TS] + "GET" + "/trade-api/v2/portfolio/orders"
    key.public_key().verify(
        base64.b64decode(req.headers[HDR_SIG]),
        msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


async def test_public_calls_unsigned(limits) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert HDR_SIG not in req.headers
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    async with _client(handler, limits) as api:
        assert (await api.exchange_status())["trading_active"] is True


async def test_auth_without_signer_fails_loudly(limits) -> None:
    async with _client(lambda r: httpx.Response(200, json={}), limits) as api:
        with pytest.raises(RuntimeError):
            await api.get_balance()


async def test_get_retries_on_429_then_succeeds(limits) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"market": {"ticker": "T"}})

    async with _client(handler, limits) as api:
        assert (await api.get_market("T"))["ticker"] == "T"
        assert api.consecutive_errors == 0
    assert len(calls) == 3


async def test_get_gives_up_after_max_retries(limits) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="down")

    async with _client(handler, limits) as api:
        with pytest.raises(KalshiAPIError) as ei:
            await api.get_market("T")
        assert ei.value.status == 503
        assert api.consecutive_errors == limits.max_retries + 1
    assert len(calls) == limits.max_retries + 1


async def test_4xx_not_retried(limits) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="nope")

    async with _client(handler, limits) as api:
        with pytest.raises(KalshiAPIError):
            await api.get_market("T")
    assert len(calls) == 1


async def test_non_get_never_blind_retried(limits) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    async with _client(handler, limits) as api:
        with pytest.raises(KalshiAPIError):
            await api.request("POST", "/portfolio/orders", json={})
    assert len(calls) == 1


async def test_pagination_follows_cursor(limits) -> None:
    pages = {None: (["a", "b"], "c1"), "c1": (["c"], "c2"), "c2": (["d"], "")}

    def handler(req: httpx.Request) -> httpx.Response:
        items, nxt = pages[req.url.params.get("cursor")]
        return httpx.Response(200, json={"markets": [{"ticker": t} for t in items], "cursor": nxt})

    async with _client(handler, limits) as api:
        got = [m["ticker"] async for m in api.iter_markets(status="open")]
    assert got == ["a", "b", "c", "d"]


def test_orderbook_parse_decimal_and_sorted() -> None:
    ob = OrderbookLevels.parse(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4200", "10.00"], ["0.0500", "278.00"], ["0.4250", "3.50"]],
                "no_dollars": [["0.5500", "1.00"]],
            }
        }
    )
    assert ob.best_yes_bid == D("0.425") and ob.yes_bids[0] == (D("0.05"), D(278))
    assert ob.best_no_bid == D("0.55")
    empty = OrderbookLevels.parse({"orderbook_fp": {"yes_dollars": None, "no_dollars": []}})
    assert empty.best_yes_bid is None and empty.best_no_bid is None
    with pytest.raises(ValueError):
        OrderbookLevels.parse({"orderbook": {}})


def test_balance_parse_prefers_dollars() -> None:
    assert Balance.parse({"balance": 50012, "balance_dollars": "500.1234"}).balance == D("500.1234")
    assert Balance.parse({"balance": 50012}).balance == D("500.12")
    assert Balance.parse({"balance": 1, "portfolio_value": 2500}).portfolio_value == D(25)
    assert Balance.parse({"balance_dollars": "1.00"}).portfolio_value is None
    with pytest.raises(ValueError):
        Balance.parse({})


def test_token_bucket_math() -> None:
    t = [0.0]
    b = TokenBucket(100, 100, clock=lambda: t[0])
    assert b.try_take(60) == 0
    wait = b.try_take(60)
    assert wait == pytest.approx(0.2)
    t[0] += 0.2
    assert b.try_take(60) == 0
    with pytest.raises(ValueError):
        b.try_take(1000)


def test_no_float_in_parsed_json_numbers() -> None:
    # Guard: exchange prices arrive as strings; a float would be refused by to_decimal.
    from kalshi_maker.money import MoneyError, to_decimal

    with pytest.raises(MoneyError):
        to_decimal(json.loads('{"p": 0.42}')["p"])
