"""Paper trading on PROD ("prod-paper"): the strategy's own quotes against the real order
book and the real trade tape, with NO orders. This is the evidence that decides whether the
maker strategy is worth real money. Demo fills are not: the demo book is thin and stale.

Each cycle (the shadow cadence, every weather.fair_interval_minutes):
  1. Fetch prod trades since the last cycle for every market with resting paper quotes.
  2. Walk them in time order against the quotes that were resting when they printed:
       * PESSIMISTIC fill: a trade strictly THROUGH our price, up to its size (the taker
         accepted a worse price, so under price priority they would have hit us first), or
         trades AT our price whose cumulative size since we posted exceeds the size that
         was already ahead of us in the queue (0 if we improved the book);
       * OPTIMISTIC fill: any trade at or through our price, up to its size (as if we had
         been first in line).
     The pessimistic book is the one that counts; the optimistic one bounds it from above.
     Taker side semantics (Kalshi trade tape): taker_side "no" = the taker bought NO = sold
     YES = hit YES bids (our `bid`); taker_side "yes" = the taker bought YES = hit NO bids
     (our `ask`, a NO bid at 1 - price).
  3. Settle positions whose market has settled (prod `result`), mark the rest at the mid.
  4. Re-quote: the strategy's desired quotes against the prod book (cancel/replace), with
     the size ahead of each quote recorded from the book it was posted into.

State (quotes, positions, realized P&L, per-market trade cursors) lives in
<data_dir>/paper_state.json; fills go to the ledger's append-only `paper_fills` table.

UNVERIFIED (confirm with one live `python -m kalshi_maker trades --ticker ...`): trade
field names. parse_trade accepts `yes_price_dollars`/`count_fp` (fixed-point strings) and
falls back to `yes_price`/`count` (integer cents / contracts), `taker_side` ("yes"/"no"),
`created_time` (ISO 8601) and `trade_id`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .book import OrderBook
from .ledger import Ledger
from .money import ONE, ZERO, to_decimal
from .portfolio import PortfolioState, Position
from .rest import KalshiREST, OrderbookLevels
from .strategy.tilt import DesiredQuote

log = logging.getLogger(__name__)

UNSET = Decimal(-1)


# ---- trade tape ----


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    ticker: str
    ts: float  # unix seconds
    yes_price: Decimal
    count: Decimal
    taker_side: str  # "yes" | "no"


def parse_trade(t: Mapping[str, Any]) -> Trade:
    yp = t.get("yes_price_dollars")
    if yp is None:
        cents = t.get("yes_price")
        if cents is None:
            raise ValueError("trade without a yes price")
        yes_price = to_decimal(cents, "yes_price") / 100
    else:
        yes_price = to_decimal(yp, "yes_price_dollars")
    cf = t.get("count_fp")
    count = to_decimal(cf, "count_fp") if cf is not None else to_decimal(t.get("count"), "count")
    side = str(t.get("taker_side") or "").lower()
    if side not in ("yes", "no"):
        raise ValueError(f"trade with unknown taker_side {side!r}")
    created = str(t.get("created_time") or "")
    ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    return Trade(
        str(t.get("trade_id") or ""), str(t.get("ticker") or ""), ts, yes_price, count, side
    )


# ---- quotes and fills ----


@dataclass
class PaperQuote:
    ticker: str
    side: str  # "bid" (buy YES) | "ask" (buy NO at 1 - price)
    price: Decimal  # YES price
    count: Decimal
    posted_at: float
    queue_ahead: Decimal  # size at our level when we posted (0 = we improved the book)
    # UNSET (negative) means "a fresh quote": remaining = count. 0 is a FILLED quote and must
    # survive a save/load round trip as 0, or a restart would fill it again.
    remaining_pess: Decimal = field(default=UNSET)
    remaining_opt: Decimal = field(default=UNSET)
    traded_at_price: Decimal = field(default=ZERO)  # cumulative volume at our price since posting

    def __post_init__(self) -> None:
        if self.remaining_pess < 0:
            self.remaining_pess = self.count
        if self.remaining_opt < 0:
            self.remaining_opt = self.count

    def hits(self, t: Trade) -> tuple[Decimal, Decimal]:
        """(pessimistic fill, optimistic fill) sizes this trade would give us."""
        if t.ts < self.posted_at or t.ticker != self.ticker:
            return ZERO, ZERO
        if self.side == "bid":
            if t.taker_side != "no":  # only takers SELLING yes hit a YES bid
                return ZERO, ZERO
            through = t.yes_price < self.price
            at = t.yes_price == self.price
        else:
            if t.taker_side != "yes":  # only takers BUYING yes hit a NO bid
                return ZERO, ZERO
            through = t.yes_price > self.price
            at = t.yes_price == self.price
        if not (through or at):
            return ZERO, ZERO
        opt = min(self.remaining_opt, t.count)
        if through:
            # The taker accepted a worse price than ours, so under price priority they would
            # have hit us first, for at least this trade's size (not for our whole quote: the
            # taker's order may have been exactly this big).
            pess = min(self.remaining_pess, t.count)
        else:
            self.traded_at_price += t.count
            beyond_queue = self.traded_at_price - self.queue_ahead
            pess = min(self.remaining_pess, max(ZERO, beyond_queue)) if beyond_queue > 0 else ZERO
            # only the part of THIS trade that reaches us counts
            pess = min(pess, t.count)
        return pess, opt


@dataclass(frozen=True, slots=True)
class PaperFill:
    mode: str  # "pess" | "opt"
    ticker: str
    side: str
    price: Decimal
    count: Decimal
    trade_id: str
    trade_ts: float
    queue_ahead: Decimal


def apply_trades(quotes: Iterable[PaperQuote], trades: Iterable[Trade]) -> list[PaperFill]:
    """Walk trades in time order against the resting quotes. Mutates remaining sizes."""
    fills: list[PaperFill] = []
    qs = list(quotes)
    for t in sorted(trades, key=lambda x: (x.ts, x.trade_id)):
        for q in qs:
            if q.remaining_pess <= 0 and q.remaining_opt <= 0:
                continue
            pess, opt = q.hits(t)
            for mode, n in (("pess", pess), ("opt", opt)):
                if n <= 0:
                    continue
                if mode == "pess":
                    q.remaining_pess -= n
                else:
                    q.remaining_opt -= n
                fills.append(
                    PaperFill(mode, q.ticker, q.side, q.price, n, t.trade_id, t.ts, q.queue_ahead)
                )
    return fills


def queue_ahead(levels: OrderbookLevels, side: str, price: Decimal) -> Decimal:
    """Size already resting at our price when we post (0 when we improve the book)."""
    if side == "bid":
        for p, s in levels.yes_bids:
            if p == price:
                return s
        return ZERO
    no_price = ONE - price
    for p, s in levels.no_bids:
        if p == no_price:
            return s
    return ZERO


def levels_to_book(ticker: str, levels: OrderbookLevels, now_mono: float) -> OrderBook:
    b = OrderBook(ticker)
    b.replace(
        [(str(p), str(s)) for p, s in levels.yes_bids],
        [(str(p), str(s)) for p, s in levels.no_bids],
        clock=0,
        now_mono=now_mono,
    )
    return b


def settle_position(pf: PortfolioState, ticker: str, result_yes: bool) -> Decimal:
    """Realise a settled position: YES pays 1 if yes, NO pays 1 if no. Returns the P&L."""
    pos = pf.positions.get(ticker)
    if pos is None or pos.qty == 0:
        pf.positions.pop(ticker, None)
        return ZERO
    won = result_yes if pos.qty > 0 else not result_yes
    payout = abs(pos.qty) if won else ZERO
    pnl = payout - pos.cost
    pf.realized += pnl
    del pf.positions[ticker]
    return pnl


# ---- persistent state ----


@dataclass
class PaperState:
    quotes: list[dict[str, Any]] = field(default_factory=list)
    positions_pess: dict[str, dict[str, str]] = field(default_factory=dict)
    positions_opt: dict[str, dict[str, str]] = field(default_factory=dict)
    realized_pess: str = "0"
    realized_opt: str = "0"
    cursor: dict[str, float] = field(default_factory=dict)  # ticker -> last trade ts seen
    settled: dict[str, str] = field(default_factory=dict)  # ticker -> result
    fills_pess: int = 0
    fills_opt: int = 0
    started_at: float = 0.0


def load_state(path: Path) -> PaperState:
    if not path.exists():
        return PaperState(started_at=time.time())
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return PaperState(**{k: v for k, v in d.items() if k in PaperState.__dataclass_fields__})
    except Exception as e:  # a corrupt state file must not stop the collector
        log.error("paper state unreadable; starting fresh", extra={"err": repr(e)})
        return PaperState(started_at=time.time())


def save_state(path: Path, st: PaperState) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(st), indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _pf_from(positions: Mapping[str, Mapping[str, str]], realized: str) -> PortfolioState:
    pf = PortfolioState()
    for t, p in positions.items():
        pf.positions[t] = Position(t, Decimal(p["qty"]), Decimal(p["cost"]))
    pf.realized = Decimal(realized)
    return pf


def _pf_to(pf: PortfolioState) -> dict[str, dict[str, str]]:
    return {t: {"qty": str(p.qty), "cost": str(p.cost)} for t, p in pf.positions.items() if p.qty}


# ---- the simulator ----


class PaperTrader:
    """Runs inside the collector. `desired` is the strategy's quote function evaluated on
    the prod books (a TradingLoop with live_book_is_real=True and the pessimistic
    portfolio as its positions)."""

    def __init__(
        self,
        prod: KalshiREST,
        ledger: Ledger | None,
        state_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.prod = prod
        self.ledger = ledger
        self.state_path = state_path
        self._clock = clock
        self.state = load_state(state_path)
        self.pess = _pf_from(self.state.positions_pess, self.state.realized_pess)
        self.opt = _pf_from(self.state.positions_opt, self.state.realized_opt)
        self.quotes: list[PaperQuote] = [
            PaperQuote(
                q["ticker"], q["side"], Decimal(q["price"]), Decimal(q["count"]),
                float(q["posted_at"]), Decimal(q["queue_ahead"]),
                Decimal(q.get("remaining_pess", q["count"])),
                Decimal(q.get("remaining_opt", q["count"])),
                Decimal(q.get("traded_at_price", "0")),
            )
            for q in self.state.quotes
        ]
        self.last_fills: list[PaperFill] = []
        self.on_fill: Callable[[PaperFill], None] | None = None

    # -- step 1+2: trades -> fills --
    async def process_trades(self) -> list[PaperFill]:
        now = self._clock()
        tickers = sorted({q.ticker for q in self.quotes})
        trades: list[Trade] = []
        for t in tickers:
            first_post = min(q.posted_at for q in self.quotes if q.ticker == t)
            since = int(self.state.cursor.get(t, first_post))
            try:
                async for raw in self.prod.iter_trades(t, min_ts=since - 1):
                    try:
                        tr = parse_trade(raw)
                    except Exception as e:
                        log.warning("unparseable trade", extra={"ticker": t, "err": repr(e)})
                        continue
                    if tr.ts > self.state.cursor.get(t, 0):
                        trades.append(tr)
            except Exception as e:
                log.warning("trade tape fetch failed", extra={"ticker": t, "err": repr(e)})
        seen: set[str] = set()
        new: list[Trade] = []
        for tr in trades:
            if tr.trade_id not in seen:
                seen.add(tr.trade_id)
                new.append(tr)
        fills = apply_trades(self.quotes, new)
        for tr in new:
            self.state.cursor[tr.ticker] = max(self.state.cursor.get(tr.ticker, 0), tr.ts)
        for f in fills:
            pf = self.pess if f.mode == "pess" else self.opt
            pf.apply_fill(f.ticker, f.side, f.price, f.count)
            if f.mode == "pess":
                self.state.fills_pess += 1
            else:
                self.state.fills_opt += 1
            if self.ledger is not None:
                await self.ledger.record_paper_fill(f, now)
            if self.on_fill is not None and f.mode == "pess":
                self.on_fill(f)
        self.last_fills = fills
        return fills

    # -- step 3: settlements --
    async def settle(self, open_tickers: set[str]) -> list[tuple[str, str, Decimal, Decimal]]:
        """Settle positions whose market is no longer open. Returns (ticker, result, pnl_pess,
        pnl_opt) per settled market."""
        out: list[tuple[str, str, Decimal, Decimal]] = []
        held = set(self.pess.positions) | set(self.opt.positions)
        for t in sorted(held - open_tickers):
            try:
                m = await self.prod.get_market(t)
            except Exception as e:
                log.warning("paper settle: fetch failed", extra={"ticker": t, "err": repr(e)})
                continue
            result = str(m.get("result") or "")
            if result not in ("yes", "no"):
                continue  # closed but not settled yet
            p1 = settle_position(self.pess, t, result == "yes")
            p2 = settle_position(self.opt, t, result == "yes")
            self.state.settled[t] = result
            out.append((t, result, p1, p2))
        return out

    # -- step 4: re-quote --
    def requote(
        self,
        desired: Mapping[str, list[DesiredQuote] | None],
        levels: Mapping[str, OrderbookLevels],
    ) -> int:
        now = self._clock()
        new: list[PaperQuote] = []
        for t, qs in desired.items():
            if not qs or t not in levels:
                continue
            for q in qs:
                ahead = queue_ahead(levels[t], q.side, q.price)
                new.append(PaperQuote(t, q.side, q.price, q.count, now, ahead))
        self.quotes = new
        return len(new)

    def unrealized(
        self, mids: Mapping[str, Decimal | None], haircut: Decimal
    ) -> tuple[Decimal, Decimal]:
        return (
            self.pess.mark_to_market_v2(mids, haircut),
            self.opt.mark_to_market_v2(mids, haircut),
        )

    def persist(self) -> None:
        self.state.quotes = [
            {**{k: str(v) for k, v in asdict(q).items()}} for q in self.quotes
        ]
        self.state.positions_pess = _pf_to(self.pess)
        self.state.positions_opt = _pf_to(self.opt)
        self.state.realized_pess = str(self.pess.realized)
        self.state.realized_opt = str(self.opt.realized)
        save_state(self.state_path, self.state)

    def summary(self, mids: Mapping[str, Decimal | None], haircut: Decimal) -> str:
        up, uo = self.unrealized(mids, haircut)
        days = max(1.0, (self._clock() - (self.state.started_at or self._clock())) / 86400)
        open_p = sum(1 for p in self.pess.positions.values() if p.qty)
        return (
            f"PROD-PAPER (since {days:.1f}d): pessimistic realized {self.pess.realized:+.2f} "
            f"unrealized {up:+.2f} ({self.state.fills_pess} fills, {open_p} open) | optimistic "
            f"realized {self.opt.realized:+.2f} unrealized {uo:+.2f} ({self.state.fills_opt} fills)"
            f" | {len(self.quotes)} quotes resting | {len(self.state.settled)} markets settled"
        )
