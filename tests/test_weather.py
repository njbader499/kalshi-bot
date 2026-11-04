"""Phase 3 weather engine: rules parsing, strike boundaries, climate-day window, DST guard,
CLI parsing, rounding, the probability model and the settlement cross-check.

Rules strings below are copied VERBATIM from the API (prod, 2026-11-04) as quoted in
DECISIONS.md, with only the date/strike varied where a test needs it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from kalshi_maker.weather.cli_reports import CliParseError, CliReport, parse_cli
from kalshi_maker.weather.contracts import (
    DST_RECHECK_DATE,
    RulesError,
    climate_day_window,
    dst_guard,
    dst_reminder,
    is_dst_day,
    parse_contract,
)
from kalshi_maker.weather.engine import WeatherEngine, member_extremes
from kalshi_maker.weather.model import (
    ModelParams,
    c_to_f,
    condition_at_least,
    condition_observed,
    integer_distribution,
    obs_round_f,
    prob_in,
    round_half_up,
)
from kalshi_maker.weather.settlement import check_settlements
from kalshi_maker.weather.sources import EnsembleData
from kalshi_maker.weather.stations import STATIONS

NY, MIA, CHI = STATIONS["CLINYC"], STATIONS["CLIMIA"], STATIONS["CLIMDW"]

GREATER_77 = (
    "If the maximum temperature recorded at New York City (CLINYC) for Nov 4, 2026, is greater "
    "than 77° fahrenheit according to The Weather Company, then the market resolves to Yes."
)
LESS_70 = (
    "If the maximum temperature recorded at New York City (CLINYC) for Nov 4, 2026, is less "
    "than 70° fahrenheit according to The Weather Company, then the market resolves to Yes."
)
BETWEEN_70_71 = (
    "If the maximum temperature recorded at New York City (CLINYC) for Nov 4, 2026, is between "
    "70-71° fahrenheit according to The Weather Company, then the market resolves to Yes."
)


def mk(ticker: str, rules: str, st: str, floor: Any, cap: Any, sub: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "rules_primary": rules,
        "strike_type": st,
        "floor_strike": floor,
        "cap_strike": cap,
        "yes_sub_title": sub,
    }


M_GREATER = mk("KXHIGHNY-26NOV04-T77", GREATER_77, "greater", 77, None, "78° or above")
M_LESS = mk("KXHIGHNY-26NOV04-T70", LESS_70, "less", None, 70, "69° or below")
M_BETWEEN = mk("KXHIGHNY-26NOV04-B70.5", BETWEEN_70_71, "between", 70, 71, "70° to 71°")


# ---------- strike semantics: the exact boundaries the owner specified ----------


def test_greater_than_77_means_78_or_above() -> None:
    c = parse_contract(M_GREATER)
    assert (c.lo, c.hi, c.label) == (78, None, ">=78")
    assert not c.yes(77)
    assert c.yes(78)
    assert c.yes(120)


def test_less_than_70_means_69_or_below() -> None:
    c = parse_contract(M_LESS)
    assert (c.lo, c.hi, c.label) == (None, 69, "<=69")
    assert c.yes(69)
    assert not c.yes(70)
    assert c.yes(-20)


def test_between_70_71_is_inclusive() -> None:
    c = parse_contract(M_BETWEEN)
    assert (c.lo, c.hi) == (70, 71)
    assert [c.yes(v) for v in (69, 70, 71, 72)] == [False, True, True, False]


def test_event_brackets_partition_the_integers() -> None:
    """The NY 2026-11-05 event as listed live: every integer in exactly one bracket."""

    def r(kind: str, a: int, b: int | None = None) -> str:
        x = {"g": f"greater than {a}", "l": f"less than {a}", "b": f"between {a}-{b}"}[kind]
        return (
            "If the maximum temperature recorded at New York City (CLINYC) for Nov 5, 2026, is "
            f"{x}° fahrenheit according to The Weather Company, then the market resolves to Yes."
        )

    ms = [
        mk("KXHIGHNY-26NOV05-T70", r("l", 70), "less", None, 70, "69° or below"),
        mk("KXHIGHNY-26NOV05-B70.5", r("b", 70, 71), "between", 70, 71, "70° to 71°"),
        mk("KXHIGHNY-26NOV05-B72.5", r("b", 72, 73), "between", 72, 73, "72° to 73°"),
        mk("KXHIGHNY-26NOV05-B74.5", r("b", 74, 75), "between", 74, 75, "74° to 75°"),
        mk("KXHIGHNY-26NOV05-B76.5", r("b", 76, 77), "between", 76, 77, "76° to 77°"),
        mk("KXHIGHNY-26NOV05-T77", r("g", 77), "greater", 77, None, "78° or above"),
    ]
    cs = [parse_contract(m) for m in ms]
    for v in range(40, 110):
        assert sum(c.yes(v) for c in cs) == 1, v


def test_parses_station_kind_and_day() -> None:
    c = parse_contract(M_GREATER)
    assert c.station is NY and c.kind == "max" and c.day == date(2026, 11, 4)
    mia = parse_contract(
        mk(
            "KXHIGHMIA-26NOV04-T88",
            GREATER_77.replace("New York City (CLINYC)", "Miami (CLIMIA)").replace("77", "88"),
            "greater",
            88,
            None,
            "89° or above",
        )
    )
    assert mia.station is MIA
    chi = parse_contract(
        mk(
            "KXHIGHCHI-26NOV04-T70",
            GREATER_77.replace("New York City (CLINYC)", "Chicago (CLIMDW)").replace("77", "70"),
            "greater",
            70,
            None,
            "71° or above",
        )
    )
    assert chi.station is CHI and chi.station.obs_station == "KMDW"  # Midway, not O'Hare


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"rules_primary": GREATER_77.replace("The Weather Company", "AccuWeather")}, "template"),
        ({"rules_primary": GREATER_77.replace("77°", "77.5°")}, "template"),
        ({"rules_primary": GREATER_77.replace("CLINYC", "CLIXYZ")}, "unknown station"),
        ({"rules_primary": GREATER_77.replace("CLINYC", "CLIMIA")}, "expected CLINYC"),
        ({"floor_strike": 76}, "floor_strike disagrees"),
        ({"floor_strike": 77.5}, "not a whole number"),
        ({"yes_sub_title": "77° or above"}, "yes_sub_title"),
        ({"strike_type": "functional"}, "unsupported strike_type"),
        ({"rules_primary": GREATER_77 + " Extra clause."}, "template"),
    ],
)
def test_unverified_wording_is_refused(change: dict[str, Any], needle: str) -> None:
    with pytest.raises(RulesError, match=needle):
        parse_contract({**M_GREATER, **change})


def test_float_strikes_from_json_accepted_when_whole() -> None:
    c = parse_contract({**M_BETWEEN, "floor_strike": 70.0, "cap_strike": 71.0})
    assert (c.lo, c.hi) == (70, 71)


# ---------- climate day window (NWS LST day) ----------


def test_window_is_midnight_to_midnight_local_standard_time() -> None:
    s, e = climate_day_window(date(2026, 11, 5), NY)
    assert (s, e) == (datetime(2026, 11, 5, 5, tzinfo=UTC), datetime(2026, 11, 6, 5, tzinfo=UTC))
    s, e = climate_day_window(date(2026, 11, 5), CHI)
    assert s == datetime(2026, 11, 5, 6, tzinfo=UTC) and e - s == timedelta(days=1)


def test_window_during_dst_is_1am_to_1259am_daylight_time() -> None:
    # A DST date: the LST day still uses the fixed standard offset, i.e. 01:00 EDT start.
    s, e = climate_day_window(date(2027, 7, 1), NY)
    assert s == datetime(2027, 7, 1, 5, tzinfo=UTC)  # 01:00 EDT == 00:00 EST
    assert e == datetime(2027, 7, 2, 5, tzinfo=UTC)


def test_dst_detection() -> None:
    assert not is_dst_day(date(2026, 11, 5), NY)
    assert not is_dst_day(date(2027, 3, 13), CHI)
    assert is_dst_day(date(2027, 3, 14), NY)
    assert is_dst_day(date(2027, 7, 1), CHI)


def test_dst_guard_refuses_dst_dates_until_rechecked() -> None:
    dst_guard(date(2026, 11, 5), NY, rechecked=False)  # standard time: fine
    dst_guard(date(2027, 3, 13), NY, rechecked=False)  # last standard-time day: fine
    for d in (DST_RECHECK_DATE, date(2027, 7, 1)):
        with pytest.raises(RulesError, match="recheck"):
            dst_guard(d, NY, rechecked=False)
        dst_guard(d, NY, rechecked=True)


def test_dst_reminder_is_dated() -> None:
    assert dst_reminder(date(2027, 2, 27)) is None
    assert "2027-03-14" in (dst_reminder(date(2027, 2, 28)) or "")
    assert dst_reminder(date(2027, 3, 14)) is None  # past it: the guard takes over


# ---------- CLI report parsing (formats seen live 2026-11-04) ----------

CLI_FINAL = """
000
CDUS41 KOKX 040618
CLINYC

