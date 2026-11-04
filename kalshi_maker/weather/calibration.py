"""Calibration report for the weather fair-value engine (run after 3+ days of shadow data).

  python -m kalshi_maker calibrate [--db data\\ledger.db] [--min-days 3] [--csv out.csv]

Joins the ledger's fair_value_snapshots with each market's real outcome from PROD public
data (`result` yes/no and `expiration_value`, the settled integer °F) and reports:

  * Brier score of the model vs the market mid on the SAME rows (paired), and a skill
    score 1 - B_model / B_market. The market is the baseline to beat, not zero.
  * Reliability: predicted vs observed frequency in 10 bins, overall and by city.
  * Per-station bias: mean(predicted mean °F - settled °F) per city and lead bucket, with
    a suggested `weather.bias_f` correction (only the day-ahead bucket, only with enough
    days). The owner decides whether to apply it.

Rules: snapshots are thinned to one per market per hour (the collector logs every 10 min).
Lead buckets are measured from the climate-day window: "same-day" (the day had started),
"day-ahead" (< 36 h before it started), "2+ days". Only finalized markets count. The ledger
is opened READ-ONLY, so the report never interferes with a running collector.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import RulesError, climate_day_window, parse_contract

LEAD_BUCKETS = ("same-day", "day-ahead", "2+ days")
CITY = {"CLINYC": "NYC", "CLIMIA": "Miami", "CLIMDW": "Chicago"}


@dataclass(frozen=True, slots=True)
class Joined:
    ticker: str
    city: str
    day: str
    taken_at: float
    lead: str
    fair: float
    confidence: float
    mid: float | None
    outcome: int  # 1 if the market settled YES
    pred_mean: float | None  # model's expected settlement value (°F)
    actual: int | None  # settled value (°F)


@dataclass
class Score:
    n: int = 0
    brier_model: float | None = None
    n_paired: int = 0
    brier_model_paired: float | None = None
    brier_market_paired: float | None = None

    @property
    def skill_vs_market(self) -> float | None:
        if not self.brier_market_paired or self.brier_model_paired is None:
            return None
        return 1.0 - self.brier_model_paired / self.brier_market_paired


@dataclass
class Bias:
    days: int = 0
    mean_error: float | None = None  # predicted - actual (°F); positive = model too warm
    mae: float | None = None


@dataclass
class Report:
    days_with_outcomes: int
    rows: int
    scores: dict[str, Score] = field(default_factory=dict)  # key "city|lead" or "ALL|lead"
    reliability: dict[str, list[tuple[float, float, float, int]]] = field(default_factory=dict)
    bias: dict[str, Bias] = field(default_factory=dict)  # key "city|lead"
    suggested_bias_f: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def lead_bucket(taken_at: float, window_start: datetime) -> str:
    hours = (window_start.timestamp() - taken_at) / 3600
    if hours <= 0:
        return "same-day"
    if hours < 36:
        return "day-ahead"
    return "2+ days"


def thin_hourly(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the last snapshot per (ticker, hour)."""
    keep: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        k = (r["ticker"], int(r["taken_at"] // 3600))
        if k not in keep or r["taken_at"] >= keep[k]["taken_at"]:
            keep[k] = r
    return sorted(keep.values(), key=lambda r: (r["ticker"], r["taken_at"]))


def read_snapshots(db: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(fair_value_snapshots)")}
        detail = "detail" if "detail" in cols else "NULL"
        cur = con.execute(
            f"SELECT taken_at, ticker, fair, confidence, yes_bid, yes_ask, {detail} "  # noqa: S608
            "FROM fair_value_snapshots ORDER BY taken_at"
        )
        out = []
        for t, ticker, fair, conf, bid, ask, det in cur:
            out.append(
                {
                    "taken_at": float(t),
                    "ticker": ticker,
                    "fair": float(fair),
                    "confidence": float(conf),
                    "bid": None if bid in (None, "") else float(bid),
                    "ask": None if ask in (None, "") else float(ask),
                    "detail": json.loads(det) if det else {},
                }
            )
        return out
    finally:
        con.close()


def join(snapshots: list[dict[str, Any]], markets: dict[str, dict[str, Any]]) -> list[Joined]:
    out: list[Joined] = []
    for s in thin_hourly(snapshots):
        m = markets.get(s["ticker"])
        if not m or m.get("result") not in ("yes", "no"):
            continue
        try:
            c = parse_contract(m)
        except RulesError:
            continue
        start, _ = climate_day_window(c.day, c.station)
        mid = None
        if s["bid"] is not None and s["ask"] is not None and s["ask"] >= s["bid"]:
            mid = (s["bid"] + s["ask"]) / 2
        ev = m.get("expiration_value")
        try:
            actual = int(float(ev)) if ev not in (None, "") else None
        except ValueError:
            actual = None
        pm = s["detail"].get("mean")
        out.append(
            Joined(
                ticker=s["ticker"],
                city=CITY.get(c.station.cli_code, c.station.cli_code),
                day=c.day.isoformat(),
                taken_at=s["taken_at"],
                lead=lead_bucket(s["taken_at"], start),
                fair=s["fair"],
                confidence=s["confidence"],
                mid=mid,
                outcome=1 if m["result"] == "yes" else 0,
                pred_mean=None if pm is None else float(pm),
                actual=actual,
            )
        )
    return out


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def score(rows: list[Joined]) -> Score:
    sc = Score(n=len(rows), brier_model=_brier([(r.fair, r.outcome) for r in rows]))
    paired = [r for r in rows if r.mid is not None]
    sc.n_paired = len(paired)
    sc.brier_model_paired = _brier([(r.fair, r.outcome) for r in paired])
    sc.brier_market_paired = _brier([(r.mid, r.outcome) for r in paired])  # type: ignore[misc]
    return sc


def reliability(rows: list[Joined], bins: int = 10) -> list[tuple[float, float, float, int]]:
    """[(bin_lo, mean_predicted, observed_freq, n)] for non-empty bins."""
    acc: dict[int, list[Joined]] = defaultdict(list)
    for r in rows:
        acc[min(bins - 1, int(r.fair * bins))].append(r)
    out = []
    for b in sorted(acc):
        rs = acc[b]
        out.append(
            (
                b / bins,
                sum(r.fair for r in rs) / len(rs),
                sum(r.outcome for r in rs) / len(rs),
                len(rs),
            )
        )
    return out


def station_bias(rows: list[Joined]) -> Bias:
    """One error per (day, hour) snapshot of the event's mean, averaged per day first."""
    per_day: dict[str, list[float]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for r in rows:
        if r.pred_mean is None or r.actual is None:
            continue
        k = (r.day, int(r.taken_at // 3600))
        if k in seen:  # brackets of one event share the same mean: count it once
            continue
        seen.add(k)
        per_day[r.day].append(r.pred_mean - r.actual)
    if not per_day:
        return Bias()
    day_err = [sum(v) / len(v) for v in per_day.values()]
    return Bias(
        days=len(day_err),
        mean_error=sum(day_err) / len(day_err),
        mae=sum(abs(e) for e in day_err) / len(day_err),
    )


def build_report(rows: list[Joined], min_days: int = 3, min_conf: float | None = None) -> Report:
    if min_conf is not None:
        rows = [r for r in rows if r.confidence >= min_conf]
    days = {(r.city, r.day) for r in rows}
    rep = Report(days_with_outcomes=len({d for _, d in days}), rows=len(rows))
    if rep.days_with_outcomes < min_days:
        rep.warnings.append(
            f"only {rep.days_with_outcomes} settled day(s) of data (< {min_days}); "
            "numbers below are NOT meaningful yet"
        )
    cities = sorted({r.city for r in rows})
    for city in ["ALL", *cities]:
        sel = rows if city == "ALL" else [r for r in rows if r.city == city]
        for lead in ("all", *LEAD_BUCKETS):
            s2 = sel if lead == "all" else [r for r in sel if r.lead == lead]
            if s2:
                rep.scores[f"{city}|{lead}"] = score(s2)
        if sel:
            rep.reliability[city] = reliability(sel)
        if city != "ALL":
            for lead in LEAD_BUCKETS:
                b = station_bias([r for r in sel if r.lead == lead])
                if b.days:
                    rep.bias[f"{city}|{lead}"] = b
            da = rep.bias.get(f"{city}|day-ahead")
            if da and da.days >= min_days and da.mean_error is not None:
                rep.suggested_bias_f[city] = round(-da.mean_error, 1)
    for k, sc in rep.scores.items():
        sk = sc.skill_vs_market
        if k.endswith("|all") and sk is not None and sc.n_paired >= 30 and sk < 0:
            rep.warnings.append(
                f"{k.split('|')[0]}: model is WORSE than the market mid (skill {sk:+.3f})"
            )
    return rep


def _f(x: float | None, fmt: str = ".4f") -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, fmt)


def format_report(rep: Report) -> str:
    out = [
        f"CALIBRATION REPORT  ({rep.rows} hourly snapshots, "
        f"{rep.days_with_outcomes} settled day(s))",
        "Brier: lower is better. Skill = 1 - Brier_model / Brier_market on the same rows",
        "(> 0 means the model beat the market mid; that, not zero, is the bar).",
    ]
    for w in rep.warnings:
        out.append(f"WARNING: {w}")
    out.append("")
    out.append(
        f"{'city':<8} {'lead':<10} {'n':>5} {'Brier':>7} {'paired':>6} "
        f"{'B_model':>8} {'B_mkt':>8} {'skill':>7}"
    )
    for k, sc in rep.scores.items():
        city, lead = k.split("|")
        out.append(
            f"{city:<8} {lead:<10} {sc.n:>5} {_f(sc.brier_model):>7} {sc.n_paired:>6} "
            f"{_f(sc.brier_model_paired):>8} {_f(sc.brier_market_paired):>8} "
            f"{_f(sc.skill_vs_market, '+.3f'):>7}"
        )
    out.append("")
    out.append("RELIABILITY (predicted vs observed YES frequency)")
    for city, tbl in rep.reliability.items():
        out.append(f"  {city}:")
        for lo, mp, of, n in tbl:
            bar = "#" * round(of * 20)
            out.append(
                f"    [{lo:.1f}-{lo + 0.1:.1f})  pred {mp:.3f}  obs {of:.3f}  n={n:<5} {bar}"
            )
    out.append("")
    out.append("PER-STATION BIAS (predicted mean - settled °F; + = model too warm)")
    for k, b in rep.bias.items():
        city, lead = k.split("|")
        out.append(
            f"  {city:<8} {lead:<10} days={b.days:<3} "
            f"bias={_f(b.mean_error, '+.2f')}  MAE={_f(b.mae, '.2f')}"
        )
    if rep.suggested_bias_f:
        out.append("")
        out.append(
            "Suggested weather.bias_f per city (day-ahead; NOT applied automatically, and the "
            "config has one global value today): "
            + ", ".join(f"{c} {v:+.1f}" for c, v in rep.suggested_bias_f.items())
        )
    return "\n".join(out)


def write_csv(rows: list[Joined], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()) if rows else ["ticker"])
        w.writeheader()
        for r in rows:
            d = asdict(r)
            d["taken_at"] = datetime.fromtimestamp(r.taken_at, UTC).isoformat()
            w.writerow(d)


async def fetch_outcomes(api: Any, tickers: Iterable[str]) -> dict[str, dict[str, Any]]:
    """GET /markets/{ticker} on PROD for each ticker (public). Unknown tickers are skipped."""
    out: dict[str, dict[str, Any]] = {}
    for t in sorted(set(tickers)):
        try:
            out[t] = await api.get_market(t)
        except Exception:  # noqa: S112 - a missing market just has no outcome yet
            continue
    return out
