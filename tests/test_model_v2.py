"""Weather model v2: X = max(what already happened, forecast of what is still to come).

The regression that motivated it (2026-11-04, NYC): at 23:00 local with the high already 72
the v1 engine gave 38% to 74+, the market 1%. The actual high was 72.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from kalshi_maker.weather.cli_reports import CliReport
from kalshi_maker.weather.contracts import parse_contract
from kalshi_maker.weather.engine import WeatherEngine
from kalshi_maker.weather.model import (
    DEFAULT_OBS_OFFSETS,
    ModelParams,
    bound_distribution,
    combine_with_bound,
    integer_distribution,
    prob_in,
)
from kalshi_maker.weather.sources import EnsembleData
from tests.test_weather import M_BETWEEN, M_GREATER, M_LESS, mk

# NYC 2026-11-04 brackets as listed live (see test_weather.py for the verbatim template)
_R = (
    "If the maximum temperature recorded at New York City (CLINYC) for Nov 4, 2026, is {x}° "
    "fahrenheit according to The Weather Company, then the market resolves to Yes."
)
B72 = mk("KXHIGHNY-26NOV04-B72.5", _R.format(x="between 72-73"), "between", 72, 73, "72° to 73°")
B74 = mk("KXHIGHNY-26NOV04-B74.5", _R.format(x="between 74-75"), "between", 74, 75, "74° to 75°")
B76 = mk("KXHIGHNY-26NOV04-B76.5", _R.format(x="between 76-77"), "between", 76, 77, "76° to 77°")
DAY_START = datetime(2026, 11, 4, 5, tzinfo=UTC)  # 00:00 EST


# ---------- pure model ----------


def test_combine_collapses_mass_onto_the_bound_instead_of_dropping_it() -> None:
    y = {70: 0.3, 71: 0.25, 72: 0.2, 73: 0.15, 74: 0.1}
    x = combine_with_bound(y, {72: 1.0}, "max")
    assert x[72] == pytest.approx(0.75)  # 0.3 + 0.25 + 0.2: nothing renormalised away
    assert x[73] == pytest.approx(0.15) and x[74] == pytest.approx(0.1)
    assert 71 not in x and 70 not in x
    # the v1 way (drop and renormalise) would have said P(74) = 0.1 / 0.45 = 0.22


def test_combine_mirrors_for_a_daily_min() -> None:
    y = {40: 0.5, 42: 0.5}
    x = combine_with_bound(y, {41: 1.0}, "min")
    assert x == {40: 0.5, 41: 0.5}


def test_combine_without_remaining_is_the_bound() -> None:
    assert combine_with_bound(None, {72: 0.6, 73: 0.4}, "max") == {72: 0.6, 73: 0.4}
    with pytest.raises(ValueError):
        combine_with_bound(None, None, "max")


def test_bound_distribution_uses_the_offset_table() -> None:
    b = bound_distribution(71, None, "max", DEFAULT_OBS_OFFSETS)
    assert b == {70: 0.05, 71: 0.45, 72: 0.40, 73: 0.10}
    assert bound_distribution(None, None, "max", DEFAULT_OBS_OFFSETS) is None


def test_official_intraday_value_is_exact_when_obs_have_not_passed_it() -> None:
    assert bound_distribution(71, 72, "max", DEFAULT_OBS_OFFSETS) == {72: 1.0}
    assert bound_distribution(None, 72, "max", DEFAULT_OBS_OFFSETS) == {72: 1.0}
    # obs went PAST an early-morning official value: offsets apply, floored at it
    b = bound_distribution(55, 38, "max", DEFAULT_OBS_OFFSETS)
    assert b == {54: 0.05, 55: 0.45, 56: 0.40, 57: 0.10}
    # min mirrors: official low 40, obs since then 42 -> official stays 40
    assert bound_distribution(42, 40, "min", DEFAULT_OBS_OFFSETS) == {40: 1.0}


def test_offsets_are_validated() -> None:
    with pytest.raises(ValueError):
        ModelParams(obs_offsets={0: 0.5, 1: 0.4})  # doesn't sum to 1
    with pytest.raises(ValueError):
        ModelParams(obs_offsets={5: 1.0})  # out of range


def test_reported_integer_rounding_with_zero_bias() -> None:
    p = ModelParams(sigma_f=0.3, nws_weight=0.0, bias_f=0.0)
    assert integer_distribution([70.9], None, p)[71] > 0.85
    assert integer_distribution([70.1], None, p)[70] > 0.85


# ---------- engine with fake sources ----------


class Sources:
    """Diurnal curve: 50F overnight, rising to a 72-74F peak at 14-15 local, falling after."""

    def __init__(
        self,
        peaks: list[float] | None = None,
        obs_c: list[tuple[int, float]] | None = None,  # (hour since day start, °C)
        reports: list[CliReport] | None = None,
        nws_daily_high: float | None = None,
    ) -> None:
        self.peaks = peaks or [72.0 + (i % 5) * 0.5 for i in range(50)]
        self.obs = obs_c or []
        self.reports = reports or []
        self.nws_daily_high = nws_daily_high

    @staticmethod
    def curve(peak: float, h: int) -> float:  # h = hours since 00:00 LST, for 48 hours
        h %= 24
        if h < 6:
            return 50.0
        if h <= 14:
            return 50.0 + (peak - 50.0) * (h - 6) / 8
        return peak - (peak - 50.0) * (h - 14) / 10

    async def cli_reports(self, st: Any) -> list[CliReport]:
        return self.reports

    async def ensemble(self, st: Any) -> EnsembleData:
        times = [DAY_START + timedelta(hours=i) for i in range(48)]
        members = {
            f"m{i}": [self.curve(pk, h) for h in range(48)] for i, pk in enumerate(self.peaks)
        }
        return EnsembleData(times, members, datetime.now(UTC))

    async def nws_hourly(self, st: Any) -> list[tuple[datetime, float]]:
        return [(DAY_START + timedelta(hours=h), self.curve(73.0, h)) for h in range(48)]

    async def nws_daily(self, st: Any) -> dict[date, tuple[float | None, float | None]]:
        if self.nws_daily_high is None:
            return {}
        return {date(2026, 11, 4): (self.nws_daily_high, 50.0)}

    async def observations(
        self, st: Any, a: datetime, b: datetime
    ) -> list[tuple[datetime, float]]:
        pts = [(DAY_START + timedelta(hours=h), c) for h, c in self.obs]
        return [(t, c) for t, c in pts if a <= t < b]


def eng(src: Sources, now: datetime, **kw: Any) -> WeatherEngine:
    params = kw.pop("params", ModelParams(sigma_f=1.0, nws_weight=0.3, bias_f=0.0))
    return WeatherEngine(src, params, now=lambda: now, **kw)  # type: ignore[arg-type]


async def price(e: WeatherEngine, ms: list[dict[str, Any]]) -> dict[str, Any]:
    fv, err = await e.price([parse_contract(m) for m in ms])
    assert not err, err
    return fv


async def test_evening_the_observed_high_is_the_answer() -> None:
    """23:00 local, hourly obs peaked at 22.2C (72F): 74+ is (nearly) impossible now."""
    obs = [(h, 10.0) for h in range(0, 10)] + [(14, 22.2), (15, 21.0), (20, 15.0), (22, 12.0)]
    now = DAY_START + timedelta(hours=23)
    fv = await price(eng(Sources(obs_c=obs), now), [B72, B74, B76, M_GREATER, M_LESS])
    assert fv[B72["ticker"]].p >= D("0.85")  # 72 or 73 (official may be a degree above hourly)
    assert fv[B74["ticker"]].p <= D("0.11")
    assert fv[B76["ticker"]].p == D("0.0000") and fv[M_GREATER["ticker"]].p == D("0.0000")
    assert fv[M_LESS["ticker"]].p == D("0.0000")
    d = fv[B72["ticker"]].detail
    assert d["model"] == "v2" and d["remaining_hours"] == 1 and d["obs_f"] == 72
    assert fv[B72["ticker"]].confidence >= 0.8  # peak passed: quotable


async def test_evening_with_official_intraday_value_is_near_certain() -> None:
    obs = [(14, 21.7), (18, 18.0)]  # hourly max 71F; the 4pm CLI already says 72
    rep = CliReport("CLINYC", date(2026, 11, 4), 72, 50, False)
    now = DAY_START + timedelta(hours=23, minutes=30)
    fv = await price(eng(Sources(obs_c=obs, reports=[rep]), now), [B72, B74])
    assert fv[B72["ticker"]].p >= D("0.99") and fv[B74["ticker"]].p <= D("0.01")
    assert fv[B72["ticker"]].confidence >= 0.85


async def test_after_midnight_without_final_cli_is_the_bound_exactly() -> None:
    obs = [(14, 22.2)]
    now = DAY_START + timedelta(hours=26)  # 02:00 next day, final CLI not out yet
    fv = await price(eng(Sources(obs_c=obs), now), [B72, B74])
    assert fv[B72["ticker"]].detail["remaining_hours"] == 0
    assert fv[B72["ticker"]].p == D("0.8500") and fv[B74["ticker"]].p == D("0.1000")


async def test_before_the_day_the_forecast_rules() -> None:
    now = DAY_START - timedelta(hours=8)
    fv = await price(eng(Sources(), now), [B72, B74, M_LESS])
    assert D("0.4") < fv[B72["ticker"]].p < D("0.95")  # peaks 72-74, sigma 1
    assert D("0.2") < fv[B74["ticker"]].p < fv[B72["ticker"]].p
    assert fv[B72["ticker"]].detail["remaining_hours"] == 24
    assert "bound" not in fv[B72["ticker"]].detail
    assert fv[B72["ticker"]].confidence >= 0.85


async def test_morning_obs_do_not_cap_the_day() -> None:
    """07:00 local with overnight obs at 50F: the afternoon forecast still decides."""
    obs = [(h, 10.0) for h in range(0, 7)]
    now = DAY_START + timedelta(hours=7)
    fv = await price(eng(Sources(obs_c=obs), now), [B72, M_LESS])
    assert fv[B72["ticker"]].p > D("0.4")
    assert fv[M_LESS["ticker"]].p < D("0.1")
    assert fv[B72["ticker"]].detail["bound"] == 50
    assert fv[B72["ticker"]].confidence >= 0.85  # before the ramp: it's a forecast


async def test_daytime_ramp_without_official_value_halves_confidence() -> None:
    obs = [(h, 10.0) for h in range(0, 12)]
    now = DAY_START + timedelta(hours=12)  # 12:00 local, inside [10, 16)
    fv = await price(eng(Sources(obs_c=obs), now), [B72])
    f = fv[B72["ticker"]]
    assert f.confidence <= 0.5 and f.detail["intraday_unofficial"] is True
    rep = CliReport("CLINYC", date(2026, 11, 4), 65, 50, False)
    fv2 = await price(eng(Sources(obs_c=obs, reports=[rep]), now), [B72])
    assert fv2[B72["ticker"]].confidence > 0.5
    # ramp hours are configurable
    fv3 = await price(eng(Sources(obs_c=obs), now, ramp_hours=(13, 16)), [B72])
    assert fv3[B72["ticker"]].confidence > 0.5


async def test_nws_daily_high_lifts_the_nws_component_before_noon() -> None:
    now = DAY_START - timedelta(hours=8)
    lo = await price(eng(Sources(nws_daily_high=None), now), [B72, B76])
    hi = await price(eng(Sources(nws_daily_high=77.0), now), [B72, B76])
    assert hi[B76["ticker"]].p > lo[B76["ticker"]].p
    assert hi[B76["ticker"]].detail["nws_daily"] == 77.0
    # ... but not in the afternoon, when only the remaining hourly values matter
    pm = DAY_START + timedelta(hours=17)
    obs = [(14, 22.2)]
    late = await price(eng(Sources(obs_c=obs, nws_daily_high=77.0), pm), [B72, B76])
    assert "nws_daily" not in late[B72["ticker"]].detail
    assert late[B76["ticker"]].p < D("0.05")


async def test_per_station_params_override_the_global_ones() -> None:
    now = DAY_START - timedelta(hours=8)
    warm = ModelParams(sigma_f=1.0, nws_weight=0.0, bias_f=3.0)
    fv = await price(eng(Sources(), now, station_params={"CLINYC": warm}), [B72, B76])
    assert fv[B76["ticker"]].p > fv[B72["ticker"]].p  # peaks 72-74 + 3 = 75-77
    assert fv[B76["ticker"]].detail["params"]["bias_f"] == 3.0


async def test_event_probabilities_still_sum_to_one() -> None:
    now = DAY_START + timedelta(hours=23)
    obs = [(14, 22.2)]
    ms = [M_LESS, mk("KXHIGHNY-26NOV04-B70.5", _R.format(x="between 70-71"), "between", 70, 71,
                     "70° to 71°"), B72, B74, B76, M_GREATER]  # fmt: skip
    fv = await price(eng(Sources(obs_c=obs), now), ms)
    assert abs(sum(f.p for f in fv.values()) - 1) <= D("0.0003")
    assert sum(1 for f in fv.values() if f.p > 0) >= 2


def test_prob_in_handles_open_ends() -> None:
    d = {70: 0.2, 72: 0.5, 75: 0.3}
    assert prob_in(d, None, 71) == pytest.approx(0.2)
    assert prob_in(d, 72, None) == pytest.approx(0.8)
    assert prob_in(d, 72, 73) == pytest.approx(0.5)


def test_between_contract_still_parses_the_verified_template() -> None:
    c = parse_contract(M_BETWEEN)
    assert (c.lo, c.hi) == (70, 71)