CLIMATE REPORT
NATIONAL WEATHER SERVICE NEW YORK, NY
118 AM EST WED NOV 04 2026

...THE CENTRAL PARK NY CLIMATE SUMMARY FOR NOVEMBER 3 2026...

CLIMATE NORMAL PERIOD 1991 TO 2020

WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         68    336 PM  79    2003  58     10       59
  MINIMUM         52    559 AM  28    1875  46      6       50
  AVERAGE         60                        52      8       55

PRECIPITATION (IN)
  YESTERDAY        0.00          2.60 1910   0.12  -0.12     0.04
"""


def test_parse_final_cli() -> None:
    r = parse_cli(CLI_FINAL)
    assert r == CliReport("CLINYC", date(2026, 11, 3), 68, 52, True)


def test_parse_partial_cli_is_not_final() -> None:
    txt = CLI_FINAL.replace(
        "FOR NOVEMBER 3 2026...", "FOR NOVEMBER 3 2026...\nVALID TODAY AS OF 0400 PM LOCAL TIME."
    ).replace(" YESTERDAY\n  MAXIMUM", " TODAY\n  MAXIMUM")
    r = parse_cli(txt)
    assert not r.final and r.max_f == 68


def test_parse_missing_and_garbage() -> None:
    r = parse_cli(CLI_FINAL.replace("MAXIMUM         68", "MAXIMUM         MM"))
    assert r.max_f is None and r.min_f == 52
    with pytest.raises(CliParseError):
        parse_cli("nothing here")


# ---------- rounding & observations ----------


@pytest.mark.parametrize("x,want", [(70.49, 70), (70.5, 71), (70.51, 71), (-0.5, 0), (-0.51, -1)])
def test_round_half_up(x: float, want: int) -> None:
    assert round_half_up(x) == want


def test_obs_rounding() -> None:
    # 21.7 °C (live KNYC reading) = 71.06 °F -> the station most likely reports 71
    assert c_to_f(21.7) == pytest.approx(71.06)
    assert obs_round_f(21.7) == 71
    assert obs_round_f(0.0) == 32
    assert obs_round_f(25.6) == 78  # 78.08


def test_condition_observed_slack() -> None:
    d = integer_distribution([70.0] * 20, None, ModelParams())
    c = condition_observed(d, 78, "max", 0.05)
    assert c[77] == pytest.approx(0.05)  # one-degree conversion slack
    assert prob_in(c, 78, None) == pytest.approx(0.95)
    assert prob_in(c, None, 76) == 0
    m = condition_observed(d, 60, "min", 0.05)
    assert m[61] == pytest.approx(0.05) and prob_in(m, None, 60) == pytest.approx(0.95)


# ---------- model ----------


def test_distribution_is_a_probability_distribution() -> None:
    d = integer_distribution([70.2, 71.0, 72.4] * 10, 71.0, ModelParams())
    assert sum(d.values()) == pytest.approx(1.0)
    assert all(v >= 0 for v in d.values())
    assert max(d, key=d.__getitem__) in (70, 71, 72)


def test_reported_integer_rounding_in_model() -> None:
    p = ModelParams(sigma_f=0.3, nws_weight=0.0)
    # P(X=k) = P(k-0.5 <= T < k+0.5): latent 70.9 -> 71, latent 70.1 -> 70
    assert integer_distribution([70.9], None, p)[71] > 0.85
    assert integer_distribution([70.1], None, p)[70] > 0.85
    # exactly on the half: an even split between 70 and 71
    d = integer_distribution([70.5], None, p)
    assert d[70] == pytest.approx(0.5, abs=0.01) and d[71] == pytest.approx(0.5, abs=0.01)


def test_conditioning_on_observed_max() -> None:
    d = integer_distribution([70.0] * 20, None, ModelParams())
    c = condition_at_least(d, 72)
    assert min(c) == 72 and sum(c.values()) == pytest.approx(1.0)
    assert condition_at_least({70: 1.0}, 75) == {75: 1.0}  # observation beats the forecast


def test_prob_in_matches_contract_sets() -> None:
    d = {69: 0.1, 70: 0.2, 71: 0.3, 77: 0.15, 78: 0.25}
    assert prob_in(d, 78, None) == pytest.approx(0.25)  # greater than 77
    assert prob_in(d, None, 69) == pytest.approx(0.1)  # less than 70
    assert prob_in(d, 70, 71) == pytest.approx(0.5)  # between 70-71


@pytest.mark.parametrize(
    "kw", [{"sigma_f": 0.0}, {"sigma_f": 50}, {"nws_weight": 1.5}, {"bias_f": 9}]
)
def test_model_params_bounds(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ModelParams(**kw)


def test_member_extremes_use_lst_window_only() -> None:
    start = datetime(2026, 11, 5, 5, tzinfo=UTC)
    times = [start - timedelta(hours=3) + timedelta(hours=i) for i in range(30)]
    vals = [99.0] * 3 + [60.0 + (i % 24) * 0.1 for i in range(24)] + [99.0] * 3
    ens = EnsembleData(times, {"m1": list(vals), "m2": [None] * 30}, datetime.now(UTC))
    ext = member_extremes(ens, start, start + timedelta(days=1), "max")
    assert ext == [pytest.approx(62.3)]  # 99s outside the window ignored; all-None member dropped


# ---------- engine with fake sources ----------


class FakeSources:
    def __init__(self, reports: list[CliReport] | None = None, obs_c: list[float] | None = None):
        self.reports = reports or []
        self.obs_c = obs_c or []

    async def cli_reports(self, st: Any) -> list[CliReport]:
        return self.reports

    async def ensemble(self, st: Any) -> EnsembleData:
        start = datetime(2026, 11, 4, 5, tzinfo=UTC)
        times = [start + timedelta(hours=i) for i in range(48)]
        members = {f"m{i}": [69.0 + (i % 5) * 0.5] * 48 for i in range(50)}
        return EnsembleData(times, members, datetime.now(UTC))

    async def nws_hourly(self, st: Any) -> list[tuple[datetime, float]]:
        start = datetime(2026, 11, 4, 5, tzinfo=UTC)
        return [(start + timedelta(hours=i), 70.0) for i in range(48)]

    async def observations(self, st: Any, a: datetime, b: datetime) -> list[tuple[datetime, float]]:
        return [(a + timedelta(hours=i), c) for i, c in enumerate(self.obs_c)]


def _engine(src: FakeSources, now: datetime) -> WeatherEngine:
    return WeatherEngine(src, ModelParams(), now=lambda: now)  # type: ignore[arg-type]


async def test_final_cli_pins_the_outcome() -> None:
    src = FakeSources(reports=[CliReport("CLINYC", date(2026, 11, 4), 72, 60, True)])
    fv, err = await _engine(src, datetime(2026, 11, 5, 12, tzinfo=UTC)).price(
        [parse_contract(M_GREATER), parse_contract(M_LESS), parse_contract(M_BETWEEN)]
    )
    assert not err
    assert [fv[t].p for t in (M_GREATER["ticker"], M_LESS["ticker"], M_BETWEEN["ticker"])] == [
        D("0.0000"), D("0.0000"), D("0.0000"),
    ]  # fmt: skip
    assert all(f.confidence == 1.0 and f.source == "cli_final" for f in fv.values())


async def test_observed_max_kills_lower_brackets() -> None:
    # 25.6 °C = 78.08 °F -> reported 78 most likely (77 with 5% conversion slack)
    src = FakeSources(obs_c=[20.0, 25.6])
    fv, _ = await _engine(src, datetime(2026, 11, 4, 20, tzinfo=UTC)).price(
        [parse_contract(M_LESS), parse_contract(M_BETWEEN), parse_contract(M_GREATER)]
    )
    assert fv[M_LESS["ticker"]].p == 0
    assert fv[M_BETWEEN["ticker"]].p == 0
    assert fv[M_GREATER["ticker"]].p == D("0.9500")  # greater than 77 = 78+: near certain
    assert fv[M_GREATER["ticker"]].detail["bound"] == 78


async def test_intraday_confidence_halved_without_official_cli() -> None:
    now = datetime(2026, 11, 4, 20, tzinfo=UTC)
    fv, _ = await _engine(FakeSources(obs_c=[21.0]), now).price([parse_contract(M_GREATER)])
    f = fv[M_GREATER["ticker"]]
    assert f.confidence <= 0.5 and f.detail["intraday_unofficial"] is True
    partial = CliReport("CLINYC", date(2026, 11, 4), 71, 60, False)
    fv2, _ = await _engine(FakeSources(reports=[partial], obs_c=[21.0]), now).price(
        [parse_contract(M_GREATER)]
    )
    assert fv2[M_GREATER["ticker"]].confidence > 0.5  # official intraday value restores it


async def test_intraday_cli_is_a_hard_bound() -> None:
    partial = CliReport("CLINYC", date(2026, 11, 4), 78, 60, False)
    src = FakeSources(reports=[partial], obs_c=[25.0])  # METAR says 77, CLI says 78
    fv, _ = await _engine(src, datetime(2026, 11, 4, 22, tzinfo=UTC)).price(
        [parse_contract(M_GREATER)]
    )
    assert fv[M_GREATER["ticker"]].p == D("1.0000")
    assert fv[M_GREATER["ticker"]].detail["cli_partial"] == 78


async def test_fair_values_sum_to_one_across_event() -> None:
    fv, _ = await _engine(FakeSources(), datetime(2026, 11, 4, 3, tzinfo=UTC)).price(
        [parse_contract(M_LESS), parse_contract(M_BETWEEN), parse_contract(M_GREATER)]
    )
    tot = sum(f.p for f in fv.values())
    # brackets 72-77 aren't in this list, so the sum is <= 1
    assert D("0") < tot <= D("1.0003")


async def test_engine_refuses_dst_dates() -> None:
    dst_rules = GREATER_77.replace("Nov 4, 2026", "Jul 1, 2027")
    m = {**M_GREATER, "ticker": "KXHIGHNY-27JUL01-T77", "rules_primary": dst_rules}
    fv, err = await _engine(FakeSources(), datetime(2027, 6, 30, tzinfo=UTC)).price(
        [parse_contract(m)]
    )
    assert not fv and "recheck" in err["KXHIGHNY-27JUL01-T77"]


# ---------- settlement cross-check (auto-flagging) ----------


class FakeMarketsAPI:
    def __init__(self, markets: list[dict[str, Any]]) -> None:
        self.markets = markets
        self.params: dict[str, Any] = {}

    async def _gen(self, xs: list[dict[str, Any]]):
        for x in xs:
            yield x

    def iter_markets(self, **kw: Any):
        self.params = kw
        return self._gen(self.markets if kw.get("series_ticker") == "KXHIGHNY" else [])


async def test_settlement_check_agrees_and_flags() -> None:
    cli = FakeSources(reports=[CliReport("CLINYC", date(2026, 11, 4), 71, 55, True)])
    ok_between = {**M_BETWEEN, "result": "yes", "expiration_value": "71.00"}
    ok_less = {**M_LESS, "result": "no", "expiration_value": "71.00"}
    bad_value = {**M_GREATER, "result": "no", "expiration_value": "72.00"}
    bad_result = {**M_GREATER, "ticker": "KXHIGHNY-26NOV04-T77b", "result": "yes",
                  "expiration_value": "71.00"}  # fmt: skip
    frac = {**M_LESS, "ticker": "KXHIGHNY-26NOV04-T70b", "result": "no", "expiration_value": "71.5"}
    no_value = {**M_LESS, "ticker": "KXHIGHNY-26NOV04-T70c", "result": "no", "expiration_value": ""}
    api = FakeMarketsAPI([ok_between, ok_less, bad_value, bad_result, frac, no_value])
    res = await check_settlements(api, cli, ["KXHIGHNY"], now_s=1_800_000_000)  # type: ignore[arg-type]
    assert (
        api.params["status"] == "settled"
        and api.params["min_settled_ts"] == 1_800_000_000 - 8 * 86400
    )
    assert (res.checked, res.agreed, res.skipped_no_value) == (5, 2, 1)
    joined = "\n".join(res.mismatches)
    assert "Kalshi value 72.00 != CLI 71" in joined
    assert "result yes but CLI 71 implies no" in joined
    assert "71.5 is not a whole number" in joined


async def test_settlement_check_skips_when_cli_aged_out() -> None:
    api = FakeMarketsAPI([{**M_BETWEEN, "result": "yes", "expiration_value": "71.00"}])
    res = await check_settlements(api, FakeSources(), ["KXHIGHNY"])  # type: ignore[arg-type]
    assert (res.checked, res.skipped_no_cli, res.mismatches) == (0, 1, [])


async def test_cli_listing_survives_unfetchable_product() -> None:
    """A product listed in the index but 404 on fetch is skipped and retried later."""
    import httpx

    from kalshi_maker.weather.sources import WeatherSources

    fail = {"on": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/products/types/CLI/locations/NYC":
            return httpx.Response(
                200,
                json={
                    "@graph": [
                        {"@id": "https://api.weather.gov/products/new",
                         "issuanceTime": "2026-11-04T21:37:00+00:00"},
                        {"@id": "https://api.weather.gov/products/old",
                         "issuanceTime": "2026-11-04T06:18:00+00:00"},
                    ]
                },
            )  # fmt: skip
        if req.url.path == "/products/new" and fail["on"]:
            return httpx.Response(404, json={"title": "Not Found"})
        return httpx.Response(200, json={"productText": CLI_FINAL})

    clock = [0.0]
    src = WeatherSources(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=lambda: clock[0]
    )
    reps = await src.cli_reports(NY)
    assert [r.max_f for r in reps] == [68]  # the fetchable one still arrives
    fail["on"] = False
    clock[0] += 3600  # listing cache expires
    reps = await src.cli_reports(NY)
    assert len(reps) == 2  # the skipped product was retried, not cached as bad
    await src.aclose()
