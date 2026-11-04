"""Local order book for one market (Decimal only).

Kalshi books hold BIDS ONLY on each side (docs: getting_started/orderbook_responses):
  * YES bids at YES prices
  * NO bids at NO prices (NO-leg pricing)
A NO bid at q is an offer to sell YES at 1 - q, so:
  best YES ask = 1 - best NO bid       best NO ask = 1 - best YES bid

Internally both sides are stored in their own leg's pricing (REST convention). The WS feed
is subscribed with `use_yes_price: true` (Kalshi will make that the default), which reports
NO levels in YES-leg pricing. The parser converts them back with `no_leg=False`, so the
book is identical whichever feed built it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import ONE, ZERO, to_decimal

Levels = dict[Decimal, Decimal]  # price -> size


class BookError(ValueError):
    pass


@dataclass
class OrderBook:
    ticker: str
    yes: Levels = field(default_factory=dict)  # YES bids, YES prices
    no: Levels = field(default_factory=dict)  # NO bids, NO prices
    clock: int = 0  # exchange clock of the newest change applied
    valid: bool = False  # False until a snapshot arrives; False again after a gap
    updated_mono: float = 0.0  # time.monotonic() of the last applied message

    # ---- mutation ----
    def replace(
        self,
        yes_levels: Iterable[tuple[Any, Any]],
        no_levels: Iterable[tuple[Any, Any]],
        clock: int,
        now_mono: float,
        no_in_yes_price: bool = False,
    ) -> None:
        """Replace the whole book from a snapshot (never merged into the old one)."""
        self.yes = _parse_levels(yes_levels, "yes", in_yes_price=False)
        self.no = _parse_levels(no_levels, "no", in_yes_price=no_in_yes_price)
        self.clock = clock
        self.valid = True
        self.updated_mono = now_mono

    def apply_delta(
        self,
        side: str,
        price: Any,
        delta: Any,
        clock: int,
        now_mono: float,
        no_in_yes_price: bool = False,
    ) -> None:
        if not self.valid:
            raise BookError(f"{self.ticker}: delta applied to an invalid book")
        p = to_decimal(price, "price_dollars")
        d = to_decimal(delta, "delta_fp")
        if side == "yes":
            levels = self.yes
        elif side == "no":
            levels = self.no
            if no_in_yes_price:
                p = ONE - p
        else:
            raise BookError(f"{self.ticker}: unknown side {side!r}")
        _check_price(p, side)
        new = levels.get(p, ZERO) + d
        if new < ZERO:
            # Our book disagrees with the exchange: treat as a gap and resync.
            self.valid = False
            raise BookError(f"{self.ticker}: {side} level {p} would go negative ({new})")
        if new == ZERO:
            levels.pop(p, None)
        else:
            levels[p] = new
        self.clock = max(self.clock, clock)
        self.updated_mono = now_mono

    def invalidate(self) -> None:
        self.valid = False

    # ---- queries ----
    @property
    def best_yes_bid(self) -> Decimal | None:
        return max(self.yes) if self.yes else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return max(self.no) if self.no else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        b = self.best_no_bid
        return None if b is None else ONE - b

    @property
    def best_no_ask(self) -> Decimal | None:
        b = self.best_yes_bid
        return None if b is None else ONE - b

    def size_at_yes_bid(self) -> Decimal:
        b = self.best_yes_bid
        return ZERO if b is None else self.yes[b]

    def size_at_yes_ask(self) -> Decimal:
        b = self.best_no_bid
        return ZERO if b is None else self.no[b]

    @property
    def mid(self) -> Decimal | None:
        b, a = self.best_yes_bid, self.best_yes_ask
        if b is None or a is None:
            return None
        return (b + a) / 2

    @property
    def crossed(self) -> bool:
        """YES bid >= YES ask (i.e. yes_bid + no_bid >= 1). Never true on a healthy book."""
        b, a = self.best_yes_bid, self.best_yes_ask
        return b is not None and a is not None and b >= a

    def age(self, now_mono: float) -> float:
        return now_mono - self.updated_mono

    def summary(self) -> dict[str, str | None]:
        def s(x: Decimal | None) -> str | None:
            return None if x is None else str(x)

        return {
            "ticker": self.ticker,
            "yes_bid": s(self.best_yes_bid),
            "yes_ask": s(self.best_yes_ask),
            "bid_size": str(self.size_at_yes_bid()),
            "ask_size": str(self.size_at_yes_ask()),
            "valid": str(self.valid),
        }


def _check_price(p: Decimal, side: str) -> None:
    if not (ZERO < p < ONE):
        raise BookError(f"{side} price {p} outside (0, 1)")


def _parse_levels(raw: Iterable[tuple[Any, Any]], side: str, in_yes_price: bool) -> Levels:
    out: Levels = {}
    for pair in raw or []:
        p_raw, s_raw = pair
        p = to_decimal(p_raw, f"{side}.price")
        if in_yes_price:
            p = ONE - p
        _check_price(p, side)
        size = to_decimal(s_raw, f"{side}.size")
        if size < ZERO:
            raise BookError(f"{side} level {p} has negative size {size}")
        if size > ZERO:
            out[p] = out.get(p, ZERO) + size
    return out


def book_from_rest(ticker: str, data: Mapping[str, Any], now_mono: float) -> OrderBook:
    """Build from GET /markets/{ticker}/orderbook (NO levels already in NO-leg pricing)."""
    ob = data.get("orderbook_fp")
    if not isinstance(ob, Mapping):
        raise BookError("orderbook response missing 'orderbook_fp'")
    b = OrderBook(ticker)
    b.replace(ob.get("yes_dollars") or [], ob.get("no_dollars") or [], 0, now_mono)
    return b
