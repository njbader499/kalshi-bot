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
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from .book import OrderBook
from .ledger import Ledger
from .money import TickGrid
from .orders import DryRunExecutor, Executor, LiveExecutor, OrderManager
from .portfolio import MarketMeta, PortfolioState, Position
from .rest import KalshiREST
from .risk import RiskManager
from .settings import AppConfig, RiskLimits
from .strategy.tilt import DesiredQuote, MarketView, tilt_quote

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FairInput:
    p: Decimal
    confidence: float
    at: float  # unix time computed


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
        self, ticker: str, p: Decimal, confidence: float, at: float | None = None
    ) -> None:
        self.fairs[ticker] = FairInput(p, confidence, self._clock() if at is None else at)

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
    def desired(self, books: Mapping[str, OrderBook]) -> dict[str, DesiredQuote | None]:
        now = self._clock()
        t_cfg = self.cfg.trading
        cands: list[tuple[bool, Decimal, DesiredQuote]] = []
        for t, f in self.fairs.items():
            b = books.get(t)
            grid = self.grids.get(t)
            if b is None or not b.valid or grid is None or t not in self.pf.meta:
                continue
            if t in self.off_markets or self.pf.meta[t].category in self.off_categories:
                continue
            if now - f.at > t_cfg.max_fair_age_seconds:
                continue
            v = MarketView(t, f.p, f.confidence, b.best_yes_bid, b.best_yes_ask, grid)
            q = tilt_quote(v, self.cfg.tilt, t_cfg.order_count, self.cfg.weather.min_confidence)
            if q is None or edge(q, f.p) <= 0:
                continue
            n = size_quote(
                q, f.p, self.cfg.tilt.edge_buffer, t_cfg.edge_full_size, t_cfg.order_count,
                self.cfg.risk.max_order_cost, room_for(self.pf, t, self.cfg.risk),
            )  # fmt: skip
            if n < 1:
                continue  # no room under the caps for even one contract
            q = DesiredQuote(q.ticker, q.side, q.price, n, q.reason + f" edge={edge(q, f.p)}")
            cands.append((t in self.pf.quoted_markets(), edge(q, f.p), q))
        cands.sort(key=lambda c: (not c[0], -c[1], c[2].ticker))
        chosen = {c[2].ticker: c[2] for c in cands[: self.cfg.risk.max_markets_quoted]}
        out: dict[str, DesiredQuote | None] = {t: None for t in self.fairs}
        out.update(chosen)
        return out

    def equity_proxy(self, books: Mapping[str, OrderBook]) -> Decimal:
        bids = {t: b.best_yes_bid for t, b in books.items()}
        asks = {t: b.best_yes_ask for t, b in books.items()}
        return self.pf.realized - self.pf.fees + self.pf.mark_to_market(bids, asks)

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


def describe_mode(cfg: AppConfig) -> str:
    if cfg.trading.trading_enabled:
        return "LIVE ORDER PLACEMENT ENABLED (trading_enabled: true)"
    return "trading disabled: order manager runs DRY (records would-be orders, sends nothing)"
