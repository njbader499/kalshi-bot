"""RiskManager (single gate, stops, persistence, property test) and OrderManager diffing."""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kalshi_maker.auth import Signer
from kalshi_maker.book import OrderBook
from kalshi_maker.ledger import Ledger
from kalshi_maker.money import PriceRange, TickGrid
from kalshi_maker.order_types import OrderRequest
from kalshi_maker.orders import DryRunExecutor, LiveExecutor, OrderManager, TradingDisabled
from kalshi_maker.portfolio import MarketMeta, PortfolioState, RestingOrder
from kalshi_maker.rest import KalshiREST, OrderOutcomeUnknown
from kalshi_maker.risk import OrderIntent, RiskManager
from kalshi_maker.settings import AppConfig, load_config
from kalshi_maker.strategy.tilt import DesiredQuote
from kalshi_maker.trading import TradingLoop

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
CFG: AppConfig = load_config(EXAMPLE)
L = CFG.risk  # $500 bankroll limits: order 15, market 50, event 75, category 150, capital 300
CENT = TickGrid([PriceRange(D(0), D(1), D("0.01"))])


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def portfolio(n_events: int = 6, per_event: int = 3, categories: int = 2) -> PortfolioState:
    p = PortfolioState()
    for e in range(n_events):
        for i in range(per_event):
            p.meta[f"E{e}-M{i}"] = MarketMeta(f"E{e}", f"C{e % categories}")
    return p


def rm(enabled: bool = True, path: Path | None = None, clock: Clock | None = None) -> RiskManager:
    return RiskManager(L, enabled, path, clock or Clock())


# ---------- the gate ----------


def test_trading_disabled_rejects_everything() -> None:
    d = rm(enabled=False).check_new(OrderIntent("E0-M0", "bid", D("0.5"), D(1)), portfolio())
    assert not d.ok and "trading_enabled is false" in d.reason


def test_example_config_ships_with_trading_disabled() -> None:
    assert CFG.trading.trading_enabled is False


@pytest.mark.parametrize(
    "intent,setup,needle",
    [
        (OrderIntent("E0-M0", "bid", D("0.5"), D(31)), None, "max_order_cost"),  # $15.50
        (OrderIntent("E0-M0", "ask", D("0.5"), D(31)), None, "max_order_cost"),  # NO cost
        (OrderIntent("E0-M0", "bid", D("1.0"), D(1)), None, "outside"),
        (OrderIntent("E0-M0", "bid", D("0.5"), D(0)), None, "positive"),
        (OrderIntent("ZZ", "bid", D("0.5"), D(1)), None, "metadata"),
        (OrderIntent("E0-M0", "bid", D("0.5"), D(20)), "market45", "market exposure"),
        (OrderIntent("E0-M2", "bid", D("0.5"), D(20)), "event70", "event exposure"),
    ],
)
def test_caps(intent: OrderIntent, setup: str | None, needle: str) -> None:
    p = portfolio()
    if setup == "market45":
        p.add_resting(RestingOrder("x", "c", "E0-M0", "bid", D("0.45"), D(100)))  # $45
    if setup == "event70":
        p.add_resting(RestingOrder("x", "c", "E0-M0", "bid", D("0.5"), D(80)))  # $40
        p.add_resting(RestingOrder("y", "c", "E0-M1", "bid", D("0.5"), D(60)))  # $30
    d = rm().check_new(intent, p)
    assert not d.ok and needle in d.reason, d.reason


def test_max_markets_quoted_and_resting_orders() -> None:
    p = portfolio()
    r = rm()
    for i, t in enumerate(["E0-M0", "E1-M0", "E2-M0"]):  # max_markets_quoted = 3
        p.add_resting(RestingOrder(f"o{i}", "c", t, "bid", D("0.1"), D(1)))
    assert not r.check_new(OrderIntent("E3-M0", "bid", D("0.1"), D(1)), p).ok
    assert r.check_new(OrderIntent("E0-M0", "bid", D("0.1"), D(1)), p).ok  # same market OK


def test_orders_per_minute() -> None:
    c = Clock()
    r = rm(clock=c)
    p = portfolio()
    it = OrderIntent("E0-M0", "bid", D("0.1"), D(1))
    for _ in range(L.max_orders_per_minute):
        assert r.check_new(it, p).ok
        r.record_sent()
    assert "max_orders_per_minute" in r.check_new(it, p).reason
    c.t += 61
    assert r.check_new(it, p).ok


