"""Kalshi trading fee math.

Verified against docs.kalshi.com/getting_started/fee_rounding (read 2026-11-04), which
supersedes the spec's "ceil to the cent":

  model_fee   = rate * contracts * P * (1 - P)    (P = fill price; contracts may be fractional)
  trade_fee   = ceil to $0.000001 (6 dp)
  rounding    = whatever brings the balance change back onto the member's precision grid
                ($0.0001 for direct members, $0.01 for FCM-cleared members)
  rebate      = accumulated rounding overpayment, returned per order (never makes net < 0)
  net fee     = trade_fee + rounding - rebate

Rates: taker 0.07 (General Trading Fees Table). Makers pay 0 on series whose `fee_type` is
"quadratic". They pay maker fees only on "quadratic_with_maker_fees" (0.25 multiplier) and
"quadratic_with_combo_maker_fees" (0.5); see the Series schema. Multiply by the series'
`fee_multiplier`.

The ledger always records the exchange's actual per-fill `fee_cost`. These functions are for
pre-trade estimates and risk, so `fill_fee_upper_bound` ignores rebates (conservative).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .money import ONE, ZERO, MoneyError

MICRO = Decimal("0.000001")
DIRECT_BALANCE_PRECISION = Decimal("0.0001")

MAKER_FEE_FACTOR = {
    "quadratic": Decimal(0),
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
}


def _check(rate: Decimal, contracts: Decimal, price: Decimal) -> None:
    if rate < ZERO:
        raise MoneyError("negative fee rate")
    if contracts < ZERO:
        raise MoneyError("negative contract count")
    if not (ZERO <= price <= ONE):
        raise MoneyError(f"price {price} outside [0, 1]")


def model_fee(rate: Decimal, contracts: Decimal, price: Decimal) -> Decimal:
    _check(rate, contracts, price)
    return rate * contracts * price * (ONE - price)


def trade_fee(rate: Decimal, contracts: Decimal, price: Decimal) -> Decimal:
    """Model fee rounded UP to $0.000001."""
    return model_fee(rate, contracts, price).quantize(MICRO, rounding=ROUND_CEILING)


def fill_fee_upper_bound(
    rate: Decimal,
    contracts: Decimal,
    price: Decimal,
    buy: bool = True,
    balance_precision: Decimal = DIRECT_BALANCE_PRECISION,
) -> Decimal:
    """trade fee + rounding fee for one fill, ignoring rebates (so never an underestimate)."""
    tf = trade_fee(rate, contracts, price)
    revenue = contracts * price * (-1 if buy else 1)
    change = revenue - tf
    aligned = change.quantize(balance_precision, rounding=ROUND_FLOOR)
    return tf + (change - aligned)


def taker_fee(contracts: Decimal, price: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    return trade_fee(rate, contracts, price)


def maker_rate(fee_type: str | None, fee_multiplier: Decimal, taker_rate: Decimal) -> Decimal:
    """Maker rate for a series. Unknown fee types raise, so a new structure can't sneak in."""
    if fee_type is None or fee_type not in MAKER_FEE_FACTOR:
        raise MoneyError(f"unknown series fee_type {fee_type!r}; refusing to assume maker fee")
    return taker_rate * MAKER_FEE_FACTOR[fee_type] * fee_multiplier


def maker_fee(contracts: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return trade_fee(rate, contracts, price)
