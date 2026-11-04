"""Order book math and WebSocket seq handling (gap -> snapshot -> replace)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from hypothesis import given
from hypothesis import strategies as st

from kalshi_maker.auth import Signer
from kalshi_maker.book import BookError, OrderBook, book_from_rest
from kalshi_maker.ws import KalshiWS, WSCallbacks

T = "KXHIGHNY-26NOV05-T60"
T2 = "KXHIGHNY-26NOV05-T62"


# ---------- book ----------


def test_yes_no_conversion() -> None:
    b = OrderBook(T)
    b.replace([["0.40", "10"], ["0.42", "5"]], [["0.55", "7"], ["0.50", "1"]], 1, 0.0)
    assert b.best_yes_bid == D("0.42")
    assert b.best_yes_ask == D("0.45")  # 1 - best NO bid 0.55
    assert b.best_no_ask == D("0.58")  # 1 - best YES bid
    assert b.mid == D("0.435")
    assert b.size_at_yes_ask() == D(7)
    assert not b.crossed


def test_no_side_in_yes_leg_pricing_converts_back() -> None:
    a, b = OrderBook(T), OrderBook(T)
    a.replace([], [["0.55", "7"]], 1, 0.0)  # REST / legacy WS: NO-leg
    b.replace([], [["0.45", "7"]], 1, 0.0, no_in_yes_price=True)  # use_yes_price: YES-leg
    assert a.no == b.no
    b.apply_delta("no", "0.45", "-2", 2, 0.0, no_in_yes_price=True)
    assert b.no == {D("0.55"): D(5)}


def test_delta_removes_level_at_zero_and_rejects_negative() -> None:
    b = OrderBook(T)
    b.replace([["0.40", "10"]], [], 1, 0.0)
    b.apply_delta("yes", "0.40", "-10", 2, 0.0)
    assert b.yes == {} and b.best_yes_bid is None
    with pytest.raises(BookError):
        b.apply_delta("yes", "0.30", "-1", 3, 0.0)
    assert not b.valid  # disagreement with the exchange -> needs resync


def test_delta_on_invalid_book_refused() -> None:
    with pytest.raises(BookError):
        OrderBook(T).apply_delta("yes", "0.40", "1", 1, 0.0)


def test_snapshot_replaces_not_merges() -> None:
    b = OrderBook(T)
    b.replace([["0.40", "10"], ["0.10", "3"]], [], 1, 0.0)
    b.replace([["0.41", "1"]], [], 2, 0.0)
    assert b.yes == {D("0.41"): D(1)}


def test_subpenny_levels() -> None:
    b = OrderBook(T)
    b.replace([["0.0050", "100"]], [["0.9950", "2"]], 1, 0.0)
    assert b.best_yes_bid == D("0.005") and b.best_yes_ask == D("0.005")
    assert b.crossed  # locked book flagged


def test_rest_parse() -> None:
    b = book_from_rest(
        T, {"orderbook_fp": {"yes_dollars": [["0.1", "1.00"]], "no_dollars": None}}, 0
    )
    assert b.valid and b.best_yes_bid == D("0.1") and b.best_yes_ask is None
    with pytest.raises(BookError):
        book_from_rest(T, {}, 0)


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["yes", "no"]),
            st.integers(1, 99),
            st.integers(-50, 50),
        ),
        max_size=60,
    )
)
def test_book_never_negative_and_floats_never_used(ops) -> None:
    b = OrderBook(T)
    b.replace([], [], 0, 0.0)
    for i, (side, cents, delta) in enumerate(ops):
        if not b.valid:
            b.replace([], [], i, 0.0)
        try:
            b.apply_delta(side, f"0.{cents:02d}", str(delta), i, 0.0)
        except BookError:
            continue
        for lv in (b.yes, b.no):
            assert all(isinstance(p, D) and isinstance(s, D) and s > 0 for p, s in lv.items())


# ---------- websocket handler ----------


def _ws(**kw) -> KalshiWS:
    return KalshiWS(
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        Signer("kid", ed25519.Ed25519PrivateKey.generate()),
        [T, T2],
        mono=lambda: 100.0,
        wall=lambda: 1_800_000_000.0,
        **kw,
    )


def _snap(seq: int, ticker: str = T, yes=None, no=None, sid: int = 1) -> str:
    msg: dict = {"market_ticker": ticker, "market_id": "x", "clock": seq}
    if yes is not None:
        msg["yes_dollars_fp"] = yes
    if no is not None:
        msg["no_dollars_fp"] = no
    return json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg})


def _delta(seq: int, price: str, delta: str, side: str = "yes", ticker: str = T) -> str:
    return json.dumps(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": seq,
            "msg": {
                "market_ticker": ticker,
                "market_id": "x",
                "clock": 1000 + seq,
                "price_dollars": price,
                "delta_fp": delta,
                "side": side,
            },
        }
    )


def _subscribed(ws: KalshiWS) -> None:
    cmds = ws.subscribe_commands()
    assert cmds[0]["params"] == {
        "channels": ["orderbook_delta"],
        "market_tickers": [T, T2],
        "use_yes_price": True,
    }
    assert [c["params"]["channels"] for c in cmds[1:]] == [["fill"], ["user_orders"]]
    ws.handle(
        json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}})
    )


def test_snapshot_then_deltas_in_order() -> None:
    ws = _ws()
    _subscribed(ws)
    assert ws.handle(_snap(1, T, yes=[["0.40", "10"]], no=[["0.45", "3"]])) == []
    assert ws.handle(_snap(2, T2, yes=[], no=[])) == []
    assert ws.handle(_delta(3, "0.40", "-4")) == []
    b = ws.books[T]
    assert b.yes == {D("0.40"): D(6)}
    assert b.no == {D("0.55"): D(3)}  # yes-leg 0.45 -> no-leg 0.55
    assert b.best_yes_ask == D("0.45")


def test_seq_gap_invalidates_all_and_requests_snapshot() -> None:
    gaps: list[str] = []
    ws = _ws(callbacks=WSCallbacks(on_gap=gaps.append))
    _subscribed(ws)
    ws.handle(_snap(1, T, yes=[["0.40", "10"]]))
    ws.handle(_snap(2, T2, yes=[["0.20", "1"]]))
    cmds = ws.handle(_delta(5, "0.40", "-1"))  # 3 and 4 missing
    assert gaps and ws.stats.gaps == 1
    assert cmds == [
        {
            "id": cmds[0]["id"],
            "cmd": "update_subscription",
            "params": {"sids": [1], "market_tickers": [T, T2], "action": "get_snapshot"},
        }
    ]
    assert not ws.books[T].valid and not ws.books[T2].valid
    # deltas while waiting for the snapshot are dropped, never applied to a stale book
    ws.handle(_delta(6, "0.40", "+100"))
    assert not ws.books[T].valid
    # fresh snapshot REPLACES the book
    ws.handle(_snap(7, T, yes=[["0.41", "2"]]))
    assert ws.books[T].valid and ws.books[T].yes == {D("0.41"): D(2)}
    ws.handle(_delta(8, "0.41", "1"))
    assert ws.books[T].yes == {D("0.41"): D(3)}
    assert not ws.books[T2].valid  # still waiting for its own snapshot


def test_duplicate_seq_is_a_gap() -> None:
    ws = _ws()
    _subscribed(ws)
    ws.handle(_snap(1, T, yes=[]))
    ws.handle(_delta(2, "0.40", "1"))
    ws.handle(_delta(2, "0.40", "1"))  # replayed message
    assert ws.stats.gaps == 1 and not ws.books[T].valid


def test_bad_delta_resyncs_only_that_market() -> None:
    ws = _ws()
    _subscribed(ws)
    ws.handle(_snap(1, T, yes=[]))
    ws.handle(_snap(2, T2, yes=[]))
    cmds = ws.handle(_delta(3, "0.40", "-5"))  # would go negative
    assert cmds[0]["params"]["market_tickers"] == [T]
    assert not ws.books[T].valid and ws.books[T2].valid


def test_unknown_and_untracked_messages_ignored() -> None:
    ws = _ws()
    _subscribed(ws)
    ws.handle(_snap(1, T, yes=[]))
    assert ws.handle(json.dumps({"type": "brand_new_type", "msg": {}})) == []
    assert ws.handle(_delta(2, "0.40", "1", ticker="OTHER-MARKET")) == []  # seq still counted
    assert ws.handle(_delta(3, "0.40", "1")) == []
    assert ws.stats.gaps == 0 and ws.books[T].yes == {D("0.40"): D(1)}
    assert ws.handle(b"not json") == []


def test_fills_and_orders_routed_and_clock_lead() -> None:
    got: list = []
    ws = _ws(callbacks=WSCallbacks(on_fill=got.append, on_user_order=got.append))
    ws.handle(
        json.dumps({"type": "fill", "sending_ts_ms": 1_800_000_000_500, "msg": {"trade_id": "t"}})
    )
    ws.handle(json.dumps({"type": "user_order", "msg": {"order_id": "o"}}))
    assert got == [{"trade_id": "t"}, {"order_id": "o"}]
    assert ws.stats.clock_lead_lower_bound == pytest.approx(0.5)


def test_subscribe_error_raises_for_reconnect() -> None:
    ws = _ws()
    ws.subscribe_commands()
    with pytest.raises(ConnectionError):
        ws.handle(
            json.dumps({"type": "error", "id": 1, "msg": {"code": 8, "msg": "Unknown channel"}})
        )


class FakeConn:
    def __init__(self, script: list[str | Exception]) -> None:
        self.script = script
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if not self.script:
            raise ConnectionError("server went away")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


async def test_run_reconnects_and_reports_connection() -> None:
    events: list[tuple[bool, str]] = []
    conns = [
        FakeConn([_snap(1, T, yes=[["0.4", "1"]])]),
        FakeConn([_snap(1, T, yes=[["0.5", "1"]])]),
    ]
    headers_seen: list[dict] = []
    stop = asyncio.Event()

    async def connect(url: str, headers: dict) -> FakeConn:
        headers_seen.append(headers)
        if not conns:
            stop.set()
            raise ConnectionError("no more")
        return conns.pop(0)

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    ws = KalshiWS(
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        Signer("kid", ed25519.Ed25519PrivateKey.generate()),
        [T],
        callbacks=WSCallbacks(on_connection=lambda up, why: events.append((up, why))),
        connect=connect,
        sleep=no_sleep,
    )
    await asyncio.wait_for(ws.run(stop), timeout=5)
    assert [e[0] for e in events] == [True, False, True, False, False]
    assert ws.stats.connects == 2
    assert all("KALSHI-ACCESS-SIGNATURE" in h for h in headers_seen)
    assert not ws.books[T].valid  # invalidated on disconnect