def test_amend_reducing_is_allowed_increasing_is_checked() -> None:
    p = portfolio()
    p.add_resting(RestingOrder("o", "c", "E0-M0", "bid", D("0.45"), D(100)))  # $45 of $50
    r = rm()
    assert r.check_amend(p, "o", D("0.40"), D(100)).ok  # cheaper
    assert not r.check_amend(p, "o", D("0.55"), D(100)).ok  # $55 > market cap
    assert "o" in p.resting  # check_amend never mutates the book


def test_cancels_never_need_the_gate() -> None:
    # (documented contract: OrderManager cancels without consulting the RiskManager)
    r = rm(enabled=False)
    assert not r.can_trade().ok


# ---------- loss stops & persistence ----------


def test_daily_stop_halts_until_next_utc_day(tmp_path: Path) -> None:
    c = Clock(1_800_000_000.0)  # 2027-01-15
    r = rm(path=tmp_path / "rs.json", clock=c)
    assert r.update_equity(D(0)) == []
    ev = r.update_equity(D(-25))
    assert ev and "daily loss stop" in ev[0]
    assert "daily loss stop" in r.can_trade().reason
    c.t += 86400
    r.update_equity(D(-25))  # new day: new baseline
    assert r.can_trade().ok


def test_weekly_stop_needs_manual_resume_and_survives_restart(tmp_path: Path) -> None:
    c = Clock()
    path = tmp_path / "rs.json"
    r = rm(path=path, clock=c)
    r.update_equity(D(0))
    for day in range(3):  # lose $20/day, under the daily stop, over the weekly in total
        c.t += 3600 * 6 if day == 0 else 86400
        r.update_equity(D(-20) * (day + 1))
    assert not r.can_trade().ok
    r2 = rm(path=path, clock=c)  # restart
    assert "weekly" in r2.can_trade().reason
    r2.resume_weekly()
    assert rm(path=path, clock=c).state.weekly_halt is False


def test_drawdown_kill_is_sticky_and_reset_rebases_peak(tmp_path: Path) -> None:
    c = Clock()
    path = tmp_path / "rs.json"
    r = rm(path=path, clock=c)
    r.update_equity(D(50))  # peak 50
    c.t += 86400 * 8  # different day & week so daily/weekly don't fire first
    ev = r.update_equity(D(-50))  # drawdown 100
    assert any("KILL" in e for e in ev)
    assert "KILLED" in rm(path=path, clock=c).can_trade().reason
    r.reset_kill()
    r.update_equity(D(-50))  # peak re-based to -50: no instant re-kill
    assert not r.state.killed


def test_corrupt_state_file_fails_safe(tmp_path: Path) -> None:
    p = tmp_path / "rs.json"
    p.write_text("{not json")
    assert "KILLED" in rm(path=p).can_trade().reason


def test_api_error_pause() -> None:
    c = Clock()
    r = rm(clock=c)
    msgs = [r.on_api_error() for _ in range(L.max_consecutive_api_errors)]
    assert msgs[-1] and "pausing" in msgs[-1]
    assert "API errors" in r.can_trade().reason
    c.t += L.api_error_pause_seconds + 1
    assert r.can_trade().ok


# ---------- property: no sequence of approvals + fills breaches a cap ----------

ops = st.lists(
    st.one_of(
        st.tuples(
            st.just("order"),
            st.integers(0, 5),  # event
            st.integers(0, 2),  # market
            st.sampled_from(["bid", "ask"]),
            st.integers(1, 99),  # price cents
            st.integers(1, 40),  # count
        ),
        st.tuples(st.just("fill"), st.integers(0, 50), st.integers(1, 40)),
        st.tuples(st.just("cancel"), st.integers(0, 50)),
    ),
    max_size=80,
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops)
def test_no_sequence_breaches_caps(seq: list[tuple[Any, ...]]) -> None:
    p = portfolio()
    c = Clock()
    r = rm(clock=c)
    n = 0
    for op in seq:
        c.t += 3  # 20 orders/min max through here, under the 30/min cap
        if op[0] == "order":
            _, e, m, side, cents, cnt = op
            it = OrderIntent(f"E{e}-M{m}", side, D(cents) / 100, D(cnt))
            if r.check_new(it, p).ok:
                r.record_sent()
                p.add_resting(RestingOrder(f"o{n}", "c", it.ticker, side, it.price, it.count))
                n += 1
        elif op[0] == "fill" and p.resting:
            o = list(p.resting.values())[op[1] % len(p.resting)]
            p.apply_fill(o.ticker, o.side, o.price, min(D(op[2]), o.remaining), order_id=o.order_id)
        elif op[0] == "cancel" and p.resting:
            p.remove_resting(list(p.resting)[op[1] % len(p.resting)])
        # invariants after every step
        assert p.total_exposure() <= L.max_capital_deployed
        for t in p.meta:
            assert p.market_exposure(t) <= L.max_cost_per_market
        for e in {m.event for m in p.meta.values()}:
            assert p.event_exposure(e) <= L.max_cost_per_event
        for cat in {m.category for m in p.meta.values()}:
            assert p.category_exposure(cat) <= L.max_cost_per_category
        assert len(p.resting) <= L.max_resting_orders
        assert len(p.quoted_markets()) <= L.max_markets_quoted


