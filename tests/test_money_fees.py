from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kalshi_maker.fees import (
    fill_fee_upper_bound,
    maker_fee,
    maker_rate,
    taker_fee,
    trade_fee,
)
from kalshi_maker.money import MoneyError, TickGrid, to_decimal

PENNY = {"price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]}
# Sub-cent ("tapered") structure: 0.1¢ in the tails, 1¢ in the middle.
TAPERED = {
    "price_ranges": [
        {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
        {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
        {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
    ]
}
HALF = {"price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0050"}]}


def test_to_decimal_rejects_float() -> None:
    assert to_decimal("0.9600") == D("0.96")
    assert to_decimal("278.00") == D(278)
    for bad in (0.96, True, "abc", "NaN", "Infinity", None):
        with pytest.raises(MoneyError):
            to_decimal(bad)


def test_half_cent_ticks() -> None:
    g = TickGrid.from_market(HALF)
    assert g.round_down(D("0.4249")) == D("0.420")
    assert g.round_down(D("0.4250")) == D("0.425")
    assert g.round_up(D("0.4251")) == D("0.430")
    assert g.round_up(D("0.4250")) == D("0.425")
    assert g.is_valid(D("0.995")) and not g.is_valid(D("0.997"))
    assert g.min_price == D("0.005") and g.max_price == D("0.995")


def test_penny_bounds() -> None:
    g = TickGrid.from_market(PENNY)
    assert g.round_down(D("0.005")) is None  # below the 1¢ floor: can't bid
    assert g.round_down(D("1.5")) == D("0.99")
    assert g.round_up(D("0.999")) is None  # above 99¢: can't ask
    assert g.round_up(D("-1")) == D("0.01")


def test_tapered_ticks() -> None:
    g = TickGrid.from_market(TAPERED)
    assert g.round_down(D("0.0567")) == D("0.056")
    assert g.round_down(D("0.5567")) == D("0.55")
    assert g.round_up(D("0.5501")) == D("0.56")
    assert g.round_up(D("0.9011")) == D("0.902")
    assert g.round_down(D("0.1005")) == D("0.10")
    assert g.tick_at(D("0.05")) == D("0.001") and g.tick_at(D("0.5")) == D("0.01")


def test_missing_price_ranges_refused() -> None:
    with pytest.raises(MoneyError):
        TickGrid.from_market({"ticker": "X"})
    with pytest.raises(MoneyError):
        TickGrid.from_market({"price_ranges": [{"start": "0", "end": "1", "step": "0.03"}]})


prices = st.decimals(min_value=D("-0.5"), max_value=D("1.5"), places=6)


@given(prices, st.sampled_from([PENNY, TAPERED, HALF]))
def test_rounding_properties(p: D, mkt: dict) -> None:
    g = TickGrid.from_market(mkt)
    lo, hi = g.round_down(p), g.round_up(p)
    if lo is not None:
        assert g.is_valid(lo) and lo <= max(p, g.min_price)
        assert lo <= p or p > g.max_price or lo == g.min_price
    if hi is not None:
        assert g.is_valid(hi) and hi >= min(p, g.max_price)
    if g.min_price <= p <= g.max_price:
        assert lo is not None and hi is not None and lo <= p <= hi
        if g.is_valid(p):
            assert lo == hi == p


@pytest.mark.parametrize(
    "n,p,fee",
    [
        ("1", "0.50", "0.017500"),  # exact
        ("100", "0.50", "1.750000"),
        ("1", "0.055", "0.003639"),  # docs worked example: 0.00363825 -> ceil 6dp
        ("1", "0.01", "0.000693"),
        ("3", "0.37", "0.048951"),
        ("1", "0.333", "0.015548"),  # 0.0155477... -> ceil
        ("0", "0.50", "0"),
        ("1", "0", "0"),
        ("1", "1", "0"),
        ("2.5", "0.40", "0.042000"),  # fractional contracts
    ],
)
def test_taker_fee(n: str, p: str, fee: str) -> None:
    assert taker_fee(D(n), D(p)) == D(fee)


def test_docs_rounding_example_fcm_cent_precision() -> None:
    # docs fee_rounding worked example: buy, revenue -0.055, model fee 0.00363825, 1c precision
    total = fill_fee_upper_bound(D("0.07"), D(1), D("0.055"), buy=True, balance_precision=D("0.01"))
    assert total == D("0.003639") + D("0.001361")


def test_direct_member_rounding_is_small() -> None:
    f = fill_fee_upper_bound(D("0.07"), D(1), D("0.055"))
    assert D("0.003639") <= f < D("0.003639") + D("0.0001")


def test_maker_rate_by_fee_type() -> None:
    t = D("0.07")
    assert maker_rate("quadratic", D(1), t) == 0
    assert maker_rate("quadratic_with_maker_fees", D(1), t) == D("0.0175")
    assert maker_rate("quadratic_with_combo_maker_fees", D(2), t) == D("0.07")
    with pytest.raises(MoneyError):
        maker_rate("flat", D(1), t)
    with pytest.raises(MoneyError):
        maker_rate(None, D(1), t)


def test_maker_fee_default_zero_and_nonzero() -> None:
    assert maker_fee(D(100), D("0.5"), D(0)) == 0
    assert maker_fee(D(100), D("0.5"), D("0.0175")) == D("0.4375")


def test_fee_guards() -> None:
    for args in ((D(-1), D(1), D("0.5")), (D("0.07"), D(-1), D("0.5")), (D("0.07"), D(1), D(2))):
        with pytest.raises(MoneyError):
            trade_fee(*args)


@given(st.integers(0, 10000), st.decimals(min_value=0, max_value=1, places=4))
def test_fee_is_ceiling(n: int, p: D) -> None:
    exact = D("0.07") * n * p * (1 - p)
    f = taker_fee(D(n), p)
    assert f >= exact and f - exact < D("0.000001") and f == f.quantize(D("0.000001"))
