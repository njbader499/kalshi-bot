"""Edge-scaled sizing, resize hysteresis, daily summary scheduling, new stations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from kalshi_maker.daily import SummaryState, build_text, due, ledger_counts, load_state, save_state
from kalshi_maker.ledger import Ledger
from kalshi_maker.money import PriceRange, TickGrid
from kalshi_maker.orders import DryRunExecutor, OrderManager
from kalshi_maker.portfolio import MarketMeta, PortfolioState, RestingOrder
from kalshi_maker.risk import RiskManager
from kalshi_maker.settings import load_config
from kalshi_maker.strategy.tilt import DesiredQuote
from kalshi_maker.weather.contracts import RulesError, parse_contract
from kalshi_maker.weather.stations import SERIES_STATION, STATIONS

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
CFG = load_config(EXAMPLE)
L = CFG.risk
CENT = TickGrid([PriceRange(D(0), D(1), D("0.01"))])

from kalshi_maker.trading import room_for, size_quote  # noqa: E402


def q(price: str, side: str = "bid", t: str = "A1") -> DesiredQuote:
    return DesiredQuote(t, side, D(price), D(1), "t")


# ---------- sizing ----------


def test_min_edge_gives_min_count_full_edge_gives_cap() -> None:
    big = D(1000)
    # edge == buffer (0.02): 1 contract
    assert size_quote(q("0.78"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), big) == 1
    # edge 0.12 -> $15 at 0.68 -> 22 contracts ($14.96)
    assert size_quote(q("0.68"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), big) == 22
    # beyond full edge still capped at $15
    assert size_quote(q("0.30"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), big) == 50


def test_halfway_edge_is_half_the_cap() -> None:
    # edge 0.07 = halfway between 0.02 and 0.12 -> $7.50 / 0.73 = 10.27 -> 10
    assert size_quote(q("0.73"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), D(1000)) == 10


def test_no_side_uses_no_cost() -> None:
    # buy NO at YES price 0.40 -> unit cost 0.60; fair 0.20 -> edge 0.20 -> $15 / 0.60 = 25
    assert size_quote(q("0.40", "ask"), D("0.20"), D("0.02"), D("0.12"), D(1), D(15), D(1000)) == 25


def test_room_clips_and_zero_room_means_no_quote() -> None:
    assert size_quote(q("0.50"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), D("2.40")) == 4
    assert size_quote(q("0.50"), D("0.80"), D("0.02"), D("0.12"), D(1), D(15), D("0.40")) == 0


def test_scaling_disabled_keeps_min_count() -> None:
    assert size_quote(q("0.30"), D("0.80"), D("0.02"), D("0"), D(1), D(15), D(1000)) == 1


def pf3() -> PortfolioState:
    p = PortfolioState()
    for t, ev in (("A1", "A"), ("A2", "A"), ("B1", "B")):
        p.meta[t] = MarketMeta(ev, "Climate and Weather")
    return p


def test_room_is_tightest_cap_excluding_own_resting() -> None:
    p = pf3()
    p.add_resting(RestingOrder("o1", "c", "A1", "bid", D("0.50"), D(20)))  # $10 own
    p.add_resting(RestingOrder("o2", "c", "A2", "bid", D("0.50"), D(120)))  # $60 same event
    # event cap 75: other markets in event A use 60 -> room 15 (own $10 excluded)
    assert room_for(p, "A1", L) == D(15)
    p.add_resting(RestingOrder("o3", "c", "A2", "bid", D("0.50"), D(20)))  # event now 70 excl own
    assert room_for(p, "A1", L) == D(5)


async def test_trading_loop_sizes_by_edge_within_caps(tmp_path: Path) -> None:
    from kalshi_maker.book import OrderBook
    from kalshi_maker.trading import TradingLoop

    cfg = CFG.model_copy(
        update={"trading": CFG.trading.model_copy(update={"edge_full_size": D("0.12")})}
    )
    tl = TradingLoop(cfg, None, clock=lambda: 1_800_000_000.0)
    tl.update_markets(
        [{"ticker": "E0-M0", "event_ticker": "E0",
          "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}],
        "Climate and Weather",
    )  # fmt: skip
    tl.update_fair("E0-M0", D("0.85"), 1.0)
    b = OrderBook("E0-M0")
    b.replace([["0.70", "10"]], [["0.20", "10"]], 1, 0.0)
    await tl.step({"E0-M0": b}, gate_open=True)
    (o,) = tl.pf.resting.values()
    assert o.price == D("0.71") and o.remaining == 21  # edge 0.14 >= 0.12: $15 / 0.71
    assert o.cost <= L.max_order_cost


# ---------- resize hysteresis ----------


async def test_small_resizes_are_ignored_big_ones_amend() -> None:
    t = {"t": 1_800_000_000.0}
    p = pf3()
    m = OrderManager(
        DryRunExecutor(), RiskManager(L, True, None, lambda: t["t"]), p, None,
        CFG.trading, CFG.tilt, CFG.safety.order_expiry_seconds, lambda: t["t"],
    )  # fmt: skip
    grids = {"A1": CENT}
    await m.sync({"A1": DesiredQuote("A1", "bid", D("0.50"), D(20), "t")}, grids, True)
    o = next(iter(p.resting.values()))
    t["t"] += 30
    await m.sync({"A1": DesiredQuote("A1", "bid", D("0.50"), D(22), "t")}, grids, True)
    assert o.remaining == 20  # +10%: below the 25% hysteresis
    await m.sync({"A1": DesiredQuote("A1", "bid", D("0.50"), D(10), "t")}, grids, True)
    assert o.remaining == 10  # -50%: resized


# ---------- daily summary ----------


@pytest.mark.parametrize(
    "utc,last,want",
    [
        (datetime(2026, 11, 5, 6, 59, tzinfo=UTC), "", None),  # 07:59 CET: not yet
        (datetime(2026, 11, 5, 7, 0, tzinfo=UTC), "", date(2026, 11, 5)),  # 08:00 CET
        (datetime(2026, 11, 5, 7, 0, tzinfo=UTC), "2026-11-05", None),  # already sent
        (datetime(2026, 11, 5, 20, 0, tzinfo=UTC), "2026-11-04", date(2026, 11, 5)),  # catch-up
        (datetime(2027, 7, 1, 5, 59, tzinfo=UTC), "", None),  # 07:59 CEST
        (datetime(2027, 7, 1, 6, 0, tzinfo=UTC), "", date(2027, 7, 1)),  # 08:00 CEST (DST)
    ],
)
def test_daily_due_in_prague_time(utc: datetime, last: str, want: date | None) -> None:
    assert due(utc, "08:00", "Europe/Prague", last) == want


def test_summary_state_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    assert load_state(p) == SummaryState()
    save_state(p, SummaryState("2026-11-05", "499.11"))
    assert load_state(p).last_equity == "499.11"


def test_summary_text() -> None:
    counts = {"fills": 3, "contracts": D(5), "fees": D("0"), "creates": 12, "settlements": 1,
              "revenue": D("1.00")}  # fmt: skip
    txt = build_text(
        date(2026, 11, 5), "demo", D("501.50"), D("490.00"), D("11.50"), D("499.11"), counts,
        2, D("40.00"), D("300"), "ok (no halts)", "126/126 agree",
    )  # fmt: skip
    assert "DEMO" in txt and "Change since last summary: $2.39" in txt
    assert "3 fill(s), 5 contract(s)" in txt and "12 order(s) placed" in txt
    first = build_text(date(2026, 11, 5), "demo", D(1), D(1), D(0), None, counts, 0, D(0),
                       D(300), "ok", "-")  # fmt: skip
    assert "first summary" in first


async def test_ledger_counts_last_24h(tmp_path: Path) -> None:
    lg = await Ledger.open(tmp_path / "l.db")
    lg._clock = lambda: 1_800_000_000.0
    fill = {"trade_id": "t1", "order_id": "o", "market_ticker": "T", "count_fp": "2.00"}
    await lg.record_fill(
        {**fill, "yes_price_dollars": "0.50", "fee_cost": "0.0100", "is_taker": False}, "ws"
    )
    await lg.record_order_action("create", "T", "bid", "0.5000", "2.00", "x", False)
    await lg.record_order_action("create", "T", "bid", "0.5000", "2.00", "x", True)  # dry: excluded
    await lg.record_settlement({"ticker": "T", "settled_time": "x", "revenue": 200})
    c = await ledger_counts(lg, 1_800_000_000.0 - 86400)
    assert c == {"fills": 1, "contracts": D(2), "fees": D("0.01"), "creates": 1,
                 "settlements": 1, "revenue": D(2)}  # fmt: skip
    await lg.close()


def test_bad_summary_tz_rejected() -> None:
    from kalshi_maker.settings import Alerts

    with pytest.raises(ValueError):
        Alerts(fill_batch_minutes=15, daily_summary_tz="Mars/Olympus")
    with pytest.raises(ValueError):
        Alerts(fill_batch_minutes=15, daily_summary_time="25:00")


# ---------- new stations ----------

NEW = [
    ("KXHIGHLAX", "Los Angeles", "CLILAX", "KLAX"),
    ("KXHIGHTATL", "Atlanta", "CLIATL", "KATL"),
    ("KXHIGHTBOS", "Boston", "CLIBOS", "KBOS"),
    ("KXHIGHTDAL", "Dallas", "CLIDFW", "KDFW"),
    ("KXHIGHDEN", "Denver", "CLIDEN", "KDEN"),
]


@pytest.mark.parametrize("series,place,code,obs", NEW)
def test_new_cities_parse_with_their_station(series: str, place: str, code: str, obs: str) -> None:
    rules = (  # verbatim template seen live 2026-11-04 (e.g. KXHIGHLAX-26NOV05-T82)
        f"If the maximum temperature recorded at {place} ({code}) for Nov 5, 2026, is greater "
        "than 82° fahrenheit according to The Weather Company, then the market resolves to Yes."
    )
    m: dict[str, Any] = {
        "ticker": f"{series}-26NOV05-T82", "event_ticker": f"{series}-26NOV05",
        "rules_primary": rules, "strike_type": "greater", "floor_strike": 82,
        "cap_strike": None, "yes_sub_title": "83° or above",
    }  # fmt: skip
    c = parse_contract(m)
    assert c.station is STATIONS[code] and c.station.obs_station == obs and c.lo == 83
    assert SERIES_STATION[series] == code
    with pytest.raises(RulesError, match="expected"):
        parse_contract({**m, "rules_primary": rules.replace(code, "CLINYC")})
