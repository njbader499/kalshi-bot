"""Trading loop (Phase 4): fair values + books -> tilt quotes -> OrderManager.

  trading_enabled: false (default)  -> DryRunExecutor + a *shadow* RiskManager (the same
      limits, evaluated as if trading were on, with no state file). Every decision goes to
      ledger `order_actions` with dry_run=1. Nothing is ever sent.
  trading_enabled: true (owner only) -> LiveExecutor + the real RiskManager with persisted
      halt state (<data_dir>/risk_state.json).

Market selection: candidates need a fresh fair value (<= max_fair_age_seconds), confidence
>= weather.min_confidence, a valid book and a tick grid. Markets already quoted are kept
first (avoids churn), then ranked by edge (fair - price for YES bids, price - fair for NO
bids), up to risk.max_markets_quoted.

Fair value used for quoting (2026-11-05): the MODEL fair is blended with the REAL market's
mid (prod public book: on demo the live book is not a price) per config.blend, and a market
is not quoted at all when the model and the market disagree by more than
blend.max_disagreement. Two quote modes can run together: `tilt` (one side, where the
blended fair is extreme) and `symmetric` (both sides inside the spread, the maker edge).

Equity for the risk stops = realized - fees + unrealized, with each position MARKED at the
reference mid (prod) or the blended fair, less a haircut; never at a thin demo book's
best bid and never at 0 just because a book is empty (that false mark tripped the drawdown
and weekly stops on 2026-11-05 with zero settlements).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

from .book import OrderBook
from .ledger import Ledger
from .money import TickGrid
from .orders import DryRunExecutor, Executor, LiveExecutor, OrderManager
from .portfolio import MarketMeta, PortfolioState, Position
from .rest import KalshiREST
from .risk import RiskManager
from .settings import AppConfig, MarketBlendCfg, RiskLimits
from .strategy.symmetric import SymView, symmetric_quotes
from .strategy.tilt import DesiredQuote, MarketView, tilt_quote

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FairInput:
    p: Decimal  # MODEL fair value
    confidence: float
    at: float  # unix time computed
    ref_bid: Decimal | None = None  # real market (prod) best YES bid at that time
    ref_ask: Decimal | None = None


P4 = Decimal("0.0001")
MARK_HAIRCUT = Decimal("0.02")


def blended_fair(
    model: Decimal, ref_bid: Decimal | None, ref_ask: Decimal | None, cfg: MarketBlendCfg
) -> tuple[Decimal | None, str]:
    """(fair to quote on, how it was formed). None = do not quote this market."""
    if ref_bid is None or ref_ask is None or ref_ask <= ref_bid:
        return (model, "model_only") if cfg.model_only_ok else (None, "no_ref")
    if ref_ask - ref_bid > cfg.max_spread:
        return (model, "model_only") if cfg.model_only_ok else (None, "wide_ref")
    mid = (ref_bid + ref_ask) / 2
    if abs(model - mid) > cfg.max_disagreement:
        return None, f"disagree model={model} mid={mid}"
    f = (cfg.weight * mid + (1 - cfg.weight) * model).quantize(P4, ROUND_HALF_EVEN)
    # Never lean outside the market's own range: the model picks where INSIDE the spread
    # the quotes centre, it cannot push them past the best bid or ask.
    f = min(ref_ask, max(ref_bid, f))
    return f, "blend"


def edge(q: DesiredQuote, fair: Decimal) -> Decimal:
    return fair - q.price if q.side == "bid" else q.price - fair


def room_for(pf: PortfolioState, ticker: str, limits: RiskLimits) -> Decimal:
    """Worst-case $ room for a new/replacement quote in `ticker`: the tightest of every cap,
    measured WITHOUT this market's own resting orders (they get replaced or amended)."""
    own = sum((o.cost for o in pf.resting_for(ticker)), Decimal(0))
    meta = pf.meta[ticker]
    return min(
        limits.max_order_cost,
        limits.max_cost_per_market - (pf.market_exposure(ticker) - own),
        limits.max_cost_per_event - (pf.event_exposure(meta.event) - own),
        limits.max_cost_per_category - (pf.category_exposure(meta.category) - own),
        limits.max_capital_deployed - (pf.total_exposure() - own),
    )


def size_quote(
    q: DesiredQuote, fair: Decimal, buffer: Decimal, edge_full: Decimal, min_count: Decimal,
    max_order_cost: Decimal, room: Decimal,
) -> Decimal:  # fmt: skip
    """Whole contracts: min_count at edge == buffer, scaling linearly to max_order_cost at
    edge >= edge_full, clipped to `room`. 0 means 'can't afford even one'."""
    unit = q.price if q.side == "bid" else 1 - q.price
    if unit <= 0:
        return Decimal(0)
    target = min_count * unit
    if edge_full > buffer:
        frac = min(Decimal(1), max(Decimal(0), (edge(q, fair) - buffer) / (edge_full - buffer)))
        target = max(target, max_order_cost * frac)
    target = min(target, max_order_cost, room)
    return (target / unit).to_integral_value(rounding=ROUND_FLOOR)


