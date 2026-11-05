"""Prod-paper simulator, adverse-selection marks, pull windows and re-quote-on-move."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kalshi_maker.book import OrderBook
from kalshi_maker.ledger import Ledger
from kalshi_maker.marks import GRACE_SECONDS, MarkTracker
from kalshi_maker.paper import (
    PaperFill,
    PaperQuote,
    PaperState,
    PaperTrader,
    Trade,
    apply_trades,
    levels_to_book,
    load_state,
    parse_trade,
    queue_ahead,
    save_state,
    settle_position,
)
from kalshi_maker.portfolio import PortfolioState
from kalshi_maker.rest import OrderbookLevels
from kalshi_maker.runner import Runner
from kalshi_maker.strategy.tilt import DesiredQuote
from kalshi_maker.trading import in_pull_window

D = Decimal
T = "KXHIGHNY-25NOV06-B50"


def trade(ts: float, price: str, count: str, taker: str, tid: str | None = None) -> Trade:
    return Trade(tid or f"t{ts}-{price}-{count}", T, ts, D(price), D(count), taker)


# ---- trade tape parsing ----


def test_parse_trade_fixed_point_and_cents_fallback() -> None:
    fp = parse_trade(
        {
            "trade_id": "a", "ticker": T, "yes_price_dollars": "0.4200", "count_fp": "3.00",
            "taker_side": "no", "created_time": "2026-11-05T12:00:00Z",
        }
    )  # fmt: skip
    assert (fp.yes_price, fp.count, fp.taker_side) == (D("0.42"), D(3), "no")
    cents = parse_trade(
        {
            "trade_id": "b", "ticker": T, "yes_price": 42, "count": 3, "taker_side": "YES",
            "created_time": "2026-11-05T12:00:00+00:00",
        }
    )  # fmt: skip
    assert (cents.yes_price, cents.count, cents.taker_side) == (D("0.42"), D(3), "yes")
    assert fp.ts == cents.ts
    with pytest.raises(ValueError):
        parse_trade({"yes_price": 42, "count": 1, "taker_side": "buy", "created_time": "x"})


# ---- fills ----


def test_bid_hit_only_by_yes_sellers_and_only_after_posting() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(5), posted_at=100.0, queue_ahead=D(0))
    assert q.hits(trade(99.0, "0.39", "5", "no")) == (D(0), D(0))  # before we posted
    assert q.hits(trade(101.0, "0.39", "5", "yes")) == (D(0), D(0))  # a YES buyer: hits asks
    assert q.hits(trade(101.0, "0.41", "5", "no")) == (D(0), D(0))  # above our bid
    assert q.hits(trade(101.0, "0.39", "2", "no")) == (D(2), D(2))  # through: certain, its size
    assert q.hits(trade(101.0, "0.39", "9", "no")) == (D(5), D(5))  # capped at our size


def test_at_price_fill_waits_for_the_queue_ahead() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(5), posted_at=100.0, queue_ahead=D(10))
    # 8 traded at our price: still 2 ahead of us. Optimistic would have had us first.
    assert q.hits(trade(101.0, "0.40", "8", "no")) == (D(0), D(5))
    q.remaining_opt -= D(5)
    # 6 more at price: 14 cumulative, 4 beyond the queue -> 4 of our 5 fill
    assert q.hits(trade(102.0, "0.40", "6", "no")) == (D(4), D(0))
    q.remaining_pess -= D(4)
    # 3 more at price: only 1 left to fill
    assert q.hits(trade(103.0, "0.40", "3", "no")) == (D(1), D(0))


def test_at_price_fill_never_exceeds_the_trade_size() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(5), posted_at=100.0, queue_ahead=D(10))
    assert q.hits(trade(101.0, "0.40", "11", "no")) == (D(1), D(5))  # 1 beyond the queue
    q.remaining_pess -= D(1)
    q.remaining_opt -= D(5)
    assert q.hits(trade(102.0, "0.40", "1", "no")) == (D(1), D(0))  # not min(remaining, 2)


def test_filled_quote_survives_a_restart_as_filled() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(5), 100.0, D(0), remaining_pess=D(0), remaining_opt=D(0))
    assert q.remaining_pess == 0 and q.remaining_opt == 0
    fresh = PaperQuote(T, "bid", D("0.40"), D(5), 100.0, D(0))
    assert fresh.remaining_pess == 5 and fresh.remaining_opt == 5


def test_ask_is_a_no_bid_hit_by_yes_buyers() -> None:
    q = PaperQuote(T, "ask", D("0.60"), D(4), posted_at=100.0, queue_ahead=D(0))
    assert q.hits(trade(101.0, "0.61", "1", "no")) == (D(0), D(0))  # a YES seller
    assert q.hits(trade(101.0, "0.59", "1", "yes")) == (D(0), D(0))  # below our ask
    assert q.hits(trade(101.0, "0.61", "1", "yes")) == (D(1), D(1))  # through
    q2 = PaperQuote(T, "ask", D("0.60"), D(4), posted_at=100.0, queue_ahead=D(0))
    assert q2.hits(trade(101.0, "0.60", "3", "yes")) == (D(3), D(3))  # at price, no queue


def test_apply_trades_walks_in_time_order_and_tracks_remaining() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(5), posted_at=100.0, queue_ahead=D(0))
    fills = apply_trades(
        [q],
        [trade(103.0, "0.40", "2", "no", "late"), trade(101.0, "0.40", "4", "no", "early")],
    )
    pess = [f for f in fills if f.mode == "pess"]
    assert [(f.trade_id, f.count) for f in pess] == [("early", D(4)), ("late", D(1))]
    assert q.remaining_pess == 0 and q.remaining_opt == 0
    # nothing more fills once the quote is exhausted
    assert apply_trades([q], [trade(104.0, "0.39", "9", "no")]) == []


def test_filled_quote_survives_a_restart_as_filled_after_through_trade() -> None:
    q = PaperQuote(T, "bid", D("0.40"), D(2), posted_at=100.0, queue_ahead=D(0))
    fills = apply_trades([q], [trade(101.0, "0.39", "1", "no"), trade(102.0, "0.38", "5", "no")])
    expect = [("pess", D(1)), ("opt", D(1)), ("pess", D(1)), ("opt", D(1))]
    assert [(f.mode, f.count) for f in fills] == expect
    assert q.remaining_pess == 0 and q.remaining_opt == 0


def test_queue_ahead_reads_the_level_we_join() -> None:
    lv = OrderbookLevels(
        yes_bids=((D("0.38"), D(20)), (D("0.40"), D(7))),
        no_bids=((D("0.55"), D(9)), (D("0.58"), D(3))),
    )
    assert queue_ahead(lv, "bid", D("0.40")) == 7
    assert queue_ahead(lv, "bid", D("0.41")) == 0  # improving the book
    assert queue_ahead(lv, "ask", D("0.42")) == 3  # NO bid at 0.58
    assert queue_ahead(lv, "ask", D("0.50")) == 0


def test_levels_to_book_gives_a_valid_two_sided_book() -> None:
    lv = OrderbookLevels(yes_bids=((D("0.40"), D(7)),), no_bids=((D("0.58"), D(3)),))
    b = levels_to_book(T, lv, now_mono=5.0)
    assert isinstance(b, OrderBook) and b.valid
    assert (b.best_yes_bid, b.best_yes_ask) == (D("0.40"), D("0.42"))


# ---- settlement ----


def test_settle_position_pays_the_winning_side() -> None:
    pf = PortfolioState()
    pf.apply_fill(T, "bid", D("0.40"), D(5))  # 5 YES at 0.40 = cost 2.00
    assert settle_position(pf, T, result_yes=True) == D("3.00")
    pf.apply_fill(T, "ask", D("0.60"), D(2))  # 2 NO at 0.40 = cost 0.80
    assert settle_position(pf, T, result_yes=True) == D("-0.80")
    pf.apply_fill(T, "ask", D("0.60"), D(2))
    assert settle_position(pf, T, result_yes=False) == D("1.20")
    assert pf.realized == D("3.40") and T not in pf.positions
    assert settle_position(pf, T, result_yes=True) == 0  # nothing held


# ---- state ----


def test_state_round_trip(tmp_path: Path) -> None:
    st = PaperState(
        quotes=[{"ticker": T, "side": "bid", "price": "0.40", "count": "5", "posted_at": "100.0",
                 "queue_ahead": "3", "remaining_pess": "5", "remaining_opt": "2",
                 "traded_at_price": "1"}],
        positions_pess={T: {"qty": "5", "cost": "2.00"}},
        realized_pess="1.25", cursor={T: 123.0}, settled={"X": "yes"}, fills_pess=2,
        started_at=50.0,
    )  # fmt: skip
    p = tmp_path / "paper_state.json"
    save_state(p, st)
    back = load_state(p)
    assert back == st
    p.write_text("{not json", encoding="utf-8")
    fresh = load_state(p)
    assert fresh.quotes == [] and fresh.started_at > 0


class FakeProd:
    def __init__(self) -> None:
        self.trades: dict[str, list[dict[str, Any]]] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        self.books: dict[str, OrderbookLevels] = {}
        self.calls: list[tuple[str, str]] = []

    async def iter_trades(
        self, ticker: str, min_ts: int | None = None, max_ts: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("trades", ticker))
        for t in self.trades.get(ticker, []):
            yield t

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.calls.append(("market", ticker))
        return self.markets.get(ticker, {})

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> OrderbookLevels:
        self.calls.append(("book", ticker))
        return self.books[ticker]


def raw_trade(ts_iso: str, price: str, count: str, taker: str, tid: str) -> dict[str, Any]:
    return {
        "trade_id": tid, "ticker": T, "yes_price_dollars": price, "count_fp": count,
        "taker_side": taker, "created_time": ts_iso,
    }  # fmt: skip


def test_paper_trader_fills_persists_and_settles(tmp_path: Path) -> None:
    async def go() -> None:
        prod = FakeProd()
        clock = {"t": 1_700_000_000.0}
        lg = await Ledger.open(tmp_path / "l.db")
        try:
            pt = PaperTrader(prod, lg, tmp_path / "paper.json", clock=lambda: clock["t"])
            lv = OrderbookLevels(yes_bids=((D("0.40"), D(7)),), no_bids=((D("0.58"), D(3)),))
            n = pt.requote({T: [DesiredQuote(T, "bid", D("0.40"), D(5), "test")]}, {T: lv})
            assert n == 1 and pt.quotes[0].queue_ahead == 7
            fills = await pt.process_trades()
            assert fills == [] and prod.calls == [("trades", T)]
            # a trade through our price after we posted: pessimistic fill of everything
            iso = "2023-11-14T22:13:30Z"  # 1_700_000_010
            prod.trades[T] = [
                raw_trade(iso, "0.39", "2", "no", "x1"),
                raw_trade(iso, "0.39", "2", "no", "x1"),  # duplicate page overlap
            ]
            fills = await pt.process_trades()
            assert [(f.mode, f.count) for f in fills] == [("pess", D(2)), ("opt", D(2))]
            assert pt.pess.positions[T].qty == 2 and pt.opt.positions[T].qty == 2
            assert pt.state.cursor[T] == pytest.approx(1_700_000_010.0)
            # the same trade is not applied twice on the next cycle
            assert await pt.process_trades() == []
            pt.persist()
            back = PaperTrader(prod, None, tmp_path / "paper.json", clock=lambda: clock["t"])
            assert back.pess.positions[T].qty == 2 and back.state.fills_pess == 1
            assert back.quotes[0].remaining_pess == 3 and back.quotes[0].remaining_opt == 3
            # settlement: still open -> nothing; closed without result -> wait; result -> P&L
            assert await back.settle({T}) == []
            assert await back.settle(set()) == []
            prod.markets[T] = {"ticker": T, "result": "yes"}
            out = await back.settle(set())
            assert out == [(T, "yes", D("1.20"), D("1.20"))]
            assert back.pess.realized == D("1.20") and back.state.settled[T] == "yes"
            text = back.summary({T: None}, D("0.02"))
            assert "pessimistic realized +1.20" in text
            async with lg.db.execute("SELECT mode, count FROM paper_fills ORDER BY mode") as cur:
                rows = [tuple(r) async for r in cur]
            assert rows == [("opt", "2"), ("pess", "2")]
        finally:
            await lg.close()

    asyncio.run(go())


def test_paper_fills_are_append_only(tmp_path: Path) -> None:
    async def go() -> None:
        lg = await Ledger.open(tmp_path / "l.db")
        try:
            f = PaperFill("pess", T, "bid", D("0.4"), D(1), "t1", 1.0, D(0))
            await lg.record_paper_fill(f, 2.0)
            with pytest.raises(Exception, match="append-only"):
                await lg.db.execute("DELETE FROM paper_fills")
        finally:
            await lg.close()

    asyncio.run(go())


# ---- adverse-selection marks ----


def test_mark_tracker_takes_due_marks_and_gives_up_after_grace(tmp_path: Path) -> None:
    async def go() -> None:
        prod = FakeProd()
        prod.books[T] = OrderbookLevels(
            yes_bids=((D("0.30"), D(1)),), no_bids=((D("0.66"), D(1)),)  # mid 0.32
        )
        clock = {"t": 1000.0}
        lg = await Ledger.open(tmp_path / "l.db")
        try:
            mt = MarkTracker(prod, lg, clock=lambda: clock["t"])  # type: ignore[arg-type]
            await mt.register("k1", "paper_pess", T, "bid", D("0.40"), 1000.0, D("0.41"))
            assert await mt.tick() == 0  # nothing due yet
            clock["t"] = 1000.0 + 300 + 10
            assert await mt.tick() == 1  # the 5-minute mark
            # bot was down: the 30-minute mark is far past its grace -> "na"; 60m taken
            clock["t"] = 1000.0 + 3600 + 30
            assert await mt.tick() == 2
            pend = await lg.pending_fill_marks(clock["t"])
            assert pend == []
            adv = await lg.adverse_selection("paper", 0.0)
            # bought YES at 0.40 with mid 0.41; mid is 0.32 afterwards: 0.09 against us
            assert adv["mid_5m"] == (1, "0.0900") and adv["mid_60m"] == (1, "0.0900")
            assert adv["mid_30m"] == (0, "-")  # "na" is excluded
            assert (await lg.adverse_selection("real", 0.0))["mid_5m"] == (0, "-")
            assert GRACE_SECONDS < 300
        finally:
            await lg.close()

    asyncio.run(go())


def test_adverse_selection_sign_for_no_buys(tmp_path: Path) -> None:
    async def go() -> None:
        lg = await Ledger.open(tmp_path / "l.db")
        try:
            await lg.upsert_fill_mark("a", "real", T, "ask", "0.60", 10.0, mid_at_fill="0.59")
            await lg.upsert_fill_mark("a", "", T, "ask", "", 10.0, mid_5m="0.65")
            # bought NO (sold YES at 0.60); YES mid rose 0.06 -> 0.06 against us
            assert (await lg.adverse_selection("real", 0.0))["mid_5m"] == (1, "0.0600")
            with pytest.raises(ValueError):
                await lg.upsert_fill_mark("a", "", T, "ask", "", 10.0, bogus="1")
        finally:
            await lg.close()

    asyncio.run(go())


# ---- pull windows and re-quote on move ----


def test_in_pull_window_wraps_the_hour() -> None:
    assert not in_pull_window(30, [])
    assert in_pull_window(50, [50, 6]) and in_pull_window(59, [50, 6])
    assert in_pull_window(0, [50, 6]) and in_pull_window(5, [50, 6])
    assert not in_pull_window(6, [50, 6]) and not in_pull_window(49, [50, 6])
    assert in_pull_window(12, [10, 20]) and not in_pull_window(20, [10, 20])


def _runner_stub(requote: bool, quoted: set[str], books: dict[str, Any]) -> Any:
    cfg = SimpleNamespace(trading=SimpleNamespace(requote_on_move=requote, min_requote_seconds=2))
    return SimpleNamespace(
        settings=SimpleNamespace(config=cfg),
        ws=SimpleNamespace(books=books),
        trading=SimpleNamespace(pf=SimpleNamespace(quoted_markets=lambda: quoted)),
        _last_step_mono=100.0,
        _last_step_book_mono=0.0,
    )


def test_books_moved_triggers_once_per_change_and_respects_the_floor() -> None:
    books = {T: SimpleNamespace(updated_mono=105.0), "OTHER": SimpleNamespace(updated_mono=150.0)}
    r = _runner_stub(True, {T}, books)
    assert not Runner._books_moved(r, 101.0)  # within min_requote_seconds of the last step
    assert Runner._books_moved(r, 103.0)  # our quoted book moved since the last step
    assert not Runner._books_moved(r, 104.0)  # same change does not re-trigger
    books[T].updated_mono = 110.0
    assert Runner._books_moved(r, 111.0)
    books["OTHER"].updated_mono = 200.0  # an unquoted market moving does not count
    assert not Runner._books_moved(r, 201.0)
    assert not Runner._books_moved(_runner_stub(False, {T}, books), 300.0)  # feature off
    assert not Runner._books_moved(_runner_stub(True, set(), books), 300.0)  # nothing quoted
    r2 = _runner_stub(True, {T}, books)
    r2.ws = None
    assert not Runner._books_moved(r2, 300.0)


def test_pull_minutes_validation() -> None:
    from kalshi_maker.settings import TradingCfg

    base: dict[str, Any] = {
        "order_count": 1, "loop_seconds": 20, "max_fair_age_seconds": 900,
        "min_seconds_between_amends": 5,
    }  # fmt: skip
    assert TradingCfg(**base).pull_minutes == [50, 6]  # the default
    assert TradingCfg(**base, pull_minutes=[]).pull_minutes == []
    assert TradingCfg(**base, pull_minutes=[10, 20]).pull_minutes == [10, 20]
    with pytest.raises(ValueError):
        TradingCfg(**base, pull_minutes=[50])
    with pytest.raises(ValueError):
        TradingCfg(**base, pull_minutes=[50, 60])


def test_time_import_is_used_by_fake_clock() -> None:
    assert time.time() > 0


# ---- runner wiring: the paper loop quotes on the prod book and fills from the tape ----


def test_runner_paper_cycle_end_to_end(tmp_path: Path) -> None:
    import httpx
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from kalshi_maker.auth import Signer
    from kalshi_maker.rest import KalshiREST
    from kalshi_maker.settings import Secrets, Settings, load_config

    from .conftest import EXAMPLE

    async def go() -> None:
        cfg = load_config(EXAMPLE)
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={
            "data_dir": tmp_path, "kill_file": tmp_path / "KILL"})})  # fmt: skip
        settings = Settings(cfg, Secrets(_env_file=None))  # type: ignore[call-arg]
        api = KalshiREST(
            cfg.hosts.demo.rest, cfg.rate_limits,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )  # fmt: skip
        lg = await Ledger.open(tmp_path / "l.db")
        prod = FakeProd()
        try:
            signer = Signer("k", ed25519.Ed25519PrivateKey.generate())
            r = Runner(settings, api, lg, signer, cfg.paths.kill_file, None, prod_public=prod)  # type: ignore[arg-type]
            assert r.paper is not None and r.paper_loop is not None and r.marks is not None
            assert r.paper_loop.live_book_is_real and r.paper_loop.pf is r.paper.pess
            assert not r.paper_loop.live  # a dry loop: can never send
            mk = {"ticker": T, "event_ticker": T.rsplit("-", 1)[0],
                  "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}  # fmt: skip
            for loop in (r.trading, r.paper_loop):
                loop.update_markets([mk], "Climate and Weather")
            lv = OrderbookLevels(yes_bids=((D("0.40"), D(7)),), no_bids=((D("0.56"), D(3)),))
            prod.books[T] = lv
            r.prod_levels = {T: lv}
            r.paper_loop.update_fair(T, D("0.42"), 0.9, ref_bid=D("0.40"), ref_ask=D("0.44"))
            await r._paper_cycle({T})
            sides = sorted((q.side, q.price) for q in r.paper.quotes)
            assert sides == [("ask", D("0.43")), ("bid", D("0.41"))]  # inside the 4-tick book
            assert all(q.queue_ahead == 0 for q in r.paper.quotes)  # both improve the book
            assert (tmp_path / "paper_state.json").exists()
            # a taker sells YES through our bid: pessimistic fill, position, mark registered
            posted = r.paper.quotes[0].posted_at
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(posted + 5))
            prod.trades[T] = [raw_trade(iso, "0.40", "1", "no", "tape1")]
            await r._paper_cycle({T})
            assert r.paper.pess.positions[T].qty == 1 and r.paper.state.fills_pess == 1
            pend = await lg.pending_fill_marks(time.time() + 10)  # the tape trade is "now + 5"
            assert [(k.split(":")[0], side) for k, _t, side, _at, _h in pend] == [("paper", "bid")]
            assert "PROD-PAPER" in r.status_text()
            # the pull window applies to the paper loop too
            r.trading.pull_active = True
            await r._paper_cycle({T})
            assert r.paper_loop.skipped.get(T, "").startswith("pull window")
            assert r.paper.quotes == []
        finally:
            await r.weather_sources.aclose()
            await api.aclose()
            await lg.close()

    asyncio.run(go())
