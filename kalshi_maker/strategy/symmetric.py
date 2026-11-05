"""Mode S, `symmetric`: two resting bids around the blended fair value. Pure function.

The structural maker edge (Becker, 72M trades: makers about +1.1% vs takers about -1.1%,
2.2-2.7pp in weather) is earned by RESTING on both sides and letting takers cross the
spread, not by betting against the market's view. So this mode quotes

    YES bid at  fair - skew - half_spread   (V2 `bid`)
    NO  bid at  (1 - fair + skew) - half_spread, i.e. a YES ask at fair - skew + half_spread
                                               (V2 `ask`, cost 1 - price)

inside the current book, never crossing it, only when the book's own spread is wide enough
to leave room (min_market_spread_ticks). Inventory skew moves both quotes away from the
side already held (long YES -> both quotes lower, so the YES bid is less likely to fill and
the NO bid more likely); past max_inventory only the reducing quote is posted. A YES fill
and a NO fill in the same market net to cash on Kalshi, which is how the spread is realised.

Guards: prices on the market's tick grid and inside [min_price, 1 - min_price]; the YES bid
never above fair - half_spread/2 and the YES ask never below fair + half_spread/2, whatever
the skew (the reducing side may give up half its edge to shed inventory, no more).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..money import ONE, TickGrid
from ..settings import SymmetricCfg
from .tilt import DesiredQuote


@dataclass(frozen=True, slots=True)
class SymView:
    ticker: str
    fair: Decimal  # blended fair value (see trading.blended_fair)
    yes_bid: Decimal | None
    yes_ask: Decimal | None  # = 1 - best NO bid
    grid: TickGrid
    position: Decimal = Decimal(0)  # + YES, - NO (Kalshi position_fp convention)


def symmetric_quotes(v: SymView, cfg: SymmetricCfg, count: Decimal) -> list[DesiredQuote]:
    if not cfg.enabled or v.yes_bid is None or v.yes_ask is None:
        return []
    if v.yes_ask <= v.yes_bid:
        return []  # crossed/locked book: let it sort itself out
    tick = v.grid.tick_at(v.yes_bid)
    if v.yes_ask - v.yes_bid < tick * cfg.min_market_spread_ticks:
        return []
    center = v.fair - cfg.skew_per_contract * v.position
    half = cfg.half_spread
    out: list[DesiredQuote] = []
    hi_cap = ONE - cfg.min_price

    if v.position < cfg.max_inventory:  # may add YES
        # inventory skew may tighten the reducing side, but never past half the edge
        target = min(center - half, v.fair - half / 2, v.yes_ask - tick)
        px = v.grid.round_down(target)
        if px is not None and cfg.min_price <= px <= hi_cap:
            out.append(DesiredQuote(v.ticker, "bid", px, count, f"sym bid fair={v.fair}"))

    if v.position > -cfg.max_inventory:  # may add NO
        target = max(center + half, v.fair + half / 2, v.yes_bid + tick)
        px = v.grid.round_up(target)
        if px is not None and cfg.min_price <= px <= hi_cap:
            out.append(DesiredQuote(v.ticker, "ask", px, count, f"sym ask fair={v.fair}"))

    if len(out) == 2 and out[0].price >= out[1].price:
        return []  # can't happen with half_spread > 0, but never post a self-crossing pair
    return out