class TradingLoop:
    def __init__(
        self,
        cfg: AppConfig,
        ledger: Ledger | None,
        api: KalshiREST | None = None,
        clock: Callable[[], float] = time.time,
        executor: Executor | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self.pf = PortfolioState()
        live = cfg.trading.trading_enabled
        if executor is None:
            if live:
                if api is None:
                    raise ValueError("live trading needs an API client")
                executor = LiveExecutor(api, cfg.trading)
            else:
                executor = DryRunExecutor()
        if live and executor.dry_run:
            raise ValueError("trading_enabled is true but a dry-run executor was supplied")
        if not live and not executor.dry_run:
            raise ValueError("trading_enabled is false: refusing a live executor")
        self.live = live
        # Live: the real RiskManager, halt state persisted. Dry: a *shadow* RiskManager that
        # evaluates the same limits as if trading were on (so the dry log shows what real
        # limits would allow), with no state file. Either way the hard switch is the
        # executor: in dry mode there is no API handle at all.
        self.risk = RiskManager(
            cfg.risk,
            trading_enabled=True,
            state_path=(cfg.paths.data_dir / "risk_state.json") if live else None,
            clock=clock,
        )
        self.om = OrderManager(
            executor, self.risk, self.pf, ledger, cfg.trading, cfg.tilt,
            cfg.safety.order_expiry_seconds, clock,
        )  # fmt: skip
        self.grids: dict[str, TickGrid] = {}
        self.fairs: dict[str, FairInput] = {}
        self.risk_events: list[str] = []
        self.paused = False  # /pause: cancel all, quote nothing until /resume
        self.off_markets: set[str] = set()  # /mode <ticker> off
        self.off_categories: set[str] = set()  # /mode <category> off
        # On prod the live book IS the real market; on demo it is not a price, so the
        # reference comes from the prod public snapshot stored with each fair value.
        self.live_book_is_real = False
        self.skipped: dict[str, str] = {}  # ticker -> why not quoted this step
        # Same-day weather markets (set by the runner from the contracts) are pulled inside
        # trading.pull_minutes, when the hourly observation lands.
        self.same_day_tickers: set[str] = set()
        self.pull_active = False

    # ---- inputs ----
    def update_markets(self, markets: Iterable[Mapping[str, Any]], category: str) -> None:
        for m in markets:
            t = str(m["ticker"])
            try:
                self.grids[t] = TickGrid.from_market(m)
            except Exception:
                self.grids.pop(t, None)
                continue
            self.pf.meta[t] = MarketMeta(str(m.get("event_ticker") or t), category)

    def update_fair(
        self,
        ticker: str,
        p: Decimal,
        confidence: float,
        at: float | None = None,
        ref_bid: Decimal | None = None,
        ref_ask: Decimal | None = None,
    ) -> None:
        self.fairs[ticker] = FairInput(
            p, confidence, self._clock() if at is None else at, ref_bid, ref_ask
        )

    def _reference(
        self, t: str, f: FairInput, b: OrderBook
    ) -> tuple[Decimal | None, Decimal | None]:
        """Real-market bid/ask: the live book on prod, the stored prod snapshot on demo."""
        if self.live_book_is_real:
            return b.best_yes_bid, b.best_yes_ask
        return f.ref_bid, f.ref_ask

    def quote_fair(self, t: str, f: FairInput, b: OrderBook) -> tuple[Decimal | None, str]:
        rb, ra = self._reference(t, f, b)
        return blended_fair(f.p, rb, ra, self.cfg.blend)

    def load_positions(self, positions: Iterable[Mapping[str, Any]]) -> None:
        """From GET /portfolio/positions after reconcile (verified field names).
        Cost basis = market_exposure_dollars (conservative for risk)."""
        self.pf.positions.clear()
        for p in positions:
            qty = Decimal(str(p.get("position_fp") or "0"))
            if qty == 0:
                continue
            cost = Decimal(str(p.get("market_exposure_dollars") or "0"))
            t = str(p.get("ticker"))
            self.pf.positions[t] = Position(t, qty, cost)

    def on_all_cancelled(self) -> None:
        """The safety gate cancelled every bot order on the exchange."""
        self.pf.resting.clear()

    # ---- one step ----
    def desired(self, books: Mapping[str, OrderBook]) -> dict[str, list[DesiredQuote] | None]:
        now = self._clock()
        t_cfg = self.cfg.trading
        min_conf = self.cfg.weather.min_confidence
        self.skipped = {}
        cands: list[tuple[bool, Decimal, str, list[DesiredQuote]]] = []
        for t, f in self.fairs.items():
            b = books.get(t)
            grid = self.grids.get(t)
            if b is None or not b.valid or grid is None or t not in self.pf.meta:
                continue
            if t in self.off_markets or self.pf.meta[t].category in self.off_categories:
                continue
            if now - f.at > t_cfg.max_fair_age_seconds:
                self.skipped[t] = "stale fair"
                continue
            if self.pull_active and t in self.same_day_tickers:
                self.skipped[t] = "pull window (hourly obs due)"
                continue
            if f.confidence < min_conf:
                self.skipped[t] = f"low confidence {f.confidence}"
                continue
            fair, how = self.quote_fair(t, f, b)
            if fair is None:
                self.skipped[t] = how
                continue
            room = room_for(self.pf, t, self.cfg.risk)
            quotes: list[DesiredQuote] = []
            best_edge = Decimal(0)
            pos = self.pf.positions[t].qty if t in self.pf.positions else Decimal(0)
            if self.cfg.symmetric.enabled:
                sv = SymView(t, fair, b.best_yes_bid, b.best_yes_ask, grid, pos)
                for q in symmetric_quotes(sv, self.cfg.symmetric, self.cfg.symmetric.count):
                    e = edge(q, fair)
                    if e <= 0:
                        continue
                    n = size_quote(
                        q, fair, Decimal(0), t_cfg.edge_full_size, self.cfg.symmetric.count,
                        self.cfg.risk.max_order_cost, room,
                    )  # fmt: skip
                    if n >= 1:
                        why = f"{q.reason} ({how}) edge={e}"
                        quotes.append(DesiredQuote(q.ticker, q.side, q.price, n, why))
                        best_edge = max(best_edge, e)
            if t_cfg.tilt_enabled and not quotes:
                v = MarketView(t, fair, f.confidence, b.best_yes_bid, b.best_yes_ask, grid)
                tq = tilt_quote(v, self.cfg.tilt, t_cfg.order_count, min_conf)
                if tq is not None and edge(tq, fair) > 0:
                    n = size_quote(
                        tq, fair, self.cfg.tilt.edge_buffer, t_cfg.edge_full_size,
                        t_cfg.order_count, self.cfg.risk.max_order_cost, room,
                    )  # fmt: skip
                    if n >= 1:
                        e = edge(tq, fair)
                        why = f"{tq.reason} ({how}) edge={e}"
                        quotes.append(DesiredQuote(tq.ticker, tq.side, tq.price, n, why))
                        best_edge = e
            if not quotes:
                continue
            cands.append((t in self.pf.quoted_markets(), best_edge, t, quotes))
        cands.sort(key=lambda c: (not c[0], -c[1], c[2]))
        chosen = {c[2]: c[3] for c in cands[: self.cfg.risk.max_markets_quoted]}
        out: dict[str, list[DesiredQuote] | None] = {t: None for t in self.fairs}
        out.update(chosen)
        return out

    def marks(self, books: Mapping[str, OrderBook]) -> dict[str, Decimal | None]:
        """YES mark per held market: reference mid (real market) if fresh, else the blended
        or model fair, else the live book mid if two-sided, else None (= mark at cost)."""
        now = self._clock()
        out: dict[str, Decimal | None] = {}
        for t, p in self.pf.positions.items():
            if p.qty == 0:
                continue
            f = self.fairs.get(t)
            b = books.get(t)
            mark: Decimal | None = None
            if f is not None and now - f.at <= self.cfg.blend.max_ref_age_seconds:
                rb, ra = self._reference(t, f, b) if b is not None else (f.ref_bid, f.ref_ask)
                two_sided = rb is not None and ra is not None and ra > rb
                mark = (rb + ra) / 2 if two_sided else f.p  # type: ignore[operator]
            if mark is None and b is not None and b.valid and b.mid is not None:
                mark = b.mid
            out[t] = mark
        return out

    def unrealized(self, books: Mapping[str, OrderBook]) -> Decimal:
        return self.pf.mark_to_market_v2(self.marks(books), MARK_HAIRCUT)

    def equity_proxy(self, books: Mapping[str, OrderBook]) -> Decimal:
        return self.pf.realized - self.pf.fees + self.unrealized(books)

    async def step(self, books: Mapping[str, OrderBook], gate_open: bool) -> list[str]:
        """Returns new risk events (each one means: everything was just cancelled)."""
        events = self.risk.update_equity(self.equity_proxy(books))
        if events:
            await self.om.cancel_all("risk stop: " + "; ".join(events))
            self.risk_events.extend(events)
            return events
        can = gate_open and not self.paused and self.risk.can_trade().ok
        await self.om.sync(self.desired(books), self.grids, can)
        if can:
            await self.om.refresh_expiries()
        return []

    async def cancel_all(self, reason: str) -> int:
        return await self.om.cancel_all(reason)


def in_pull_window(minute: int, window: list[int]) -> bool:
    """minute-of-hour inside [start, end), wrapping past :59 (e.g. [50, 6])."""
    if not window:
        return False
    a, b = window
    return a <= minute < b if a <= b else (minute >= a or minute < b)


def describe_mode(cfg: AppConfig) -> str:
    if cfg.trading.trading_enabled:
        return "LIVE ORDER PLACEMENT ENABLED (trading_enabled: true)"
    return "trading disabled: order manager runs DRY (records would-be orders, sends nothing)"
