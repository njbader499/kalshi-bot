"""Parameter fitting for the weather model (pure functions; the network/CLI parts live in
scripts/calibrate_weather.py so they can be run on the laptop, which can reach the APIs).

Two things are fitted:

1. Per-station forecast bias and spread. For each past day, the forecast daily extreme
   (Open-Meteo historical forecast, same hourly-sampled quantity the bot uses, from the
   deterministic runs of the SAME models whose ensembles the bot uses) is compared with
   the official value (NCEI GHCN-Daily TMAX/TMIN = the NWS climate report). The mean
   residual per model is that model's member bias; the member-weighted combination is
   `bias_f`; the residual spread, less the part the live ensemble already expresses, is
   `sigma_f`. The NWS forecast is NOT calibrated this way (it is forecaster-adjusted).
2. The observation offset table: P(official extreme - hourly-METAR extreme = d), from the
   bot's own shadow log (obs_f at the last snapshot before the final CLI vs cli_final).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# Residual sd that a day-1 global ensemble typically expresses on its own at these stations
# (p10-p90 spread of 2-3F seen live 2026-11-04/05 -> sd about 1.0F). sigma_f is the extra
# spread the model must add so total spread matches the observed residuals.
ENSEMBLE_OWN_SD_F = 1.0
MIN_SIGMA_F = 0.7
MAX_SIGMA_F = 4.0
MIN_DAYS_FOR_BIAS = 30

# Live ensemble composition (Open-Meteo ensemble API, verified 2026-11-04): 31 GEFS members
# and 51 ECMWF ENS members. The combined bias is weighted the same way, and each model's
# own bias is applied to its members (model.ModelParams.bias_by_model).
MEMBER_WEIGHTS = {"gfs": 31.0, "ecmwf": 51.0}


def model_key(model_name: str) -> str | None:
    n = model_name.lower()
    if "ecmwf" in n:
        return "ecmwf"
    if "gfs" in n or "gefs" in n:
        return "gfs"
    return None


@dataclass(frozen=True, slots=True)
class StationFit:
    code: str
    n: int
    bias_f: float  # member-weighted mean(actual - forecast), rounded to 0.1
    mae: float
    resid_sd: float
    sigma_f: float  # suggested model parameter
    by_model: dict[str, tuple[int, float, float]]  # model -> (n, bias, mae)

    @property
    def usable(self) -> bool:
        return self.n >= MIN_DAYS_FOR_BIAS

    def bias_by_key(self) -> dict[str, float]:
        """{'gfs': bias, 'ecmwf': bias} for the models that were fitted."""
        out: dict[str, float] = {}
        for m, (_n, b, _mae) in self.by_model.items():
            k = model_key(m)
            if k is not None:
                out[k] = round(b, 1)
        return out


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _sd(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def fit_station(
    code: str,
    actual: Mapping[str, float],  # ISO date -> official extreme (F)
    forecasts: Mapping[str, Mapping[str, float]],  # model -> ISO date -> forecast extreme (F)
) -> StationFit:
    """Fit bias and spread for one station from day-aligned forecasts and actuals.

    The combined forecast is the mean across models on days where every model has a value
    (that is what the live mixture of members approximates); per-model stats are reported
    too so a bad model can be dropped.
    """
    by_model: dict[str, tuple[int, float, float]] = {}
    for m, fc in forecasts.items():
        res = [actual[d] - v for d, v in fc.items() if d in actual]
        if res:
            by_model[m] = (len(res), round(_mean(res), 2), round(_mean([abs(r) for r in res]), 2))
    common = [d for d in actual if all(d in fc for fc in forecasts.values())]
    weights = {m: MEMBER_WEIGHTS.get(model_key(m) or "", 1.0) for m in forecasts}
    wsum = sum(weights.values()) or 1.0

    def combined(d: str) -> float:  # member-weighted mean forecast, like the live mixture
        return sum(weights[m] * fc[d] for m, fc in forecasts.items()) / wsum

    res = [actual[d] - combined(d) for d in common]
    if not res:
        return StationFit(code, 0, 0.0, 0.0, 0.0, 1.5, by_model)
    bias = round(_mean(res), 1)
    sd = _sd(res)
    sigma = math.sqrt(max(0.0, sd * sd - ENSEMBLE_OWN_SD_F**2))
    sigma = round(min(MAX_SIGMA_F, max(MIN_SIGMA_F, sigma)), 1)
    return StationFit(
        code, len(res), bias, round(_mean([abs(r) for r in res]), 2), round(sd, 2), sigma, by_model
    )


def offset_table(
    pairs: Iterable[tuple[int, int]], prior: Mapping[int, float], prior_weight: float = 8.0
) -> dict[int, float]:
    """P(official - hourly extreme = d) from (hourly_extreme, official) pairs, shrunk toward
    the prior with `prior_weight` pseudo-observations. Offsets outside [-2, 3] are clipped."""
    counts: dict[int, float] = {d: p * prior_weight for d, p in prior.items()}
    n = prior_weight
    for hourly, official in pairs:
        d = max(-2, min(3, official - hourly))
        counts[d] = counts.get(d, 0.0) + 1.0
        n += 1.0
    out = {d: round(c / n, 3) for d, c in sorted(counts.items()) if c > 0}
    drift = round(1.0 - sum(out.values()), 3)
    if drift:  # rounding: put the remainder on the mode so it sums to exactly 1
        k = max(out, key=lambda d: out[d])
        out[k] = round(out[k] + drift, 3)
    return out


def calibration_yaml(fits: Iterable[StationFit], offsets: Mapping[int, float] | None) -> str:
    """A `weather:` fragment ready to paste into config.yaml (only usable fits)."""
    lines = [
        "# Generated by scripts/calibrate_weather.py. Paste under `weather:` in config.yaml.",
        "",
    ]
    if offsets:
        lines.append("obs_offset_probs:")
        for d, p in sorted(offsets.items()):
            lines.append(f'  "{d}": {p}')
    lines.append("stations:")
    any_station = False
    for f in fits:
        if not f.usable:
            lines.append(
                f"  # {f.code}: only {f.n} days, not enough to fit (need {MIN_DAYS_FOR_BIAS})"
            )
            continue
        any_station = True
        lines.append(f"  {f.code}:  # n={f.n} mae={f.mae} resid_sd={f.resid_sd}")
        lines.append(f"    bias_f: {f.bias_f}")
        for k, b in sorted(f.bias_by_key().items()):
            lines.append(f"    bias_{k}_f: {b}")
        lines.append(f"    sigma_f: {f.sigma_f}")
    if not any_station:
        lines.append("  {}")
    return "\n".join(lines) + "\n"
