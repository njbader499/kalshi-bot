"""Model parameter fitting (fetching lives in scripts/calibrate_weather.py, run on the laptop)."""

from __future__ import annotations

import pytest

from kalshi_maker.weather.fit import (
    MIN_DAYS_FOR_BIAS,
    MIN_DAYS_LIVE_BIAS,
    LiveBias,
    calibration_yaml,
    clip_bias,
    fit_station,
    live_bias,
    model_key,
    offset_table,
)
from kalshi_maker.weather.model import DEFAULT_OBS_OFFSETS, ModelParams


def test_fit_recovers_a_cold_bias() -> None:
    days = [f"2026-10-{i:02d}" for i in range(1, 31)] + [f"2026-11-{i:02d}" for i in range(1, 6)]
    actual = {d: 80.0 + (i % 5) for i, d in enumerate(days)}
    gfs = {d: actual[d] - 3.0 + (0.5 if i % 2 else -0.5) for i, d in enumerate(days)}
    ecm = {d: actual[d] - 2.0 for d in days}
    f = fit_station("CLIMIA", actual, {"gfs_global": gfs, "ecmwf_ifs025": ecm})
    assert f.n == 35 and f.usable
    # combined bias is weighted like the live ensemble (31 GEFS : 51 ECMWF members)
    assert f.bias_f == pytest.approx((31 * 3.0 + 51 * 2.0) / 82, abs=0.06)
    assert f.by_model["gfs_global"][1] == pytest.approx(3.0, abs=0.01)
    assert f.by_model["ecmwf_ifs025"][1] == pytest.approx(2.0, abs=0.01)
    assert f.bias_by_key() == {"gfs": 3.0, "ecmwf": 2.0}
    assert 0.7 <= f.sigma_f <= 4.0
    ModelParams(bias_f=f.bias_f, sigma_f=f.sigma_f, bias_by_model=f.bias_by_key())


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
    assert "bias_ecmwf_f" not in text  # model "m" is neither gfs nor ecmwf: no ecmwf line
    assert "bias_gfs_f: 0.0" in text  # GFS members explicitly uncorrected by default
    assert "CLINYC: only 1 days" in text and f"need {MIN_DAYS_FOR_BIAS}" in text
    assert '"0": 0.5' in text
    lax = fit_station(
        "CLILAX", {d: 75.0 for d in days},
        {"gfs_global": {d: 74.7 for d in days}, "ecmwf_ifs025": {d: 80.1 for d in days}},
    )  # fmt: skip
    text2 = calibration_yaml([lax], None, gfs_bias="fit")
    assert "bias_gfs_f: 0.3" in text2 and "bias_ecmwf_f: -5.1" in text2
    # default: the GFS proxy is not trusted -> GFS members uncorrected, fallback = ECMWF
    text3 = calibration_yaml([lax], None)
    assert "bias_gfs_f: 0.0" in text3 and "bias_ecmwf_f: -5.1" in text3
    assert "    bias_f: -5.1" in text3


def test_fitted_biases_are_clipped_to_the_config_bound() -> None:
    days = [f"2026-10-{i:02d}" for i in range(1, 31)] + ["2026-11-01"]
    f = fit_station("CLILAX", {d: 70.0 for d in days}, {"ecmwf_ifs025": {d: 78.0 for d in days}})
    assert f.bias_f == -6.0 and f.bias_by_key() == {"ecmwf": -6.0}
    assert clip_bias(-7.3) == -6.0 and clip_bias(2.26) == 2.3
    ModelParams(bias_f=f.bias_f, bias_by_model=f.bias_by_key())


def test_live_ensemble_bias_replaces_the_archive_fit_when_it_has_enough_days() -> None:
    days = [f"2026-10-{i:02d}" for i in range(1, 31)] + ["2026-11-01"]
    f = fit_station("CLIMIA", {d: 80.0 for d in days}, {"ecmwf_ifs025": {d: 78.0 for d in days}})
    few = live_bias("CLIMIA", [(82.0, 85.0)] * 5)
    assert few.n == 5 and few.bias_f == 3.0 and not few.usable
    text = calibration_yaml([f], None, {"CLIMIA": few})
    assert "bias_ecmwf_f: 2.0" in text and "LIVE" not in text
    enough = live_bias("CLIMIA", [(82.0, 85.0)] * MIN_DAYS_LIVE_BIAS)
    assert enough.usable
    text2 = calibration_yaml([f], None, {"CLIMIA": enough})
    assert "LIVE ensemble bias" in text2 and "    bias_f: 3.0" in text2
    assert "bias_ecmwf_f" not in text2
    assert isinstance(enough, LiveBias)


def test_model_key_recognises_open_meteo_names() -> None:
    assert model_key("gfs_global") == "gfs" and model_key("gfs_seamless") == "gfs"
    assert model_key("ecmwf_ifs025") == "ecmwf" and model_key("icon_seamless") is None
