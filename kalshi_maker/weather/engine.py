"""Weather fair-value engine, model v2 (see model.py for the maths and why v1 was wrong).

Per (station, kind, climate day):
  1. A final CLI report for the day -> exact value, confidence 1.0 ("cli_final").
  2. Otherwise X = max(B, Y) (min for a daily min):
       B = the reported extreme of the hours already observed (hourly obs + offset table,
           floored by an intraday CLI value when one exists);
       Y = the forecast extreme over the hours STILL TO COME, from the ensemble members'
           remaining-window extremes and the NWS forecast (hourly remaining max, lifted by
           the NWS daily high before local noon).
     Before the day starts, B is empty and Y covers the whole day.
Each contract's fair value is P(X in its YES set), returned as a Decimal rounded to 4 dp.

Confidence is a heuristic in [0, 1] used to decide whether to quote (trading.py). The
daytime ramp (default 10:00-16:00 local standard time) without an official intraday CLI is
the dangerous window: hourly observations lag the real temperature while it is rising, so
confidence is halved there. Per-station model parameters come from config
(weather.stations) with the global values as the default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from .contracts import RulesError, WeatherContract, climate_day_window, dst_guard
from .model import (
    Dist,
    ModelParams,
    bound_distribution,
    combine_with_bound,
    integer_distribution,
    mode_of,
    obs_round_f,
    prob_in,
)
from .sources import EnsembleData, WeatherSources
from .stations import Station

log = logging.getLogger(__name__)
P4 = Decimal("0.0001")
MIN_MEMBER_HOURS = 20  # a full-day member needs >= 20 of 24 window hours to count
MIN_REMAINING_FRACTION = 0.75  # a remaining-window member needs >= 75% of its hours


@dataclass(frozen=True, slots=True)
class FairValue:
    ticker: str
    p: Decimal
    confidence: float
    source: str
    as_of: datetime
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class DayModel:
    dist: Dist
    confidence: float
    source: str
    detail: dict[str, object]


def member_extremes_by_key(
    ens: EnsembleData, start: datetime, end: datetime, kind: str, min_hours: int | None = None
) -> dict[str, float]:
    """{member series name: extreme over [start, end)}. Members with too few hours are
    dropped. The name carries the model (gfs/ecmwf), which selects the member's bias."""
    idx = [i for i, t in enumerate(ens.times) if start <= t < end]
    if not idx:
        return {}
    need = MIN_MEMBER_HOURS if min_hours is None else min_hours
    out: dict[str, float] = {}
    for key, vals in ens.members.items():
        xs = [v for v in (vals[i] for i in idx) if v is not None]
        if xs and len(xs) >= need:
            out[key] = max(xs) if kind == "max" else min(xs)
    return out


def member_extremes(
    ens: EnsembleData, start: datetime, end: datetime, kind: str, min_hours: int | None = None
) -> list[float]:
    """Each member's extreme over [start, end) (values only)."""
    return list(member_extremes_by_key(ens, start, end, kind, min_hours).values())


