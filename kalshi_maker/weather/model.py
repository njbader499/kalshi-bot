"""Probability model for an integer daily max/min (pure functions, no I/O).

Model v2 (2026-11-05). The reported value is X = max(B, Y) for a daily max (min(B, Y) for a
daily min), where

  * B is what has ALREADY happened in the climate day: the extreme of the observations so
    far, as the station will report it. It is a small integer distribution, not a point:
    the official extreme is usually the hourly-METAR extreme or one degree beyond it (the
    station's daily summary uses finer-than-hourly data; °C->°F rounding can shift it by one).
    `obs_offset_probs` holds P(official - hourly_extreme = d). An intraday CLI value is an
    exact floor (ceiling for a min) on top of that.
  * Y is the extreme over the REMAINING part of the climate day, a mixture over forecast
    inputs: ensemble members N(member_remaining_extreme + bias, sigma), equal weights summing
    to (1 - w_nws), and the NWS forecast N(nws_remaining_extreme + bias, sigma_nws) with
    weight w_nws. Y is rounded half-up to an integer before the max.

Why v2: v1 conditioned the FULL-DAY forecast on "max so far >= B" by dropping the mass below
B and renormalising. Late in the day that left a large tail above B (at 23:00 local with
B = 72 it still gave 38% to 74+), when the remaining evening hours cannot produce a new
high. v1 Brier on the 2026-11-04 brackets was 0.04-0.09 through the evening versus 0.000
for the real market. With X = max(B, Y) and Y from the remaining hours only, the tail
collapses onto B as the day runs out, which is what actually happens.

A final CLI report pins X exactly (point mass). Statistics are done in float (they are
probabilities and temperatures, not money); the result becomes a Decimal price only at the
boundary (engine.py). Defaults are priors; `scripts/calibrate_weather.py` fits them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

Dist = dict[int, float]

# P(official extreme - hourly-METAR extreme = d). Prior from the eight 2026-11-04 cities
# (official was the METAR max in 3, one degree above in 4, ambiguous in 1); re-fit from the
# shadow log (obs_f at the last pre-final snapshot vs cli_final) by the calibration script.
DEFAULT_OBS_OFFSETS: dict[int, float] = {-1: 0.05, 0: 0.45, 1: 0.40, 2: 0.10}


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _check_offsets(offsets: Mapping[int, float]) -> dict[int, float]:
    if not offsets:
        raise ValueError("obs_offset_probs is empty")
    tot = 0.0
    for d, p in offsets.items():
        if not isinstance(d, int) or not (-3 <= d <= 3):
            raise ValueError(f"obs offset {d!r} out of range [-3, 3]")
        if not (0 <= p <= 1):
            raise ValueError(f"obs offset prob {p} for {d} not in [0, 1]")
        tot += p
    if abs(tot - 1.0) > 1e-6:
        raise ValueError(f"obs_offset_probs sum to {tot}, not 1")
    return {int(d): float(p) for d, p in offsets.items() if p > 0}


MODEL_KEYS = ("gfs", "ecmwf")


def model_of_member(member_key: str) -> str | None:
    """Which ensemble a member series belongs to, from its Open-Meteo series name
    (e.g. 'temperature_2m_member03_ncep_gefs_seamless', 'temperature_2m_ecmwf_ifs025_ensemble')."""
    k = member_key.lower()
    if "ecmwf" in k:
        return "ecmwf"
    if "gfs" in k or "gefs" in k:
        return "gfs"
    return None


@dataclass(frozen=True, slots=True)
class ModelParams:
    sigma_f: float = 1.5  # spread added around each ensemble member's remaining extreme
    nws_weight: float = 0.5
    nws_sigma_f: float = 2.0
    # Bias (F) added to ENSEMBLE MEMBERS only: the raw global models run cold/warm at a
    # station (resolution, coast, hourly sampling). Per model when fitted (bias_by_model,
    # keys 'gfs'/'ecmwf'), else bias_f. The NWS forecast is forecaster-adjusted and gets
    # nws_bias_f (default 0): calibrating it against raw-model residuals double-counted.
    bias_f: float = 0.0
    bias_by_model: dict[str, float] = field(default_factory=dict)
    nws_bias_f: float = 0.0
    obs_slack_p: float = 0.05  # kept for v1 helpers; v2 uses obs_offsets
    obs_offsets: dict[int, float] = field(default_factory=lambda: dict(DEFAULT_OBS_OFFSETS))

    def __post_init__(self) -> None:
        if not (0.3 <= self.sigma_f <= 10):
            raise ValueError(f"sigma_f {self.sigma_f} out of bounds [0.3, 10]")
        if not (0.3 <= self.nws_sigma_f <= 10):
            raise ValueError(f"nws_sigma_f {self.nws_sigma_f} out of bounds [0.3, 10]")
        if not (0 <= self.nws_weight <= 1):
            raise ValueError("nws_weight must be in [0, 1]")
        if not (0 <= self.obs_slack_p < 0.5):
            raise ValueError(f"obs_slack_p {self.obs_slack_p} out of bounds [0, 0.5)")
        for name, b in (("bias_f", self.bias_f), ("nws_bias_f", self.nws_bias_f)):
            if not (-6 <= b <= 6):
                raise ValueError(f"{name} {b} out of bounds [-6, 6]")
        for k, b in self.bias_by_model.items():
            if k not in MODEL_KEYS:
                raise ValueError(f"bias_by_model key {k!r} not in {MODEL_KEYS}")
            if not (-6 <= b <= 6):
                raise ValueError(f"bias_by_model[{k}] {b} out of bounds [-6, 6]")
        object.__setattr__(self, "obs_offsets", _check_offsets(self.obs_offsets))

    def bias_for(self, member_key: str) -> float:
        m = model_of_member(member_key)
        if m is not None and m in self.bias_by_model:
            return self.bias_by_model[m]
        return self.bias_f


def integer_distribution(
    members: Sequence[float],
    nws: float | None,
    p: ModelParams,
    member_biases: Sequence[float] | None = None,
) -> Dist:
    """Integer distribution of a forecast extreme Y from ensemble members and/or NWS.

    `member_biases` (one per member, from ModelParams.bias_for) overrides the flat bias_f.
    The NWS component gets nws_bias_f only.
    """
    comps: list[tuple[float, float, float]] = []  # (weight, mu, sigma)
    w_nws = p.nws_weight if nws is not None else 0.0
    if not members and nws is None:
        raise ValueError("no forecast inputs")
    if members:
        if member_biases is not None and len(member_biases) != len(members):
            raise ValueError("member_biases must match members")
        biases = member_biases if member_biases is not None else [p.bias_f] * len(members)
        w_each = (1.0 - w_nws) / len(members) if nws is not None else 1.0 / len(members)
        comps += [(w_each, m + b, p.sigma_f) for m, b in zip(members, biases, strict=True)]
    if nws is not None:
        comps.append((w_nws if members else 1.0, nws + p.nws_bias_f, p.nws_sigma_f))
    lo = math.floor(min(mu - 6 * s for _, mu, s in comps))
    hi = math.ceil(max(mu + 6 * s for _, mu, s in comps))
    dist: Dist = {}
    for k in range(lo, hi + 1):
        pk = 0.0
        for w, mu, s in comps:
            pk += w * (_phi((k + 0.5 - mu) / s) - _phi((k - 0.5 - mu) / s))
        if pk > 1e-12:
            dist[k] = pk
    return normalize(dist)


def normalize(d: Dist) -> Dist:
    tot = sum(d.values())
    if tot <= 0:
        raise ValueError("empty distribution")
    return {k: v / tot for k, v in d.items()}


# ---- v2: what has already happened, combined with what is still to come ----


def bound_distribution(
    obs_extreme: int | None, official_partial: int | None, kind: str, offsets: Mapping[int, float]
) -> Dist | None:
    """Distribution of B, the station's reported extreme of the hours already observed.

    obs_extreme: integer °F from the hourly observations so far (None if none yet).
    official_partial: an intraday CLI value (exact so far), or None.
    For a max, B = max(official_partial, obs_extreme + d) with d ~ offsets; mirrored for a min.
    Returns None when nothing has been observed.
    """
    if obs_extreme is None and official_partial is None:
        return None
    sign = 1 if kind == "max" else -1
    if official_partial is not None and (
        obs_extreme is None or sign * (official_partial - obs_extreme) >= 0
    ):
        # The official intraday value already reflects the station's finer data up to its
        # issuance and the hourly obs have not gone past it since: it IS the extreme so far.
        return {int(official_partial): 1.0}
    assert obs_extreme is not None
    out: Dist = {}
    for d, p in offsets.items():
        b = obs_extreme + sign * d
        if official_partial is not None:  # obs went past an earlier official value
            b = max(b, official_partial) if kind == "max" else min(b, official_partial)
        out[b] = out.get(b, 0.0) + p
    return normalize(out)


def combine_with_bound(remaining: Dist | None, bound: Dist | None, kind: str) -> Dist:
    """Distribution of X = max(B, Y) (min for a daily min).

    `remaining` is Y, the forecast extreme over the hours still to come (None if the day is
    over); `bound` is B (None before any observation). Mass of Y that cannot beat B
    collapses ONTO B; it is never dropped and renormalised (that was the v1 bug).
    """
    if bound is None:
        if remaining is None:
            raise ValueError("nothing known about this day")
        return dict(remaining)
    if remaining is None:
        return dict(bound)
    out: Dist = {}
    for b, pb in bound.items():
        for y, py in remaining.items():
            x = max(b, y) if kind == "max" else min(b, y)
            out[x] = out.get(x, 0.0) + pb * py
    return normalize(out)


def prob_in(d: Dist, lo: int | None, hi: int | None) -> float:
    return sum(v for k, v in d.items() if (lo is None or k >= lo) and (hi is None or k <= hi))


def mode_of(d: Dist) -> int:
    return max(d, key=lambda k: (d[k], -k))


def obs_round_f(obs_c: float) -> int:
    """METAR reading in °C -> the integer °F the station most likely reports."""
    return round_half_up(c_to_f(obs_c))


# ---- v1 helpers (kept for tests and the settlement check; not used by the v2 engine) ----


def condition_at_least(d: Dist, bound: int) -> Dist:
    kept = {k: v for k, v in d.items() if k >= bound}
    return normalize(kept) if kept else {bound: 1.0}


def condition_at_most(d: Dist, bound: int) -> Dist:
    kept = {k: v for k, v in d.items() if k <= bound}
    return normalize(kept) if kept else {bound: 1.0}


def condition_observed(d: Dist, r: int, kind: str, slack_p: float) -> Dist:
    if not (0 <= slack_p < 0.5):
        raise ValueError("slack_p must be in [0, 0.5)")
    if kind == "max":
        main, alt = condition_at_least(d, r), r - 1
    else:
        main, alt = condition_at_most(d, r), r + 1
    out = {k: v * (1 - slack_p) for k, v in main.items()}
    out[alt] = out.get(alt, 0.0) + slack_p
    return normalize({k: v for k, v in out.items() if v > 0})
