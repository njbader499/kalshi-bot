"""Tilt strategy (Mode A) and portfolio accounting."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kalshi_maker.money import PriceRange, TickGrid
from kalshi_maker.portfolio import MarketMeta, PortfolioState, RestingOrder
from kalshi_maker.settings import TiltCfg, load_config
from kalshi_maker.strategy.tilt import MarketView, tilt_quote

CENT = TickGrid([PriceRange(D(0), D(1), D("0.01"))])
TAPERED = TickGrid(
    [
        PriceRange(D(0), D("0.10"), D("0.001")),
        PriceRange(D("0.10"), D("0.90"), D("0.01")),
        PriceRange(D("0.90"), D(1), D("0.001")),
    ]
)


TILT_EXAMPLE = load_config(
    Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
).tilt


@pytest.fixture
def tilt(cfg_file: Path) -> TiltCfg:
    return load_config(cfg_file).tilt


def V(fair: str, bid: str | None, ask: str | None, grid: TickGrid = CENT, conf: float = 1.0):
    return MarketView(
        "T", D(fair), conf, None if bid is None else D(bid), None if ask is None else D(ask), grid
    )


def q(v: MarketView, tilt: TiltCfg):
    return tilt_quote(v, tilt, D(1), 0.6)


# ---------- YES favorites ----------


def test_yes_bid_one_tick_over_best_bid(tilt: TiltCfg) -> None:
    r = q(V("0.80", "0.70", "0.75"), tilt)
    assert (r.side, r.price) == ("bid", D("0.71"))  # min(0.78, 0.70 + 0.01)


def test_yes_bid_capped_at_fair_minus_buffer(tilt: TiltCfg) -> None:
    r = q(V("0.80", None, None), tilt)
    assert r.price == D("0.78")


def test_yes_bid_never_at_or_through_ask(tilt: TiltCfg) -> None:
    r = q(V("0.80", "0.77", "0.78"), tilt)
    assert r.price == D("0.77") and r.price < D("0.78")


def test_middle_fair_no_quote_and_low_conf_no_quote(tilt: TiltCfg) -> None:
    assert q(V("0.50", "0.45", "0.55"), tilt) is None
    assert q(V("0.80", "0.70", "0.75", conf=0.5), tilt) is None


def test_min_yes_price_guard(tilt: TiltCfg) -> None:
    # favorite by fair, but the book only supports a 6c bid: never buy YES under 10c
    assert q(V("0.75", "0.05", "0.95"), tilt) is None


def test_subpenny_grid_rounds_down(tilt: TiltCfg) -> None:
    r = q(V("0.955", "0.93", "0.96", grid=TAPERED), tilt)
    assert r.price == D("0.931")  # min(0.935, 0.93 + 0.001) on the 0.001 band
    assert TAPERED.is_valid(r.price)


# ---------- NO on longshots ----------


def test_no_bid_on_longshot(tilt: TiltCfg) -> None:
    r = q(V("0.20", "0.20", "0.25"), tilt)
    # NO fair 0.80 -> target 0.78; best NO bid 0.75 -> one tick better 0.76 -> YES ask 0.24
    assert (r.side, r.price) == ("ask", D("0.24"))


def test_no_bid_never_crosses_yes_bid(tilt: TiltCfg) -> None:
    r = q(V("0.20", "0.23", "0.24"), tilt)
    assert r.side == "ask" and r.price > D("0.23")


def test_min_no_price_guard(cfg_file: Path) -> None:
    c = load_config(cfg_file).tilt.model_copy(update={"longshot_threshold": D("0.5")})
    # fair 0.45 -> NO fair 0.55; a crowded NO side leaves only NO at 5c -> refuse
    assert tilt_quote(V("0.45", "0.94", "0.95"), c, D(1), 0.6) is None


@settings(max_examples=400, deadline=None)
@given(
    fair=st.integers(1, 99),
    bid=st.one_of(st.none(), st.integers(1, 98)),
    spread=st.integers(1, 20),
)
def test_quotes_never_cross_and_keep_buffer(fair: int, bid: int | None, spread: int) -> None:
    t = TILT_EXAMPLE
    yb = None if bid is None else D(bid) / 100
    ya = None if bid is None else min(D("0.99"), D(bid + spread) / 100)
    f = D(fair) / 100
    r = tilt_quote(MarketView("T", f, 1.0, yb, ya, CENT), t, D(1), 0.6)
    if r is None:
        return
    assert CENT.is_valid(r.price)
    if r.side == "bid":
        assert ya is None or r.price < ya
        assert r.price <= f - t.edge_buffer and r.price >= t.min_yes_price
    else:
        assert yb is None or r.price > yb  # selling YES above the best YES bid: no cross
        assert (1 - r.price) <= (1 - f) - t.edge_buffer and (1 - r.price) >= t.min_no_price


def test_tilt_cfg_rejects_inverted_thresholds(cfg_file: Path) -> None:
    with pytest.raises(ValueError):
        TiltCfg(**{**load_config(cfg_file).tilt.model_dump(), "longshot_threshold": D("0.5"),
                   "favorite_threshold": D("0.5")})  # fmt: skip


# ---------- portfolio ----------


def pf() -> PortfolioState:
    p = PortfolioState()
    for t, ev in (("A1", "A"), ("A2", "A"), ("B1", "B")):
        p.meta[t] = MarketMeta(ev, "Climate and Weather")
    return p


def test_resting_cost_yes_and_no() -> None:
    p = pf()
    p.add_resting(RestingOrder("o1", "c1", "A1", "bid", D("0.70"), D(10)))
    p.add_resting(RestingOrder("o2", "c2", "A2", "ask", D("0.30"), D(5)))  # buys NO at 0.70
    assert p.market_exposure("A1") == D(7) and p.market_exposure("A2") == D("3.5")
    assert p.event_exposure("A") == D("10.5") and p.total_exposure() == D("10.5")
    assert p.quoted_markets() == {"A1", "A2"}


def test_fill_moves_cost_from_resting_to_position() -> None:
    p = pf()
    p.add_resting(RestingOrder("o1", "c1", "A1", "bid", D("0.70"), D(10)))
    p.apply_fill("A1", "bid", D("0.70"), D(4), D("0.01"), order_id="o1")
    assert p.resting["o1"].remaining == D(6)
    assert p.positions["A1"].qty == D(4) and p.positions["A1"].cost == D("2.80")
    assert p.market_exposure("A1") == D("7.00")  # unchanged worst case
    assert p.fees == D("0.01")


def test_opposite_fill_closes_pair_and_realizes() -> None:
    p = pf()
    p.apply_fill("A1", "bid", D("0.70"), D(10))  # long 10 YES @ 0.70 (cost 7)
    p.apply_fill("A1", "ask", D("0.80"), D(4))  # buy 4 NO @ 0.20 -> 4 pairs worth $1
    assert p.positions["A1"].qty == D(6) and p.positions["A1"].cost == D("4.20")
    assert p.realized == D("0.40")  # (1 - 0.70 - 0.20) * 4
    p.apply_fill("A1", "ask", D("0.60"), D(8))  # buy 8 NO @ 0.40: close 6, flip to 2 NO
    assert p.positions["A1"].qty == D(-2) and p.positions["A1"].cost == D("0.80")
    assert p.realized == D("0.40") + (1 - D("0.70") - D("0.40")) * 6


def test_mark_to_market_is_conservative() -> None:
    p = pf()
    p.apply_fill("A1", "bid", D("0.70"), D(10))
    p.apply_fill("B1", "ask", D("0.30"), D(5))  # 5 NO at 0.70
    u = p.mark_to_market({"A1": D("0.65"), "B1": None}, {"A1": D("0.68"), "B1": D("0.35")})
    # YES at bid 0.65 -> -0.50 ; NO at 1 - ask 0.35 = 0.65 -> 5*0.65 - 3.50 = -0.25
    assert u == D("-0.75")


def test_unknown_market_metadata_refused() -> None:
    p = PortfolioState()
    p.add_resting(RestingOrder("o", "c", "ZZ", "bid", D("0.5"), D(1)))
    with pytest.raises(KeyError):
        p.event_exposure("x")
