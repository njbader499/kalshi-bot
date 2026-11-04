"""Ledger (append-only, dedupe, WAL), exclusions and reconciliation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kalshi_maker.ledger import Ledger
from kalshi_maker.markets import exclusion_reason
from kalshi_maker.order_types import new_client_order_id
from kalshi_maker.reconcile import ReconcileError, reconcile
from kalshi_maker.rest import Balance
from kalshi_maker.settings import load_config

WS_FILL = {
    "trade_id": "t1",
    "order_id": "o1",
    "client_order_id": "kmb-abc",
    "market_ticker": "KXHIGHNY-26NOV05-T60",
    "is_taker": False,
    "ts_ms": 1_800_000_000_000,
    "yes_price_dollars": "0.4200",
    "count_fp": "3.00",
    "fee_cost": "0.000000",
    "outcome_side": "yes",
    "book_side": "bid",
    "post_position_fp": "3.00",
}
REST_FILL = {
    "fill_id": "t1",
    "trade_id": "t1",
    "order_id": "o1",
    "ticker": "KXHIGHNY-26NOV05-T60",
    "market_ticker": "KXHIGHNY-26NOV05-T60",
    "outcome_side": "yes",
    "book_side": "bid",
    "count_fp": "3.00",
    "yes_price_dollars": "0.4200",
    "no_price_dollars": "0.5800",
    "is_taker": False,
    "fee_cost": "0.000000",
    "ts": 1_800_000_000,
}


@pytest.fixture
async def ledger(tmp_path: Path):
    lg = await Ledger.open(tmp_path / "l.db")
    yield lg
    await lg.close()


async def test_wal_mode(ledger: Ledger) -> None:
    assert await ledger.journal_mode() == "wal"


async def test_fill_dedupe_across_ws_and_rest(ledger: Ledger) -> None:
    assert await ledger.record_fill(WS_FILL, "ws")
    assert not await ledger.record_fill(REST_FILL, "rest")
    assert await ledger.fill_count() == 1
    assert await ledger.errors() == []


async def test_fill_mismatch_flagged(ledger: Ledger) -> None:
    await ledger.record_fill(WS_FILL, "ws")
    await ledger.record_fill({**REST_FILL, "count_fp": "4.00", "fee_cost": "0.01"}, "rest")
    errs = await ledger.errors("fill_mismatch")
    assert len(errs) == 1 and "count 3.00 vs 4.00" in errs[0][1] and "fee" in errs[0][1]


async def test_taker_fill_flagged(ledger: Ledger) -> None:
    await ledger.record_fill({**WS_FILL, "trade_id": "t9", "is_taker": True}, "ws")
    assert [k for k, _ in await ledger.errors()] == ["taker_fill"]


async def test_append_only_enforced(ledger: Ledger, tmp_path: Path) -> None:
    await ledger.record_fill(WS_FILL, "ws")
    await ledger.record_settlement({"ticker": "X", "settled_time": "2026-11-05T00:00:00Z"})
    await ledger.record_error("k", "d")
    raw = sqlite3.connect(tmp_path / "l.db")
    try:
        for stmt in (
            "UPDATE fills SET count = '99'",
            "DELETE FROM fills",
            "DELETE FROM settlements",
            "UPDATE errors SET detail = 'x'",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                raw.execute(stmt)
    finally:
        raw.close()


async def test_money_stored_as_text_and_floats_refused(ledger: Ledger) -> None:
    await ledger.record_fill(WS_FILL, "ws")
    async with ledger.db.execute("SELECT typeof(count), typeof(yes_price), raw FROM fills") as c:
        row = await c.fetchone()
    assert row[0] == "text" and row[1] == "text" and '"0.4200"' in row[2]
    with pytest.raises(TypeError):
        await ledger.record_fill({**WS_FILL, "trade_id": "t2", "count_fp": 3.0}, "ws")


async def test_order_intent_then_event(ledger: Ledger) -> None:
    coid = new_client_order_id()
    await ledger.record_order_intent(coid, "T", "bid", "0.0500", "1.00", 1_800_000_600)
    await ledger.record_order_event(
        {"order_id": "o7", "client_order_id": coid, "status": "resting"}
    )
    async with ledger.db.execute("SELECT order_id, status FROM orders") as c:
        assert await c.fetchone() == ("o7", "resting")


async def test_config_versions_dedupe(ledger: Ledger) -> None:
    a = await ledger.record_config("x: 1")
    b = await ledger.record_config("x: 1")
    c = await ledger.record_config("x: 2")
    assert a == b != c


# ---------- exclusions ----------

NOW = datetime(2026, 11, 4, 20, 0, tzinfo=UTC)
SERIES = {"ticker": "KXHIGHNY", "category": "Climate and Weather", "fee_type": "quadratic"}


def _m(**kw: Any) -> dict[str, Any]:
    m = {
        "ticker": "KXHIGHNY-26NOV05-T60",
        "status": "active",
        "market_type": "binary",
        "close_time": (NOW + timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
        "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}],
    }
    m.update(kw)
    return m


@pytest.fixture
def cfg(cfg_file: Path):
    return load_config(cfg_file)


def test_weather_market_allowed(cfg) -> None:
    assert exclusion_reason(_m(), SERIES, cfg.exclusions, cfg.fees, NOW) is None


@pytest.mark.parametrize(
    "market,series,needle",
    [
        (_m(ticker="KXMVECROSSCATEGORY-S1-ABC"), SERIES, "banned pattern"),
        (_m(mve_collection_ticker="KXMVE-R"), SERIES, "multivariate"),
        (_m(mve_selected_legs=[{"x": 1}]), SERIES, "multivariate"),
        (_m(market_type="scalar"), SERIES, "not binary"),
        (_m(status="closed"), SERIES, "status"),
        (_m(), None, "series metadata"),
        (_m(), {**SERIES, "category": "Sports"}, "banned category"),
        (_m(), {**SERIES, "categories": ["Politics"]}, "banned category"),
        (_m(), {**SERIES, "fee_type": "quadratic_with_maker_fees"}, "maker fees"),
        (_m(), {**SERIES, "fee_type": "brand_new"}, "unknown fee_type"),
        (_m(close_time=None), SERIES, "no close_time"),
        (_m(close_time="2026-11-04T20:30:00Z"), SERIES, "under 60 min"),
        (_m(price_ranges=[]), SERIES, "price_ranges"),
    ],
)
def test_exclusions(cfg, market, series, needle) -> None:
    why = exclusion_reason(market, series, cfg.exclusions, cfg.fees, NOW)
    assert why is not None and needle in why


# ---------- reconcile ----------


class FakeAPI:
    def __init__(self, orders: list[dict], fills: list[dict]) -> None:
        self.orders = orders
        self.fills = fills
        self.fill_params: dict = {}

    async def get_balance(self) -> Balance:
        return Balance.parse({"balance": 50000, "balance_dollars": "500.0000"})

    async def _gen(self, items: list[dict]):
        for i in items:
            yield i

    def iter_positions(self, **_: Any):
        return self._gen(
            [{"ticker": "T", "position_fp": "3.00"}, {"ticker": "U", "position_fp": "0.00"}]
        )

    def iter_orders(self, **_: Any):
        return self._gen(self.orders)

    def iter_fills(self, **kw: Any):
        self.fill_params = kw
        return self._gen(self.fills)

    def paginate(self, *_: Any, **__: Any):
        return self._gen([{"ticker": "T", "settled_time": "2026-11-05T12:00:00Z", "revenue": 300}])


async def test_reconcile_records_everything_and_counts_manual(ledger: Ledger) -> None:
    api = FakeAPI(orders=[{"order_id": "m", "client_order_id": "manual"}], fills=[REST_FILL])
    rep = await reconcile(api, ledger, now=1_800_000_000)
    assert rep.balance == "500.0000"
    assert rep.nonzero_positions == {"T": "3.00"}
    assert (rep.manual_resting, rep.bot_resting) == (1, 0)
    assert (rep.new_fills, rep.new_settlements) == (1, 1)
    assert api.fill_params["min_ts"] == 1_800_000_000 - 30 * 86400
    rep2 = await reconcile(api, ledger, now=1_800_000_100)
    assert rep2.new_fills == 0 and rep2.new_settlements == 0  # idempotent
    assert api.fill_params["min_ts"] == 1_800_000_000 - 3600  # resumes from last fill


async def test_reconcile_refuses_if_bot_orders_still_resting(ledger: Ledger) -> None:
    api = FakeAPI(orders=[{"order_id": "b", "client_order_id": new_client_order_id()}], fills=[])
    with pytest.raises(ReconcileError):
        await reconcile(api, ledger)
