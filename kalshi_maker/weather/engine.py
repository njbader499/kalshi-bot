"""Weather fair-value engine (shadow mode in Phase 3: computes and logs, never trades).

Per (station, kind, climate day):
  1. A final CLI report for the day -> exact value, confidence 1.0 ("cli_final").
  2. Otherwise build the integer distribution from the ensemble members' extreme over the
     LST climate-day window plus the NWS hourly forecast, then condition on what's already
     known: the conservative bound from today's observations and any intraday CLI value.
Each contract's fair value is P(X in its YES set), returned as a Decimal rounded to 4 dp.

Confidence is a heuristic in [0, 1], used to decide whether to quote (Phase 4). Phase 3
just logs it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal

from .contracts import RulesError, WeatherContract, climate_day_window, dst_guard
from .model import (
    Dist,
    ModelParams,
    condition_at_least,
    condition_at_most,
    condition_observed,
    integer_distribution,
    obs_round_f,
    prob_in,
)
from .sources import EnsembleData, WeatherSources
from .stations import Station

log = logging.getLogger(__name__)
P4 = Decimal("0.0001")
MIN_MEMBER_HOURS = 20  # a member needs >= 20 of 24 window hours to count


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


def member_extremes(ens: EnsembleData, start: datetime, end: datetime, kind: str) -> list[float]:
    idx = [i for i, t in enumerate(ens.times) if start <= t < end]
    out: list[float] = []
    for vals in ens.members.values():
        xs = [v for v in (vals[i] for i in idx) if v is not None]
        if len(xs) >= MIN_MEMBER_HOURS:
            out.append(max(xs) if kind == "max" else min(xs))
    return out


class WeatherEngine:
    def __init__(
        self,
        sources: WeatherSources,
        params: ModelParams,
        dst_rule_rechecked: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sources = sources
        self.params = params
        self.dst_rule_rechecked = dst_rule_rechecked
        self._now = now

    async def day_model(self, st: Station, kind: str, day: date) -> DayModel:
        now = self._now()
        start, end = climate_day_window(day, st)
        detail: dict[str, object] = {"window_utc": f"{start:%m-%d %H:%M}..{end:%m-%d %H:%M}"}

        cli = [r for r in await self.sources.cli_reports(st) if r.day == day]
        final = [r for r in cli if r.final]
        val = (final[-1].max_f if kind == "max" else final[-1].min_f) if final else None
        if val is not None:
            detail["cli_final"] = val
            detail["mean"] = float(val)
            return DayModel({val: 1.0}, 1.0, "cli_final", detail)

        conf = 1.0
        members: list[float] = []
        try:
            ens = await self.sources.ensemble(st)
            members = member_extremes(ens, start, end, kind)
            age_h = (now - ens.fetched_at).total_seconds() / 3600
            if age_h > 6:
                conf *= 0.7
        except Exception as e:
            log.warning("ensemble unavailable", extra={"station": st.cli_code, "err": repr(e)})
        detail["members"] = len(members)
        if members:
            ms = sorted(members)
            detail["ens_p10_p50_p90"] = (
                round(ms[len(ms) // 10], 1),
                round(ms[len(ms) // 2], 1),
                round(ms[(9 * len(ms)) // 10], 1),
            )

        nws_val: float | None = None
        try:
            hourly = await self.sources.nws_hourly(st)
            xs = [t for ts, t in hourly if start <= ts < end and ts >= now.replace(minute=0)]
            if len(xs) >= 6 or (xs and end <= now.replace(minute=0)):
                nws_val = max(xs) if kind == "max" else min(xs)
        except Exception as e:
            log.warning("nws forecast unavailable", extra={"station": st.cli_code, "err": repr(e)})
        detail["nws"] = nws_val

        if not members and nws_val is None:
            raise RuntimeError(f"no forecast inputs for {st.cli_code} {day}")
        if len(members) < 40:
            conf *= 0.6 if members else 0.4
        if nws_val is None:
            conf *= 0.85
        dist = integer_distribution(members, nws_val, self.params)

        # What has already happened today. An intraday CLI value is official and exact (a
        # hard bound); a METAR-derived extreme is soft (one degree of conversion slack).
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
        if hard is not None:
            if kind == "max":
                dist = condition_at_least(dist, hard)
            else:
                dist = condition_at_most(dist, hard)
            detail["bound"] = hard
        if obs_r is not None:
            beyond = hard is None or (obs_r > hard if kind == "max" else obs_r < hard)
            if beyond:
                slack = self.params.obs_slack_p
                alt = obs_r - 1 if kind == "max" else obs_r + 1
                if hard is not None and (alt < hard if kind == "max" else alt > hard):
                    slack = 0.0  # the hard bound already rules the slack value out
                dist = condition_observed(dist, obs_r, kind, slack)
                detail["bound"] = obs_r
        if start <= now and hard is None:
            # Same-day pricing from hourly METARs alone: hourly readings miss peaks between
            # observations (seen live 2026-11-04: obs 71/84/70 vs settled-looking markets at
            # 72-73/85-86/69-70). Until an official intraday CLI arrives, don't trust it enough
            # to quote (the halved confidence falls below weather.min_confidence = 0.6).
            conf *= 0.5
            detail["intraday_unofficial"] = True
        days_ahead = (day - now.date()).days
        if days_ahead >= 2:
            conf *= 0.8
        detail["mode"] = max(dist, key=lambda k: dist[k])
        detail["mean"] = round(sum(k * v for k, v in dist.items()), 3)
        return DayModel(dist, round(conf, 3), "ensemble+nws+obs", detail)

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
                p = prob_in(dm.dist, c.lo, c.hi)
                pd = Decimal(repr(min(1.0, max(0.0, p)))).quantize(P4, ROUND_HALF_EVEN)
                out[c.ticker] = FairValue(c.ticker, pd, dm.confidence, dm.source, as_of, dm.detail)
        return out, errors