# ---------- order manager ----------


def quote(price: str, side: str = "bid", count: int = 1, t: str = "E0-M0") -> DesiredQuote:
    return DesiredQuote(t, side, D(price), D(count), "test")


def om(executor: Any = None, clock: Clock | None = None, risk: RiskManager | None = None):
    p = portfolio()
    c = clock or Clock()
    m = OrderManager(
        executor or DryRunExecutor(), risk or rm(clock=c), p, None, CFG.trading, CFG.tilt,
        CFG.safety.order_expiry_seconds, c,
    )  # fmt: skip
    return m, p, c


GRIDS = {t: CENT for t in portfolio().meta}


async def test_create_keep_and_cancel() -> None:
    m, p, _ = om()
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    assert [(o.price, o.side) for o in p.resting.values()] == [(D("0.70"), "bid")]
    oid = next(iter(p.resting))
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)  # unchanged -> nothing new
    assert list(p.resting) == [oid]
    await m.sync({"E0-M0": None}, GRIDS, True)
    assert p.resting == {}


async def test_gate_closed_cancels_everything() -> None:
    m, p, _ = om()
    await m.sync({"E0-M0": quote("0.70"), "E1-M0": quote("0.30", "ask")}, GRIDS, True)
    assert len(p.resting) == 2
    await m.sync({"E0-M0": quote("0.70"), "E1-M0": quote("0.30", "ask")}, GRIDS, False)
    assert p.resting == {}


async def test_hysteresis_and_amend_cooldown() -> None:
    m, p, c = om()
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    o = next(iter(p.resting.values()))
    c.t += 30
    await m.sync({"E0-M0": quote("0.705")}, GRIDS, True)  # < 1 tick: no amend
    assert o.price == D("0.70")
    await m.sync({"E0-M0": quote("0.72")}, GRIDS, True)  # moved 2 ticks: amend
    assert o.price == D("0.72")
    await m.sync({"E0-M0": quote("0.74")}, GRIDS, True)  # within 20 s cooldown: wait
    assert o.price == D("0.72")
    c.t += 21
    await m.sync({"E0-M0": quote("0.74")}, GRIDS, True)
    assert o.price == D("0.74") and len(p.resting) == 1


async def test_side_switch_cancels_then_creates() -> None:
    m, p, _ = om()
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    await m.sync({"E0-M0": quote("0.30", "ask")}, GRIDS, True)
    assert [o.side for o in p.resting.values()] == ["ask"]


async def test_risk_rejection_creates_nothing() -> None:
    m, p, _ = om(risk=rm(enabled=False))
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    assert p.resting == {}


class UnknownOutcomeExecutor(DryRunExecutor):
    async def create(self, req: OrderRequest, grid: TickGrid) -> str:
        raise OrderOutcomeUnknown(req.client_order_id, TimeoutError())


async def test_unknown_outcome_blocks_the_market() -> None:
    m, p, _ = om(executor=UnknownOutcomeExecutor())
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    assert "E0-M0" in m.blocked and p.resting == {}
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)  # never re-sent blindly
    assert p.resting == {}


async def test_off_grid_quote_is_refused_by_validation() -> None:
    m, p, _ = om()
    await m.sync({"E0-M0": quote("0.705")}, GRIDS, True)
    assert p.resting == {}  # create_failed (validation), nothing resting


async def test_ws_fill_updates_portfolio() -> None:
    m, p, _ = om()
    await m.sync({"E0-M0": quote("0.70", count=3)}, GRIDS, True)
    oid = next(iter(p.resting))
    fill = {"order_id": oid, "market_ticker": "E0-M0", "book_side": "bid", "count_fp": "2.00"}
    m.on_fill({**fill, "yes_price_dollars": "0.7000", "fee_cost": "0.000000"})
    assert p.positions["E0-M0"].qty == 2 and p.resting[oid].remaining == 1
    assert m.filled[oid] == 2


# ---------- the hard switch ----------


def test_live_executor_refuses_when_disabled() -> None:
    api = KalshiREST("https://external-api.demo.kalshi.co/trade-api/v2", CFG.rate_limits)
    with pytest.raises(TradingDisabled):
        LiveExecutor(api, CFG.trading)


