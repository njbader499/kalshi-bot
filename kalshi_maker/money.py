"""Decimal helpers and per-market tick grids. No floats for money, price or size, ever.

Kalshi sends prices/sizes as fixed-point strings ("0.9600", "278.00") in *_dollars / *_fp
fields. Tick structure comes from the market's `price_ranges` list of {start, end, step}
(field names confirmed in the Get Markets reference; `step` == tick size is an inference,
recorded in DECISIONS.md). We never assume a 1-cent tick.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

ZERO = Decimal(0)
ONE = Decimal(1)
CENT = Decimal("0.01")


class MoneyError(ValueError):
    pass


def to_decimal(value: Any, field: str = "value") -> Decimal:
    """Parse an exchange string into Decimal. Rejects floats outright."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool | float):
        raise MoneyError(f"{field}: refusing non-string numeric {value!r} (float/bool)")
    elif isinstance(value, int | str):
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation as e:
            raise MoneyError(f"{field}: not a decimal: {value!r}") from e
    else:
        raise MoneyError(f"{field}: unsupported type {type(value).__name__}")
    if not d.is_finite():
        raise MoneyError(f"{field}: non-finite {value!r}")
    return d


def opt_decimal(value: Any, field: str = "value") -> Decimal | None:
    if value is None or value == "":
        return None
    return to_decimal(value, field)


def ceil_to_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal


class TickGrid:
    """Valid price levels for one market. Ranges are [start, end] with spacing `step`."""

    def __init__(self, ranges: Iterable[PriceRange]) -> None:
        rs = sorted(ranges, key=lambda r: r.start)
        if not rs:
            raise MoneyError("empty price_ranges")
        for r in rs:
            if not (ZERO <= r.start < r.end <= ONE) or r.step <= 0:
                raise MoneyError(f"bad price range {r}")
            if (r.end - r.start) % r.step != 0:
                raise MoneyError(f"range {r} not a whole number of steps")
        for a, b in pairwise(rs):
            if b.start < a.end:
                raise MoneyError(f"overlapping price ranges {a} {b}")
        self.ranges: tuple[PriceRange, ...] = tuple(rs)

    @classmethod
    def from_market(cls, market: Mapping[str, Any]) -> TickGrid:
        raw = market.get("price_ranges")
        if not raw:
            raise MoneyError(f"market {market.get('ticker')} has no price_ranges; won't guess tick")
        return cls(
            PriceRange(
                to_decimal(r["start"], "price_ranges.start"),
                to_decimal(r["end"], "price_ranges.end"),
                to_decimal(r["step"], "price_ranges.step"),
            )
            for r in raw
        )

    @property
    def min_price(self) -> Decimal:
        r = self.ranges[0]
        return r.start if r.start > ZERO else r.start + r.step

    @property
    def max_price(self) -> Decimal:
        r = self.ranges[-1]
        return r.end if r.end < ONE else r.end - r.step

    def is_valid(self, p: Decimal) -> bool:
        if not (self.min_price <= p <= self.max_price):
            return False
        return any(r.start <= p <= r.end and (p - r.start) % r.step == 0 for r in self.ranges)

    def _levels_near(self, p: Decimal) -> tuple[Decimal | None, Decimal | None]:
        """Highest valid level <= p and lowest valid level >= p."""
        below: Decimal | None = None
        above: Decimal | None = None
        for r in self.ranges:
            if p >= r.start:
                k = ((min(p, r.end) - r.start) / r.step).to_integral_value(ROUND_FLOOR)
                cand = r.start + k * r.step
                if below is None or cand > below:
                    below = cand
            if p <= r.end:
                k = ((max(p, r.start) - r.start) / r.step).to_integral_value(ROUND_CEILING)
                cand = r.start + k * r.step
                if above is None or cand < above:
                    above = cand
        return below, above

    def round_down(self, p: Decimal) -> Decimal | None:
        """For bids: largest valid tick <= p, or None if p is below the tradable band."""
        below, _ = self._levels_near(min(p, self.max_price))
        if below is None or below < self.min_price:
            return None
        return below

    def round_up(self, p: Decimal) -> Decimal | None:
        """For asks: smallest valid tick >= p, or None if p is above the tradable band."""
        _, above = self._levels_near(max(p, self.min_price))
        if above is None or above > self.max_price:
            return None
        return above

    def tick_at(self, p: Decimal) -> Decimal:
        for r in self.ranges:
            if r.start <= p < r.end:
                return r.step
        return self.ranges[-1].step
