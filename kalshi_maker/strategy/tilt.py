"""Mode A, `tilt` (Section 3): favorite / NO tilt. Pure function, no I/O.

Rest bids ONLY on the side the favorite-longshot bias favors:
  * fair >= favorite_threshold -> buy YES (V2 `bid`)
      price = min(fair - edge_buffer, best_yes_bid + 1 tick), rounded DOWN to the grid
  * fair <= longshot_threshold -> buy NO (V2 `ask` at YES price 1 - no_price)
      no_price = min((1 - fair) - edge_buffer, best_no_bid + 1 tick), rounded DOWN in NO
      terms, i.e. the YES-side ask is rounded UP on the YES grid
  * otherwise: no quote.
Guards:
  * never bid at or through the opposite best (post_only would reject it; check first)
  * never buy YES below min_yes_price, never buy NO below min_no_price (longshots)
  * low-confidence or stale fair values -> no quote (the caller filters; checked here too)
  * position at the market cap -> no new buying in that market (risk also enforces)
Take-profit exits are optional in the spec and not implemented yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..money import ONE, TickGrid
from ..settings import TiltCfg


@dataclass(frozen=True, slots=True)
class DesiredQuote:
    ticker: str
    side: str  # "bid" = buy YES, "ask" = buy NO
    price: Decimal  # YES price on the market grid
    count: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class MarketView:
    ticker: str
    fair: Decimal
    confidence: float
    yes_bid: Decimal | None
    yes_ask: Decimal | None  # = 1 - best NO bid
    grid: TickGrid


def tilt_quote(
    v: MarketView, cfg: TiltCfg, count: Decimal, min_confidence: float
) -> DesiredQuote | None:
    if v.confidence < min_confidence:
        return None
    if v.fair >= cfg.favorite_threshold:
        return _yes_bid(v, cfg, count)
    if v.fair <= cfg.longshot_threshold:
        return _no_bid(v, cfg, count)
    return None


def _yes_bid(v: MarketView, cfg: TiltCfg, count: Decimal) -> DesiredQuote | None:
    target = v.fair - cfg.edge_buffer
    if v.yes_bid is not None:
        target = min(target, v.yes_bid + v.grid.tick_at(v.yes_bid))
    price = v.grid.round_down(target)
    if price is None or price < cfg.min_yes_price:
        return None
    if v.yes_ask is not None and price >= v.yes_ask:
        price = v.grid.round_down(v.yes_ask - v.grid.tick_at(v.yes_ask))
        if price is None or price < cfg.min_yes_price:
            return None
    if price > v.fair - cfg.edge_buffer:  # defensive: never pay above fair - buffer
        return None
    return DesiredQuote(v.ticker, "bid", price, count, f"YES favorite fair={v.fair}")


def _no_bid(v: MarketView, cfg: TiltCfg, count: Decimal) -> DesiredQuote | None:
    no_fair = ONE - v.fair
    target_no = no_fair - cfg.edge_buffer
    best_no_bid = None if v.yes_ask is None else ONE - v.yes_ask
    if best_no_bid is not None:
        # one tick better than the best NO bid, measured on the YES grid at the mirror price
        target_no = min(target_no, best_no_bid + v.grid.tick_at(v.yes_ask))  # type: ignore[arg-type]
    yes_price = v.grid.round_up(ONE - target_no)  # NO rounded DOWN == YES ask rounded UP
    if yes_price is None:
        return None
    no_price = ONE - yes_price
    if no_price < cfg.min_no_price:
        return None
    # crossing check: buying NO at no_price crosses if no_price >= best NO ask (= 1 - yes_bid)
    if v.yes_bid is not None and yes_price <= v.yes_bid:
        yes_price2 = v.grid.round_up(v.yes_bid + v.grid.tick_at(v.yes_bid))
        if yes_price2 is None:
            return None
        yes_price, no_price = yes_price2, ONE - yes_price2
        if no_price < cfg.min_no_price:
            return None
    if no_price > no_fair - cfg.edge_buffer:
        return None
    return DesiredQuote(v.ticker, "ask", yes_price, count, f"NO on longshot fair={v.fair}")
