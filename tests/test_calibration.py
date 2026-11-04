"""Calibration report math (Brier vs market, reliability, per-station bias, thinning)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kalshi_maker.ledger import Ledger
from kalshi_maker.weather.calibration import (
    Joined,
    build_report,
    format_report,
    join,
    lead_bucket,
    read_snapshots,
    reliability,
    score,
    station_bias,
    thin_hourly,
)


def J(**kw: Any) -> Joined:
    base = dict(
        ticker="KXHIGHNY-26NOV05-B70.5", city="NYC", day="2026-11-05", taken_at=0.0,
        lead="day-ahead", fair=0.5, confidence=1.0, mid=0.5, outcome=1, pred_mean=70.0,
        actual=71,
    )  # fmt: skip
    base.update(kw)
    return Joined(**base)  # type: ignore[arg-type]


def test_brier_and_skill_vs_market() -> None:
    rows = [J(fair=0.9, mid=0.6, outcome=1), J(fair=0.1, mid=0.4, outcome=0)]
    sc = score(rows)
    assert sc.brier_model == pytest.approx(0.01)
    assert sc.brier_market_paired == pytest.approx(0.16)
    assert sc.skill_vs_market == pytest.approx(1 - 0.01 / 0.16)


def test_paired_comparison_ignores_rows_without_market() -> None:
    rows = [J(fair=1.0, mid=None, outcome=0), J(fair=0.5, mid=0.5, outcome=1)]
    sc = score(rows)
    assert sc.n == 2 and sc.n_paired == 1
    assert sc.brier_model == pytest.approx((1 + 0.25) / 2)
    assert sc.brier_model_paired == pytest.approx(0.25) == sc.brier_market_paired


def test_reliability_bins() -> None:
    rows = [J(fair=0.05, outcome=0)] * 3 + [J(fair=0.95, outcome=1)] * 2 + [J(fair=1.0, outcome=0)]
    tbl = {lo: (mp, of, n) for lo, mp, of, n in reliability(rows)}
    assert tbl[0.0] == (pytest.approx(0.05), 0.0, 3)
    assert tbl[0.9][1:] == (pytest.approx(2 / 3), 3)  # fair=1.0 lands in the top bin


def test_station_bias_counts_each_event_hour_once() -> None:
    # 3 brackets of one event at the same hour share a mean: one error, not three
    rows = [J(ticker=f"T{i}", pred_mean=72.0, actual=70, taken_at=100.0) for i in range(3)]
    rows += [J(ticker="T9", day="2026-11-06", pred_mean=69.0, actual=70, taken_at=100.0)]
    b = station_bias(rows)
    assert (
        b.days == 2 and b.mean_error == pytest.approx((2 + -1) / 2) and b.mae == pytest.approx(1.5)
    )


def test_lead_buckets() -> None:
    start = datetime(2026, 11, 5, 5, tzinfo=UTC)
    t = start.timestamp()
    assert lead_bucket(t + 60, start) == "same-day"
    assert lead_bucket(t - 3600, start) == "day-ahead"
    assert lead_bucket(t - 40 * 3600, start) == "2+ days"


def test_thin_hourly_keeps_last_per_hour() -> None:
    snaps = [{"ticker": "A", "taken_at": t, "v": t} for t in (0, 600, 1200, 3700, 4000)]
    assert [s["v"] for s in thin_hourly(snaps)] == [1200, 4000]


def test_report_warns_on_too_little_data_and_suggests_bias() -> None:
    rows = [
        J(day=f"2026-11-0{d}", taken_at=d * 86400.0, pred_mean=71.0, actual=70, fair=0.6, mid=0.5)
        for d in (1, 2, 3)
    ]
    rep = build_report(rows, min_days=3)
    assert not any("NOT meaningful" in w for w in rep.warnings)
    assert rep.suggested_bias_f == {"NYC": -1.0}  # model 1 °F too warm -> subtract 1
    assert "NYC" in format_report(rep)
    rep2 = build_report(rows[:1], min_days=3)
    assert any("NOT meaningful" in w for w in rep2.warnings) and rep2.suggested_bias_f == {}


def test_report_flags_model_worse_than_market() -> None:
    rows = [J(fair=0.9, mid=0.1, outcome=0, taken_at=float(i)) for i in range(40)]
    rep = build_report(rows, min_days=1)
    assert any("WORSE than the market" in w for w in rep.warnings)


def test_min_confidence_filter() -> None:
    rows = [J(confidence=0.5), J(confidence=1.0, ticker="X")]
    assert build_report(rows, min_days=1, min_conf=0.6).rows == 1


# ---------- end to end with a real ledger + fake prod markets ----------

RULES = (
    "If the maximum temperature recorded at New York City (CLINYC) for Nov 5, 2026, is between "
    "70-71° fahrenheit according to The Weather Company, then the market resolves to Yes."
)
MARKET = {
    "ticker": "KXHIGHNY-26NOV05-B70.5",
    "event_ticker": "KXHIGHNY-26NOV05",
    "rules_primary": RULES,
    "strike_type": "between",
    "floor_strike": 70,
    "cap_strike": 71,
    "yes_sub_title": "70° to 71°",
    "result": "yes",
    "expiration_value": "71.00",
}


async def test_read_and_join_from_ledger(tmp_path: Path) -> None:
    db = tmp_path / "l.db"
    lg = await Ledger.open(db)
    t0 = datetime(2026, 11, 4, 12, tzinfo=UTC).timestamp()  # day-ahead for Nov 5
    for i, fair in enumerate(("0.40", "0.45")):
        lg._clock = lambda i=i: t0 + i * 600  # same hour -> thinned to the later one
        await lg.record_fair_value(
            MARKET["ticker"], fair, "1.0", "ensemble+nws+obs", "x", "0.30", "0.40",
            detail={"mean": 70.4},
        )  # fmt: skip
    await lg.close()
    snaps = read_snapshots(db)
    assert len(snaps) == 2
    rows = join(snaps, {MARKET["ticker"]: MARKET, "unsettled": {}})
    assert len(rows) == 1
    r = rows[0]
    assert (r.fair, r.mid, r.outcome, r.actual, r.lead, r.city) == (
        0.45, pytest.approx(0.35), 1, 71, "day-ahead", "NYC",
    )  # fmt: skip
    # read-only: the report can never write to the collector's ledger
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO errors (recorded_at, kind, detail) VALUES (0,'x','y')")
    con.close()


async def test_unsettled_markets_are_excluded(tmp_path: Path) -> None:
    rows = join(
        [{"ticker": MARKET["ticker"], "taken_at": 0.0, "fair": 0.5, "confidence": 1.0,
          "bid": None, "ask": None, "detail": {}}],
        {MARKET["ticker"]: {**MARKET, "result": ""}},
    )  # fmt: skip
    assert rows == []
