"""Kalshi trading fee math.

fee = ceil_to_cent(rate * contracts * P * (1 - P))
  * taker rate 0.07 by default (Kalshi fee schedule)
  * maker rate 0 for most series; some series charge makers. Per-series rates come from
    config (and, from Phase 4, the series object; field names UNVERIFIED until then).
The rounding is applied to the whole order's fee, not per contract.
"""

from __future__ import annotations

from decimal import Decimal

from .money import ONE, ZERO, MoneyError, ceil_to_cent


def trade_fee(rate: Decimal, contracts: Decimal, price: Decimal) -> Decimal:
    if rate < ZERO:
        raise MoneyError("negative fee rate")
    if contracts < ZERO:
        raise MoneyError("negative contract count")
    if not (ZERO <= price <= ONE):
        raise MoneyError(f"price {price} outside [0, 1]")
    if contracts == ZERO or rate == ZERO:
        return ZERO.quantize(Decimal("0.01"))
    return ceil_to_cent(rate * contracts * price * (ONE - price))


def taker_fee(contracts: Decimal, price: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    return trade_fee(rate, contracts, price)


def maker_fee(contracts: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return trade_fee(rate, contracts, price)
