"""Opt-in live checks against the Kalshi DEMO API.

Run (PowerShell):
    $env:KALSHI_INTEGRATION = "1"; .\\.venv\\Scripts\\python.exe -m pytest -m integration -v
    Remove-Item Env:KALSHI_INTEGRATION

Needs config/config.yaml and .env with demo credentials. Refuses to run against prod.
The order test places ONE post-only YES bid of 1 contract at the market's lowest tick (far
below the market), amends its expiry, cancels it and checks it's gone. Worst case it
costs one cent of demo money.
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_maker.cli import _signer
from kalshi_maker.money import TickGrid
from kalshi_maker.order_types import BookSide, OrderRequest, new_client_order_id
from kalshi_maker.rest import KalshiREST
from kalshi_maker.settings import Env, load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KALSHI_INTEGRATION") != "1", reason="opt-in"),
]


@pytest.fixture
def settings():
    s = load_settings(Path("config/config.yaml"), Path(".env"))
    assert s.env is Env.DEMO, "integration tests only run against demo"
    assert "demo" in s.hosts.rest
    return s


@pytest.fixture
async def api(settings):
    async with KalshiREST(
        settings.hosts.rest, settings.config.rate_limits, signer=_signer(settings)
    ) as c:
        yield c


async def test_public_read(api) -> None:
    assert "trading_active" in await api.exchange_status()
    n = 0
    async for m in api.iter_markets(status="open", limit=5):
        ob = await api.get_orderbook(m["ticker"])
        assert ob is not None
        n += 1
        if n >= 2:
            break


async def test_auth_balance_and_limits(api) -> None:
    bal = await api.get_balance()
    assert bal.balance >= 0
    tier = await api.apply_account_limits()
    assert tier
    assert await api.refresh_endpoint_costs() > 0


async def test_clock_within_tolerance(api, settings) -> None:
    chk = await api.check_clock()
    assert chk is not None
    assert not chk.exceeds(settings.config.safety.clock_drift_warn_seconds)


async def _far_market(api, series: list[str]) -> tuple[str, TickGrid]:
    """An open market in the configured series whose YES ask is above the lowest tick."""
    for st in series:
        async for m in api.iter_markets(series_ticker=st, status="open", limit=200):
            try:
                grid = TickGrid.from_market(m)
            except Exception:
                continue
            ob = await api.get_orderbook(m["ticker"])
            best_no = ob.best_no_bid
            # YES ask = 1 - best NO bid; a bid at min_price must stay strictly below it.
            if best_no is None or (1 - best_no) > grid.min_price:
                return m["ticker"], grid
    pytest.skip(f"no suitable open market in {series} on demo right now")


async def _wait_order(api, oid: str, pred, timeout: float = 15.0) -> dict:
    """REST reads lag the matching engine (docs: get-user-data-timestamp), so poll."""
    import asyncio

    from kalshi_maker.rest import KalshiAPIError

    deadline = time.monotonic() + timeout
    while True:
        try:
            o = await api.get_order(oid)
        except KalshiAPIError as e:
            if e.status != 404 or time.monotonic() > deadline:
                raise
            o = {}  # not visible to reads yet
        if (o and pred(o)) or time.monotonic() > deadline:
            return o
        await asyncio.sleep(0.5)


async def test_post_only_order_lifecycle(api, settings) -> None:
    bal = await api.get_balance()
    if bal.balance < Decimal("0.05"):
        pytest.skip(
            f"demo balance is ${bal.balance}: add demo funds on demo.kalshi.co to run the "
            "order round-trip (it costs at most a cent)"
        )
    ticker, grid = await _far_market(api, settings.config.markets.weather_series)
    exp = int(time.time()) + settings.config.safety.order_expiry_seconds
    order = OrderRequest(
        ticker=ticker,
        side=BookSide.BID,
        price=grid.min_price,
        count=Decimal(1),
        client_order_id=new_client_order_id(),
        expiration_time=exp,
    )
    created = await api.create_order_idempotent(order, grid)
    oid = created["order_id"]
    cancelled = None
    try:
        assert Decimal(created["fill_count"]) == 0, "post-only bid must not fill on placement"
        assert Decimal(created["remaining_count"]) == 1
        live = await _wait_order(api, oid, lambda o: o.get("status") == "resting")
        assert live["client_order_id"] == order.client_order_id
        assert live["status"] == "resting"
        assert Decimal(live["yes_price_dollars"]) == order.price
        # expiry-only amend: same price + total count, later expiration (keeps queue spot)
        amended = await api.amend_order(
            oid,
            ticker=ticker,
            side="bid",
            price=order.price,
            count=order.count,
            expiration_time=exp + 120,
        )
        assert amended["order_id"] == oid
        want = datetime.fromtimestamp(exp + 120, UTC)
        live = await _wait_order(api, oid, lambda o: _parse(o.get("expiration_time")) == want)
        assert _parse(live.get("expiration_time")) == want, live.get("expiration_time")
        assert live["status"] == "resting"
    finally:
        cancelled = await api.cancel_order(oid, ticker)
    # the cancel response is authoritative: 1 contract removed from the book
    assert cancelled["order_id"] == oid
    assert Decimal(cancelled["reduced_by"]) == 1
    after = await _wait_order(api, oid, lambda o: o.get("status") == "canceled")
    assert after["status"] == "canceled"
    resting = [o async for o in api.iter_orders(ticker=ticker, status="resting")]
    assert oid not in {o["order_id"] for o in resting}


def _parse(v):
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


async def test_ws_books_match_rest(api, settings) -> None:
    """WS (use_yes_price=true, converted back) must equal the REST book (NO-leg prices)."""
    import asyncio

    from kalshi_maker.book import book_from_rest
    from kalshi_maker.ws import KalshiWS

    tickers = []
    async for m in api.iter_markets(series_ticker="KXHIGHNY", status="open", limit=50):
        tickers.append(m["ticker"])
    assert tickers
    ws = KalshiWS(settings.hosts.ws, _signer(settings), tickers[:6], user_channels=True)
    stop = asyncio.Event()
    task = asyncio.create_task(ws.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.2)
            if all(b.valid for b in ws.books.values()):
                break
        assert all(b.valid for b in ws.books.values()), "no snapshots"
        mismatches = []
        for t in tickers[:6]:
            for _attempt in range(3):  # a book can tick between the two reads; retry
                rest = book_from_rest(t, await api.request("GET", f"/markets/{t}/orderbook"), 0)
                wsb = ws.books[t]
                if rest.yes == wsb.yes and rest.no == wsb.no:
                    break
                await asyncio.sleep(0.5)
            else:
                mismatches.append((t, rest.summary(), wsb.summary()))
        assert not mismatches, mismatches
        assert ws.stats.gaps == 0
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
