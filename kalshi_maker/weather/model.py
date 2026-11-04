"""Probability model for an integer daily max/min (pure functions, no I/O).

Latent continuous temperature T is a mixture:
  * ensemble members: N(member_extreme + bias, sigma), equal weights summing to (1 - w_nws)
  * NWS point forecast: N(nws_extreme + bias, sigma_nws), weight w_nws (if available)
The reported value is X = round-half-up(T), an integer °F. So
  P(X = k) = sum_i w_i [Phi((k + 0.5 - mu_i)/s_i) - Phi((k - 0.5 - mu_i)/s_i)].

Conditioning on what has already happened in the climate day:
  * An intraday CLI value (official integer) is a hard bound: X >= it for a max.
  * A METAR-derived extreme r is a soft bound: X >= r with probability 1 - slack, X = r - 1
    with probability slack (°C->°F conversion can be off by one degree).
  * For a min, everything mirrors.
  * A final CLI report pins X exactly (point mass).

Statistics are done in float (they are probabilities and temperatures, not money). The
result is turned into a Decimal price only at the boundary (engine.py).

Defaults are untuned priors (DECISIONS.md). Phase 3's shadow log exists to calibrate them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Dist = dict[int, float]


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


@dataclass(frozen=True, slots=True)
class ModelParams:
    sigma_f: float = 2.0  # spread added around each ensemble member
    nws_weight: float = 0.3
    nws_sigma_f: float = 2.5
    bias_f: float = 0.0  # added to every component (hourly sampling misses peaks)
    obs_slack_p: float = 0.05  # P(official integer is 1 degree inside the METAR one)

    def __post_init__(self) -> None:
        if not (0.3 <= self.sigma_f <= 10):
            raise ValueError(f"sigma_f {self.sigma_f} out of bounds [0.3, 10]")
        if not (0.3 <= self.nws_sigma_f <= 10):
            raise ValueError(f"nws_sigma_f {self.nws_sigma_f} out of bounds [0.3, 10]")
        if not (0 <= self.nws_weight <= 1):
            raise ValueError("nws_weight must be in [0, 1]")
        if not (0 <= self.obs_slack_p < 0.5):
            raise ValueError(f"obs_slack_p {self.obs_slack_p} out of bounds [0, 0.5)")
        if not (-5 <= self.bias_f <= 5):
            raise ValueError(f"bias_f {self.bias_f} out of bounds [-5, 5]")


def integer_distribution(
    members: Sequence[float],
    nws: float | None,
    p: ModelParams,
) -> Dist:
    comps: list[tuple[float, float, float]] = []  # (weight, mu, sigma)
    w_nws = p.nws_weight if nws is not None else 0.0
    if not members and nws is None:
        raise ValueError("no forecast inputs")
    if members:
        w_each = (1.0 - w_nws) / len(members) if nws is not None else 1.0 / len(members)
        comps += [(w_each, m + p.bias_f, p.sigma_f) for m in members]
    if nws is not None:
        comps.append((w_nws if members else 1.0, nws + p.bias_f, p.nws_sigma_f))
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


def condition_at_least(d: Dist, bound: int) -> Dist:
    """Max so far >= bound. If the forecast put no mass there, the observation wins."""
    kept = {k: v for k, v in d.items() if k >= bound}
    return normalize(kept) if kept else {bound: 1.0}


def condition_at_most(d: Dist, bound: int) -> Dist:
    kept = {k: v for k, v in d.items() if k <= bound}
    return normalize(kept) if kept else {bound: 1.0}


def prob_in(d: Dist, lo: int | None, hi: int | None) -> float:
    return sum(v for k, v in d.items() if (lo is None or k >= lo) and (hi is None or k <= hi))


def obs_round_f(obs_c: float) -> int:
    """METAR reading in °C -> the integer °F the station most likely reports."""
    return round_half_up(c_to_f(obs_c))


def condition_observed(d: Dist, r: int, kind: str, slack_p: float) -> Dist:
    """Condition on an observed extreme so far, `r` (integer °F from a METAR in °C).

    Converting °C back to °F can be off by one degree from the station's own integer °F
    (whole-°C METARs especially). So with probability 1 - slack_p the official extreme is
    at least r (at most r for a min), shaped by the forecast's tail; with probability
    slack_p it is r - 1 (r + 1 for a min).
    """
    if not (0 <= slack_p < 0.5):
        raise ValueError("slack_p must be in [0, 0.5)")
    if kind == "max":
        main, alt = condition_at_least(d, r), r - 1
    else:
        main, alt = condition_at_most(d, r), r + 1
    out = {k: v * (1 - slack_p) for k, v in main.items()}
    out[alt] = out.get(alt, 0.0) + slack_p
    return normalize({k: v for k, v in out.items() if v > 0})