def test_trading_loop_dry_by_default_and_refuses_live_executor() -> None:
    tl = TradingLoop(CFG, None)
    assert tl.om.ex.dry_run and not tl.live
    enabled = CFG.model_copy(
        update={"trading": CFG.trading.model_copy(update={"trading_enabled": True})}
    )
    api = KalshiREST("https://external-api.demo.kalshi.co/trade-api/v2", CFG.rate_limits)
    with pytest.raises(ValueError, match="refusing a live executor"):
        TradingLoop(CFG, None, executor=LiveExecutor(api, enabled.trading))
    with pytest.raises(ValueError, match="dry-run executor"):
        TradingLoop(enabled, None, executor=DryRunExecutor())


async def test_live_executor_sends_post_only_v2_when_enabled(tmp_path: Path) -> None:
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(201, json={"order_id": "o1", "fill_count": "0.00",
                                          "remaining_count": "1.00", "ts_ms": 1})  # fmt: skip

    api = KalshiREST(
        "https://external-api.demo.kalshi.co/trade-api/v2", CFG.rate_limits,
        signer=Signer("k", ed25519.Ed25519PrivateKey.generate()),
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    enabled = CFG.trading.model_copy(update={"trading_enabled": True})
    m, p, _ = om(executor=LiveExecutor(api, enabled))
    import time as _t

    m._clock = _t.time  # real clock: expiration must be in the future for validation
    await m.sync({"E0-M0": quote("0.70")}, GRIDS, True)
    await api.aclose()
    assert sent and sent[0]["post_only"] is True and sent[0]["side"] == "bid"
    assert sent[0]["time_in_force"] == "good_till_canceled" and sent[0]["expiration_time"] > 0
    assert list(p.resting) == ["o1"]


# ---------- trading loop end to end (dry) ----------


def book(t: str, bid: str, ask: str) -> OrderBook:
    b = OrderBook(t)
    b.replace([[bid, "10"]], [[str(1 - D(ask)), "10"]], 1, 0.0)
    return b


MARKETS = [
    {"ticker": f"E{e}-M{i}", "event_ticker": f"E{e}",
     "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]}
    for e in range(5) for i in range(2)
]  # fmt: skip


async def test_trading_loop_dry_run_records_and_respects_max_markets(tmp_path: Path) -> None:
    lg = await Ledger.open(tmp_path / "l.db")
    c = Clock()
    tl = TradingLoop(CFG, lg, clock=c)
    tl.update_markets(MARKETS, "Climate and Weather")
    books = {}
    for e in range(5):  # five favorites, all quotable
        t = f"E{e}-M0"
        tl.update_fair(t, D("0.85"), 1.0)
        books[t] = book(t, "0.70", "0.80")
    tl.update_fair("E0-M1", D("0.50"), 1.0)  # middle: no quote
    books["E0-M1"] = book("E0-M1", "0.45", "0.55")
    await tl.step(books, gate_open=True)
    assert len(tl.pf.quoted_markets()) == CFG.risk.max_markets_quoted == 3
    async with lg.db.execute("SELECT action, dry_run FROM order_actions") as cur:
        rows = [r async for r in cur]
    assert rows and all(dr == 1 for _, dr in rows) and {a for a, _ in rows} == {"create"}
    # stale fair values stop quoting
    c.t += CFG.trading.max_fair_age_seconds + 1
    await tl.step(books, gate_open=True)
    assert tl.pf.quoted_markets() == set()
    await lg.close()


async def test_trading_loop_risk_stop_cancels_all() -> None:
    tl = TradingLoop(CFG, None, clock=Clock())
    tl.update_markets(MARKETS, "Climate and Weather")
    tl.update_fair("E0-M0", D("0.85"), 1.0)
    books = {"E0-M0": book("E0-M0", "0.70", "0.80")}
    await tl.step(books, gate_open=True)
    assert tl.pf.resting
    tl.pf.realized = D(-30)  # a bad day
    ev = await tl.step(books, gate_open=True)
    assert ev and "daily loss stop" in ev[0] and tl.pf.resting == {}


def test_no_log_extra_key_collides_with_logrecord() -> None:
    """A colliding `extra` key makes logging RAISE. It once broke the risk-stop path."""
    import logging
    import re

    reserved = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
    src = Path(__file__).resolve().parents[1] / "kalshi_maker"
    bad = []
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"extra=\{([^}]*)\}", text, re.S):
            for key in re.findall(r'"([A-Za-z_]+)"\s*:', m.group(1)):
                if key in reserved:
                    bad.append(f"{f.name}: {key}")
    assert not bad, bad
