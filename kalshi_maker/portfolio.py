"""Portfolio state for risk checks: positions, resting bot orders, worst-case exposure.

Exposure is the WORST CASE: the cost of current positions plus every resting buy filled at
its limit price. On Kalshi every order is a purchase in V2 terms: `bid` buys YES at p (cost
p per contract) and `ask` buys NO at 1 - p (cost 1 - p). So the cost of a resting order
is known exactly, and a maker fill can only happen at or better than our price.

Position sign: + = YES contracts, - = NO contracts (Kalshi `position_fp` convention).
Cost basis uses average cost; reducing a position releases cost proportionally. This is
for risk only. Tax lots (FIFO) are computed from the ledger in Phase 7.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from .money import ONE, ZERO


@dataclass(frozen=True, slots=True)
class MarketMeta:
    event: str
    category: str


@dataclass
class RestingOrder:
    order_id: str
    client_order_id: str
    ticker: str
    side: str  # "bid" (buy YES) | "ask" (buy NO)
    price: Decimal  # YES price
    remaining: Decimal
    expiration_time: int = 0

    @property
    def unit_cost(self) -> Decimal:
        return self.price if self.side == "bid" else ONE - self.price

    @property
    def cost(self) -> Decimal:
        return self.unit_cost * self.remaining


@dataclass
class Position:
    ticker: str
    qty: Decimal = ZERO  # + YES, - NO
    cost: Decimal = ZERO  # cost basis of the open contracts (>= 0)


@dataclass
class PortfolioState:
    meta: dict[str, MarketMeta] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    resting: dict[str, RestingOrder] = field(default_factory=dict)  # by order_id
    realized: Decimal = ZERO  # cumulative realized P&L (excl. fees)
    fees: Decimal = ZERO

    # ---- exposure (worst case) ----
    def _meta(self, ticker: str) -> MarketMeta:
        m = self.meta.get(ticker)
        if m is None:
            raise KeyError(f"no event/category metadata for {ticker}; refusing to size risk")
        return m

    def market_exposure(self, ticker: str) -> Decimal:
        pos = self.positions.get(ticker)
        e = pos.cost if pos else ZERO
        return e + sum((o.cost for o in self.resting.values() if o.ticker == ticker), ZERO)

    def _sum_where(self, pred: str, value: str) -> Decimal:
        tickers = {t for t in self._all_tickers() if getattr(self._meta(t), pred) == value}
        return sum((self.market_exposure(t) for t in tickers), ZERO)

    def event_exposure(self, event: str) -> Decimal:
        return self._sum_where("event", event)

    def category_exposure(self, category: str) -> Decimal:
        return self._sum_where("category", category)

    def total_exposure(self) -> Decimal:
        return sum((self.market_exposure(t) for t in self._all_tickers()), ZERO)

    def _all_tickers(self) -> set[str]:
        return {t for t, p in self.positions.items() if p.qty != 0 or p.cost != 0} | {
            o.ticker for o in self.resting.values()
        }

    def quoted_markets(self) -> set[str]:
        return {o.ticker for o in self.resting.values()}

    def resting_for(self, ticker: str) -> list[RestingOrder]:
        return [o for o in self.resting.values() if o.ticker == ticker]

    # ---- updates ----
    def add_resting(self, o: RestingOrder) -> None:
        self.resting[o.order_id] = o

    def remove_resting(self, order_id: str) -> None:
        self.resting.pop(order_id, None)

    def apply_fill(
        self, ticker: str, side: str, price: Decimal, count: Decimal, fee: Decimal = ZERO,
        order_id: str | None = None,
    ) -> None:  # fmt: skip
        """A fill of a buy: `bid` adds YES at price, `ask` adds NO at 1 - price."""
        if count <= 0:
            return
        if order_id and order_id in self.resting:
            o = self.resting[order_id]
            o.remaining = max(ZERO, o.remaining - count)
            if o.remaining == 0:
                del self.resting[order_id]
        pos = self.positions.setdefault(ticker, Position(ticker))
        signed = count if side == "bid" else -count
        unit = price if side == "bid" else ONE - price
        self.fees += fee
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):  # opening / adding
            pos.qty += signed
            pos.cost += unit * count
            return
        # Opposite side: each YES+NO pair is worth exactly $1, so closing realizes
        # (1 - avg cost of the held side - cost of the new side) per pair.
        held = abs(pos.qty)
        close = min(held, count)
        avg = pos.cost / held
        self.realized += (ONE - avg - unit) * close
        if count < held:  # partial close: same side, smaller
            pos.qty += signed
            pos.cost -= avg * close
        elif count == held:  # flat
            pos.qty, pos.cost = ZERO, ZERO
        else:  # closed and flipped to the other side
            rest = count - held
            pos.qty = rest if signed > 0 else -rest
            pos.cost = unit * rest

    def mark_to_market_v2(
        self, yes_mark: Mapping[str, Decimal | None], haircut: Decimal
    ) -> Decimal:
        """Unrealized P&L with each position valued at a YES mark less a haircut (NO at
        1 - mark less the haircut). A market with no mark is carried at cost (0 unrealized)
        rather than at 0 value: an empty book is missing information, not a loss."""
        u = ZERO
        for t, p in self.positions.items():
            if p.qty == 0:
                continue
            m = yes_mark.get(t)
            if m is None:
                continue
            m = min(ONE, max(ZERO, m))
            if p.qty > 0:
                value = max(ZERO, m - haircut) * p.qty
            else:
                value = max(ZERO, (ONE - m) - haircut) * (-p.qty)
            u += value - p.cost
        return u

    def mark_to_market(
        self,
        yes_bid: Mapping[str, Decimal | None],
        yes_ask: Mapping[str, Decimal | None],
    ) -> Decimal:
        """Unrealized P&L, CONSERVATIVE: YES valued at best YES bid, NO at best NO bid
        (= 1 - best YES ask). No bid means a value of 0."""
        u = ZERO
        for t, p in self.positions.items():
            if p.qty > 0:
                b = yes_bid.get(t)
                u += (b if b is not None else ZERO) * p.qty - p.cost
            elif p.qty < 0:
                a = yes_ask.get(t)
                nb = ONE - a if a is not None else ZERO
                u += nb * (-p.qty) - p.cost
        return u
