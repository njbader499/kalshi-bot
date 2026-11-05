"""Market blend + disagreement guard, symmetric (two-sided) quoting, the per-side order
diff, and the haircut mark-to-market that replaces the 'empty book = worthless' mark.

Motivating incidents (2026-11-04/05, demo): the untuned model priced the market's favourite
brackets at 0.11-0.22 against a real market of 0.5-0.76 and went short all of them; and the
risk stops fired on a $98 'loss' that was the cost basis of positions marked at 0 because
the demo book had no bids, with zero settlements.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from kalshi_maker.book import OrderBook
from kalshi_maker.money import PriceRange, TickGrid
from kalshi_maker.orders import DryRunExecutor, OrderManager
from kalshi_maker.portfolio import MarketMeta, PortfolioState
from kalshi_maker.risk import RiskManager
from kalshi_maker.settings import AppConfig, MarketBlendCfg, SymmetricCfg, load_config
from kalshi_maker.strategy.symmetric import SymView, symmetric_quotes
from kalshi_maker.strategy.tilt import DesiredQuote
from kalshi_maker.trading import MARK_HAIRCUT, TradingLoop, blended_fair

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
CFG: AppConfig = load_config(EXAMPLE)
GRID = TickGrid([PriceRange(D("0"), D("1"), D("0.01"))])
MARKETS = [
    {"ticker": f"E{e}-M{i}", "event_ticker": f"E{e}",
     "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]}
    for e in range(5) for i in range(2)
]  # fmt: skip


def book(t: str, bid: str | None, ask: str | None) -> OrderBook:
    b = OrderBook(t)
    yes = [[bid, "10"]] if bid else []
    no = [[str(1 - D(ask)), "10"]] if ask else []
    b.replace(yes, no, 1, 0.0)
    return b


def with_(cfg: AppConfig, **sections: object) -> AppConfig:
    return cfg.model_copy(update=sections)


# ---------- blended fair ----------


def test_blend_weights_toward_the_market_and_stays_inside_the_spread() -> None:
    f, how = blended_fair(D("0.60"), D("0.70"), D("0.72"), MarketBlendCfg(weight=D("0.6")))
    assert how == "blend" and f == D("0.70")  # 0.6*0.71 + 0.4*0.60 = 0.666 -> clamped to bid
    f2, _ = blended_fair(D("0.71"), D("0.70"), D("0.72"), MarketBlendCfg(weight=D("0.6")))
    assert f2 == D("0.71")  # inside the spread: unchanged
    f3, _ = blended_fair(D("0.80"), D("0.70"), D("0.72"), MarketBlendCfg(weight=D("0.6")))
    assert f3 == D("0.72")  # leaning above the market is clamped to the ask


def test_disagreement_blocks_the_quote() -> None:
    f, how = blended_fair(D("0.11"), D("0.75"), D("0.77"), MarketBlendCfg())
    assert f is None and how.startswith("disagree")


@pytest.mark.parametrize("bid,ask", [(None, None), (D("0.5"), None), (D("0.6"), D("0.5"))])
def test_no_usable_reference(bid: D | None, ask: D | None) -> None:
    assert blended_fair(D("0.5"), bid, ask, MarketBlendCfg()) == (None, "no_ref")
    assert blended_fair(D("0.5"), bid, ask, MarketBlendCfg(model_only_ok=True))[1] == "model_only"


def test_wide_reference_is_not_a_price() -> None:
    assert blended_fair(D("0.5"), D("0.30"), D("0.70"), MarketBlendCfg()) == (None, "wide_ref")


# ---------- symmetric quotes ----------


def sym(**kw: object) -> SymmetricCfg:
    base = {"enabled": True, "half_spread": D("0.02"), "min_market_spread_ticks": 3,
            "max_inventory": D("30"), "skew_per_contract": D("0.001"), "min_price": D("0.05")}
    return SymmetricCfg(**{**base, **kw})  # type: ignore[arg-type]


def test_two_quotes_inside_the_spread_never_crossing() -> None:
    v = SymView("T", D("0.50"), D("0.45"), D("0.55"), GRID)
    qs = symmetric_quotes(v, sym(), D(1))
    assert [(q.side, q.price) for q in qs] == [("bid", D("0.48")), ("ask", D("0.52"))]
    assert all(q.count == 1 for q in qs)


def test_quotes_are_clamped_inside_the_book() -> None:
    # fair sits at the ask: bid stays a tick under the ask, ask moves up to keep its edge
    v = SymView("T", D("0.55"), D("0.45"), D("0.55"), GRID)
    qs = symmetric_quotes(v, sym(), D(1))
    assert [(q.side, q.price) for q in qs] == [("bid", D("0.53")), ("ask", D("0.57"))]
    # fair below the best bid: the YES bid sits deeper in the book, the ask improves the
    # best bid by a tick and still keeps its full edge
    v2 = SymView("T", D("0.40"), D("0.45"), D("0.55"), GRID)
    qs2 = symmetric_quotes(v2, sym(), D(1))
    assert [(q.side, q.price) for q in qs2] == [("bid", D("0.38")), ("ask", D("0.46"))]


def test_tight_book_leaves_no_room() -> None:
    v = SymView("T", D("0.50"), D("0.49"), D("0.51"), GRID)
    assert symmetric_quotes(v, sym(), D(1)) == []
    assert symmetric_quotes(v, sym(min_market_spread_ticks=2), D(1)) != []


def test_one_sided_or_crossed_book_is_skipped() -> None:
    assert symmetric_quotes(SymView("T", D("0.5"), None, D("0.6"), GRID), sym(), D(1)) == []
    assert symmetric_quotes(SymView("T", D("0.5"), D("0.6"), D("0.6"), GRID), sym(), D(1)) == []


def test_inventory_skews_and_then_caps_a_side() -> None:
    flat = symmetric_quotes(SymView("T", D("0.50"), D("0.40"), D("0.60"), GRID), sym(), D(1))
    long_yes = symmetric_quotes(
        SymView("T", D("0.50"), D("0.40"), D("0.60"), GRID, position=D(20)), sym(), D(1)
    )
    # long 20 YES at 0.001/contract: the YES bid drops the full 2c; the ask (the reducing
    # side) tightens but keeps at least half its edge above fair: 0.52 -> 0.51
    assert flat[0].price - long_yes[0].price == D("0.02")
    assert (flat[1].price, long_yes[1].price) == (D("0.52"), D("0.51"))
    capped = symmetric_quotes(
        SymView("T", D("0.50"), D("0.40"), D("0.60"), GRID, position=D(30)), sym(), D(1)
    )
    assert [q.side for q in capped] == ["ask"]  # only the reducing side
    short_capped = symmetric_quotes(
        SymView("T", D("0.50"), D("0.40"), D("0.60"), GRID, position=D(-30)), sym(), D(1)
    )
    assert [q.side for q in short_capped] == ["bid"]


def test_price_floor_and_cap() -> None:
    v = SymView("T", D("0.04"), D("0.01"), D("0.10"), GRID)
    assert [q.side for q in symmetric_quotes(v, sym(), D(1))] == ["ask"]  # YES bid < 0.05
    assert symmetric_quotes(v, sym(enabled=False), D(1)) == []


# ---------- order manager: per-side diff ----------


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def om_with_pf() -> tuple[OrderManager, PortfolioState]:
    pf = PortfolioState()
    pf.meta["T"] = MarketMeta("E", "Climate and Weather")
    risk = RiskManager(CFG.risk, True, None, Clock())
    om = OrderManager(DryRunExecutor(), risk, pf, None, CFG.trading, CFG.tilt, 600, Clock())
    return om, pf


async def test_sync_keeps_one_order_per_side() -> None:
    om, pf = om_with_pf()
    pair = [
        DesiredQuote("T", "bid", D("0.48"), D(1), "b"),
        DesiredQuote("T", "ask", D("0.52"), D(1), "a"),
    ]
    await om.sync({"T": pair}, {"T": GRID}, True)
    live = sorted((o.side, o.price) for o in pf.resting.values())
    assert live == [("ask", D("0.52")), ("bid", D("0.48"))]
    # drop the ask: only that side is cancelled
    await om.sync({"T": [pair[0]]}, {"T": GRID}, True)
    assert [(o.side, o.price) for o in pf.resting.values()] == [("bid", D("0.48"))]
    # a single quote (tilt style) still works
    await om.sync({"T": DesiredQuote("T", "ask", D("0.60"), D(2), "x")}, {"T": GRID}, True)
    live2 = [(o.side, o.price, o.remaining) for o in pf.resting.values()]
    assert live2 == [("ask", D("0.60"), D(2))]
    await om.sync({"T": None}, {"T": GRID}, True)
    assert pf.resting == {}


# ---------- mark to market ----------


def test_mark_to_market_v2_never_values_an_unknown_at_zero() -> None:
    p = PortfolioState()
    p.meta["A"] = p.meta["B"] = MarketMeta("E", "c")
    p.apply_fill("A", "bid", D("0.70"), D(10))  # 10 YES, cost 7.00
    p.apply_fill("B", "ask", D("0.30"), D(5))  # 5 NO at 0.70, cost 3.50
    # no marks at all: carried at cost, unrealized 0 (v1 said -10.50)
    assert p.mark_to_market_v2({"A": None, "B": None}, MARK_HAIRCUT) == 0
    assert p.mark_to_market({"A": None, "B": None}, {"A": None, "B": None}) == D("-10.50")
    # marked at a real mid less the haircut
    u = p.mark_to_market_v2({"A": D("0.75"), "B": D("0.25")}, D("0.02"))
    assert u == D("0.30") + D("0.15")  # (0.73-0.70)*10 + (0.73-0.70)*5


# ---------- trading loop with references ----------


def loop(cfg: AppConfig = CFG) -> TradingLoop:
    tl = TradingLoop(cfg, None, clock=Clock())
    tl.update_markets(MARKETS, "Climate and Weather")
    return tl


async def test_loop_blocks_quotes_when_model_fades_the_market() -> None:
    tl = loop()
    # demo book is thin/junk; the real market says 0.75, the model says 0.11
    tl.update_fair("E0-M0", D("0.11"), 1.0, ref_bid=D("0.75"), ref_ask=D("0.77"))
    await tl.step({"E0-M0": book("E0-M0", "0.40", "0.60")}, gate_open=True)
    assert tl.pf.resting == {}
    assert tl.skipped["E0-M0"].startswith("disagree")


async def test_loop_quotes_on_the_blended_fair_with_tilt() -> None:
    tilt_on = CFG.trading.model_copy(update={"tilt_enabled": True})
    tl = loop(with_(CFG, symmetric=SymmetricCfg(enabled=False), trading=tilt_on))
    tl.update_fair("E0-M0", D("0.90"), 1.0, ref_bid=D("0.80"), ref_ask=D("0.82"))
    # blend = 0.8*0.81 + 0.2*0.90 = 0.828 -> inside the spread -> YES bid at
    # min(0.828 - 0.02, best demo bid 0.70 + tick) = 0.71
    await tl.step({"E0-M0": book("E0-M0", "0.70", "0.80")}, gate_open=True)
    (o,) = tl.pf.resting.values()
    assert o.side == "bid" and o.price == D("0.71")


async def test_loop_symmetric_pair_on_demo_uses_prod_reference() -> None:
    no_tilt = CFG.trading.model_copy(update={"tilt_enabled": False})
    cfg = with_(CFG, symmetric=sym(), trading=no_tilt)
    tl = loop(cfg)
    tl.update_fair("E0-M0", D("0.52"), 1.0, ref_bid=D("0.49"), ref_ask=D("0.51"))
    await tl.step({"E0-M0": book("E0-M0", "0.40", "0.60")}, gate_open=True)  # wide demo book
    got = sorted((o.side, o.price) for o in tl.pf.resting.values())
    # blended fair 0.8*0.50 + 0.2*0.52 = 0.504 -> bid 0.48, ask 0.53 (rounded away from fair)
    assert got == [("ask", D("0.53")), ("bid", D("0.48"))]


async def test_loop_on_prod_uses_the_live_book_as_reference() -> None:
    tl = loop()
    tl.live_book_is_real = True
    tl.update_fair("E0-M0", D("0.11"), 1.0)  # no stored reference needed on prod
    await tl.step({"E0-M0": book("E0-M0", "0.75", "0.77")}, gate_open=True)
    assert tl.pf.resting == {} and tl.skipped["E0-M0"].startswith("disagree")


async def test_equity_proxy_uses_reference_mid_not_empty_demo_book() -> None:
    tl = loop()
    tl.pf.apply_fill("E0-M0", "ask", D("0.44"), D(50))  # 50 NO at 0.56, cost 28.00
    tl.update_fair("E0-M0", D("0.30"), 1.0, ref_bid=D("0.40"), ref_ask=D("0.42"))
    books = {"E0-M0": book("E0-M0", None, "0.99")}  # demo: no NO bids at all
    # NO worth (1 - 0.41) - 0.02 = 0.57 each -> +0.50 ; v1 would have said -28.00
    assert tl.unrealized(books) == D("0.50")
    assert tl.equity_proxy(books) == D("0.50")
    ev = await tl.step(books, gate_open=True)
    assert ev == []  # no phantom risk stop