def _quantiles(ms: list[float]) -> tuple[float, float, float]:
    s = sorted(ms)
    n = len(s)
    return (round(s[n // 10], 1), round(s[n // 2], 1), round(s[(9 * n) // 10], 1))


class WeatherEngine:
    def __init__(
        self,
        sources: WeatherSources,
        params: ModelParams,
        dst_rule_rechecked: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        station_params: Mapping[str, ModelParams] | None = None,
        ramp_hours: tuple[int, int] = (10, 16),
    ) -> None:
        self.sources = sources
        self.params = params
        self.station_params = dict(station_params or {})
        self.dst_rule_rechecked = dst_rule_rechecked
        self._now = now
        if not (0 <= ramp_hours[0] < ramp_hours[1] <= 24):
            raise ValueError(f"bad ramp_hours {ramp_hours}")
        self.ramp_hours = ramp_hours

    def params_for(self, st: Station) -> ModelParams:
        return self.station_params.get(st.cli_code, self.params)

    async def day_model(self, st: Station, kind: str, day: date) -> DayModel:
        now = self._now()
        p = self.params_for(st)
        start, end = climate_day_window(day, st)
        detail: dict[str, object] = {
            "model": "v2",
            "window_utc": f"{start:%m-%d %H:%M}..{end:%m-%d %H:%M}",
            "params": {
                "bias_f": p.bias_f, "bias_by_model": dict(p.bias_by_model),
                "sigma_f": p.sigma_f, "nws_w": p.nws_weight, "nws_bias_f": p.nws_bias_f,
            },
        }

        cli = [r for r in await self.sources.cli_reports(st) if r.day == day]
        final = [r for r in cli if r.final]
        val = (final[-1].max_f if kind == "max" else final[-1].min_f) if final else None
        if val is not None:
            detail["cli_final"] = val
            return DayModel({val: 1.0}, 1.0, "cli_final", detail)

        # ---- Y: the forecast extreme over the hours still to come ----
        rem_start = max(start, now.replace(minute=0, second=0, microsecond=0))
        remaining_hours = max(0, int((end - rem_start).total_seconds() // 3600))
        detail["remaining_hours"] = remaining_hours
        conf = 1.0
        members: list[float] = []
        biases: list[float] = []
        remaining: Dist | None = None
        nws_val: float | None = None
        if remaining_hours > 0:
            try:
                ens = await self.sources.ensemble(st)
                need = max(1, int(remaining_hours * MIN_REMAINING_FRACTION))
                by_key = member_extremes_by_key(ens, rem_start, end, kind, min_hours=need)
                members = list(by_key.values())
                biases = [p.bias_for(k) for k in by_key]
                if (now - ens.fetched_at).total_seconds() > 6 * 3600:
                    conf *= 0.7
            except Exception as e:
                log.warning("ensemble unavailable", extra={"station": st.cli_code, "err": repr(e)})
            detail["members"] = len(members)
            if members:
                detail["ens_p10_p50_p90"] = _quantiles(members)
                corrected = sorted(m + b for m, b in zip(members, biases, strict=True))
                detail["ens_p50_corrected"] = round(corrected[len(members) // 2], 1)
            nws_val = await self._nws_remaining(st, kind, day, rem_start, end, now, detail)
            if members or nws_val is not None:
                if len(members) < 40:
                    conf *= 0.6 if members else 0.4
                if nws_val is None:
                    conf *= 0.85
                remaining = integer_distribution(members, nws_val, p, member_biases=biases)
            else:
                conf *= 0.4  # nothing to forecast the rest of the day with
        detail["nws"] = nws_val

        # ---- B: what has already happened ----
        hard: int | None = None
        partial = [r for r in cli if not r.final]
        if partial:
            pv = partial[-1].max_f if kind == "max" else partial[-1].min_f
            if pv is not None:
                hard = pv
                detail["cli_partial"] = pv
        obs_r: int | None = None
        if start <= now:
            obs = await self.sources.observations(st, start, min(end, now))
            if obs:
                ext_c = max(c for _, c in obs) if kind == "max" else min(c for _, c in obs)
                obs_r = obs_round_f(ext_c)
                detail["obs_n"] = len(obs)
                detail["obs_ext_c"] = ext_c
                detail["obs_f"] = obs_r
        bound = bound_distribution(obs_r, hard, kind, p.obs_offsets)
        if bound is not None:
            detail["bound"] = max(bound, key=lambda k: bound[k])
            detail["bound_dist"] = {str(k): round(v, 3) for k, v in sorted(bound.items())}

        if remaining is None and bound is None:
            raise RuntimeError(f"no forecast inputs and no observations for {st.cli_code} {day}")
        dist = combine_with_bound(remaining, bound, kind)

        # ---- confidence by time of day ----
        if start <= now < end and hard is None:
            local_hour = now.astimezone(timezone(timedelta(hours=st.lst_offset_hours))).hour
            if self.ramp_hours[0] <= local_hour < self.ramp_hours[1]:
                # Daytime ramp with only hourly METARs: readings lag a rising temperature
                # (seen live 2026-11-04: obs 71/84/70 vs settled 72/85/69-70). Don't quote.
                conf *= 0.5
                detail["intraday_unofficial"] = True
            elif local_hour >= self.ramp_hours[1]:
                conf *= 0.9  # peak passed; only the official-vs-hourly offset is left
        if now >= end and hard is None:
            conf *= 0.9
        days_ahead = (day - now.date()).days
        if days_ahead >= 2:
            conf *= 0.8
        detail["mode"] = mode_of(dist)
        return DayModel(dist, round(conf, 3), "ensemble+nws+obs/v2", detail)

    async def _nws_remaining(
        self,
        st: Station,
        kind: str,
        day: date,
        rem_start: datetime,
        end: datetime,
        now: datetime,
        detail: dict[str, object],
    ) -> float | None:
        """NWS forecast extreme over the remaining window: hourly values, lifted by the
        daily high/low period before local noon (the hourly series samples the curve and
        misses the peak; the daily high is the forecaster's number for the peak itself)."""
        val: float | None = None
        try:
            hourly = await self.sources.nws_hourly(st)
            xs = [t for ts, t in hourly if rem_start <= ts < end]
            if len(xs) >= 6 or (xs and end <= now + timedelta(hours=6)):
                val = max(xs) if kind == "max" else min(xs)
        except Exception as e:
            log.warning("nws forecast unavailable", extra={"station": st.cli_code, "err": repr(e)})
        local_hour = now.astimezone(timezone(timedelta(hours=st.lst_offset_hours))).hour
        before_noon = rem_start > now or local_hour < 12
        if before_noon:
            try:
                daily = await self.sources.nws_daily(st)
                d = daily.get(day)
                if d is not None:
                    dv = d[0] if kind == "max" else d[1]
                    if dv is not None:
                        detail["nws_daily"] = dv
                        better = max if kind == "max" else min
                        val = dv if val is None else better(val, dv)
            except Exception as e:
                log.warning("nws daily unavailable", extra={"station": st.cli_code, "err": repr(e)})
        return val

    async def price(
        self, contracts: Iterable[WeatherContract]
    ) -> tuple[dict[str, FairValue], dict[str, str]]:
        """Returns ({ticker: FairValue}, {ticker: reason not priced})."""
        groups: dict[tuple[str, str, date], list[WeatherContract]] = {}
        errors: dict[str, str] = {}
        for c in contracts:
            try:
                dst_guard(c.day, c.station, self.dst_rule_rechecked)
            except RulesError as e:
                errors[c.ticker] = str(e)
                continue
            groups.setdefault((c.station.cli_code, c.kind, c.day), []).append(c)
        out: dict[str, FairValue] = {}
        for (_code, kind, day), cs in groups.items():
            st = cs[0].station
            try:
                dm = await self.day_model(st, kind, day)
            except Exception as e:
                for c in cs:
                    errors[c.ticker] = f"model failed: {e!r}"
                continue
            as_of = self._now()
            for c in cs:
                pr = prob_in(dm.dist, c.lo, c.hi)
                pd = Decimal(repr(min(1.0, max(0.0, pr)))).quantize(P4, ROUND_HALF_EVEN)
                out[c.ticker] = FairValue(c.ticker, pd, dm.confidence, dm.source, as_of, dm.detail)
        return out, errors
