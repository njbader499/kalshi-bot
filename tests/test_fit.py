"""Model parameter fitting (fetching lives in scripts/calibrate_weather.py, run on the laptop)."""

from __future__ import annotations

import pytest

from kalshi_maker.weather.fit import (
    MIN_DAYS_FOR_BIAS,
    calibration_yaml,
    fit_station,
    offset_table,
)
from kalshi_maker.weather.model import DEFAULT_OBS_OFFSETS, ModelParams


def test_fit_recovers_a_cold_bias() -> None:
    days = [f"2026-10-{i:02d}" for i in range(1, 31)] + [f"2026-11-{i:02d}" for i in range(1, 6)]
    actual = {d: 80.0 + (i % 5) for i, d in enumerate(days)}
    gfs = {d: actual[d] - 3.0 + (0.5 if i % 2 else -0.5) for i, d in enumerate(days)}
    ecm = {d: actual[d] - 2.0 for d in days}
    f = fit_station("CLIMIA", actual, {"gfs_seamless": gfs, "ecmwf_ifs025": ecm})
    assert f.n == 35 and f.usable
    assert f.bias_f == pytest.approx(2.5, abs=0.11)  # mean of the two models' biases
    assert f.by_model["gfs_seamless"][1] == pytest.approx(3.0, abs=0.01)
    assert f.by_model["ecmwf_ifs025"][1] == pytest.approx(2.0, abs=0.01)
    assert 0.5 <= f.sigma_f <= 4.0
    ModelParams(bias_f=f.bias_f, sigma_f=f.sigma_f)  # within the model's bounds


def test_fit_with_no_overlap_is_unusable() -> None:
    f = fit_station("CLINYC", {"2026-11-01": 70.0}, {"gfs_seamless": {"2026-10-01": 60.0}})
    assert f.n == 0 and not f.usable


def test_offsets_shrink_toward_the_prior_then_follow_the_data() -> None:
    few = offset_table([(71, 72)], DEFAULT_OBS_OFFSETS)
    assert abs(sum(few.values()) - 1) < 1e-9
    assert few[1] > DEFAULT_OBS_OFFSETS[1]  # nudged up, not overturned
    many = offset_table([(70, 70)] * 200, DEFAULT_OBS_OFFSETS)
    assert many[0] > 0.9
    ModelParams(obs_offsets=many)  # still a valid table


def test_offsets_are_clipped() -> None:
    t = offset_table([(60, 75)], DEFAULT_OBS_OFFSETS)
    assert max(t) == 3 and abs(sum(t.values()) - 1) < 1e-9


def test_yaml_fragment_only_includes_usable_fits() -> None:
    days = [f"2026-10-{i:02d}" for i in range(1, 31)] + ["2026-11-01"]
    good = fit_station("CLIMIA", {d: 80.0 for d in days}, {"m": {d: 78.0 for d in days}})
    bad = fit_station("CLINYC", {"2026-11-01": 70.0}, {"m": {"2026-11-01": 69.0}})
    text = calibration_yaml([good, bad], {-1: 0.05, 0: 0.5, 1: 0.4, 2: 0.05})
    assert "  CLIMIA:" in text and "bias_f: 2.0" in text
    assert "CLINYC: only 1 days" in text and f"need {MIN_DAYS_FOR_BIAS}" in text
    assert '"0": 0.5' in text
