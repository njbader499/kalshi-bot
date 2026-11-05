"""Fit the weather model's per-station bias/spread and the observation offset table.

Run on the laptop (it needs Open-Meteo, NCEI and the local ledger):

    .\\.venv\\Scripts\\python.exe scripts\\calibrate_weather.py --days 365
    .\\.venv\\Scripts\\python.exe scripts\\calibrate_weather.py --days 365 ^
        --out config\\weather_calibration.yaml

Data:
  * Forecasts: Open-Meteo Historical Forecast API, daily temperature_2m_max/min per model,
    i.e. the same hourly-sampled daily extreme the live bot builds from the ensemble, for
    the station coordinates, in the station's LOCAL STANDARD time (fixed offset, the NWS
    climate day). Models: `gfs_global` (plain GFS; NOT `gfs_seamless`, which blends in the
    3 km HRRR for the US and does not represent the GEFS members the bot uses; falls back to
    gfs_seamless if the API rejects gfs_global) and `ecmwf_ifs025` (matches the ENS members).
  * Two windows are fitted: the full --days window and the last --recent-days (default
    90). Biases can be seasonal (a coastal marine layer, say), so the YAML uses the recent
    window when it has at least 60 days; both are printed.
  * Actuals: NCEI GHCN-Daily (dataset daily-summaries) TMAX/TMIN for the first-order
    station, units=standard (F). This is the official daily extreme the NWS climate
    report carries. Station ids below are the GHCN ids of the settlement stations; the
    script prints the station NAME NCEI returns so you can eyeball it.
  * Offsets: the bot's own ledger (fair_value_snapshots detail: obs_f just before the
    final CLI vs cli_final).

Output: a report on stdout and, with --out, a YAML fragment to paste under `weather:` in
config/config.yaml. Nothing is changed automatically.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_maker.weather.fit import (
    MIN_DAYS_LIVE_BIAS,
    LiveBias,
    StationFit,
    calibration_yaml,
    fit_station,
    live_bias,
    model_key,
    offset_table,
)
from kalshi_maker.weather.model import DEFAULT_OBS_OFFSETS
from kalshi_maker.weather.stations import SERIES_STATION, STATIONS

UA = "kalshi-maker/0.1 calibration (personal research; contact via repo owner)"
OPEN_METEO = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NCEI = "https://www.ncei.noaa.gov/access/services/data/v1"

# GHCN-Daily ids of the settlement stations (first-order ASOS sites). Verified by NAME at run
# time: the script prints what NCEI returns for each id.
GHCN_IDS = {
    "CLINYC": "USW00094728",  # NY CITY CENTRAL PARK
    "CLIMIA": "USW00012839",  # MIAMI INTL AP
    "CLIMDW": "USW00014819",  # CHICAGO MIDWAY AP
    "CLILAX": "USW00023174",  # LOS ANGELES INTL AP
    "CLIATL": "USW00013874",  # ATLANTA HARTSFIELD INTL AP
    "CLIBOS": "USW00014739",  # BOSTON LOGAN INTL AP
    "CLIDFW": "USW00003927",  # DALLAS FT WORTH INTL AP
    "CLIDEN": "USW00003017",  # DENVER INTL AP
}


def lst_tz(offset_hours: int) -> str:
    """IANA fixed-offset zone for local standard time: UTC-5 -> 'Etc/GMT+5' (sign inverted)."""
    return f"Etc/GMT{'+' if offset_hours < 0 else '-'}{abs(offset_hours)}"


FALLBACK_MODELS = {"gfs_global": "gfs_seamless"}


def fetch_forecasts(
    client: httpx.Client, code: str, models: list[str], start: date, end: date, kind: str
) -> dict[str, dict[str, float]]:
    st = STATIONS[code]
    var = "temperature_2m_max" if kind == "max" else "temperature_2m_min"
    out: dict[str, dict[str, float]] = {}
    for m in models:
        tried = m
        while True:
            r = client.get(
                OPEN_METEO,
                params={
                    "latitude": st.lat, "longitude": st.lon, "daily": var, "models": tried,
                    "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "temperature_unit": "fahrenheit", "timezone": lst_tz(st.lst_offset_hours),
                },
            )  # fmt: skip
            if r.status_code == 400 and tried in FALLBACK_MODELS:
                print(f"   model {tried} rejected by Open-Meteo; using {FALLBACK_MODELS[tried]}")
                tried = FALLBACK_MODELS[tried]
                continue
            r.raise_for_status()
            break
        d = r.json()
        days = d["daily"]["time"]
        vals = d["daily"][var]
        out[tried] = {t: float(v) for t, v in zip(days, vals, strict=True) if v is not None}
    return out


def fetch_actuals(
    client: httpx.Client, code: str, start: date, end: date, kind: str
) -> tuple[dict[str, float], str]:
    dt = "TMAX" if kind == "max" else "TMIN"
    r = client.get(
        NCEI,
        params={
            "dataset": "daily-summaries", "stations": GHCN_IDS[code], "dataTypes": dt,
            "startDate": start.isoformat(), "endDate": end.isoformat(), "format": "json",
            "units": "standard", "includeStationName": "true",
        },
    )  # fmt: skip
    r.raise_for_status()
    rows = r.json()
    name = str(rows[0].get("NAME", "?")) if rows else "?"
    return {str(x["DATE"]): float(x[dt]) for x in rows if x.get(dt) not in (None, "")}, name


def live_bias_from_ledger(path: Path) -> dict[str, list[tuple[float, float]]]:
    """Per station: (raw ensemble p50 at the last snapshot before the climate day started,
    settled value) pairs from the shadow log. Only model-v2 rows carry `remaining_hours`."""
    out: dict[str, list[tuple[float, float]]] = {}
    if not path.exists():
        return out
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT ticker, taken_at, detail FROM fair_value_snapshots WHERE detail IS NOT NULL "
        "ORDER BY taken_at"
    ).fetchall()
    finals: dict[str, int] = {}
    day_ahead: dict[str, float] = {}  # event -> raw p50 at the last pre-day snapshot
    for ticker, _taken_at, detail in rows:
        ev = ticker.rsplit("-", 1)[0]
        try:
            d = json.loads(detail)
        except json.JSONDecodeError:
            continue
        if d.get("cli_final") is not None:
            finals[ev] = int(d["cli_final"])
        elif d.get("remaining_hours") == 24 and d.get("ens_p10_p50_p90"):
            day_ahead[ev] = float(d["ens_p10_p50_p90"][1])  # later snapshots overwrite
    for ev, final in finals.items():
        if ev in day_ahead:
            code = SERIES_STATION.get(ev.split("-", 1)[0])
            if code:
                out.setdefault(code, []).append((day_ahead[ev], float(final)))
    return out


def offsets_from_ledger(path: Path) -> list[tuple[int, int]]:
    """(hourly obs extreme, official final) pairs from the shadow log."""
    if not path.exists():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT ticker, taken_at, detail FROM fair_value_snapshots WHERE detail IS NOT NULL "
        "ORDER BY taken_at"
    ).fetchall()
    finals: dict[str, int] = {}
    last_obs: dict[str, tuple[float, int]] = {}
    for ticker, taken_at, detail in rows:
        ev = ticker.rsplit("-", 1)[0]
        try:
            d = json.loads(detail)
        except json.JSONDecodeError:
            print(f"skipping unreadable detail for {ticker}")
            continue
        if d.get("cli_final") is not None:
            finals[ev] = int(d["cli_final"])
        elif d.get("obs_f") is not None:
            day_over = d.get("remaining_hours", 1) == 0
            if day_over or ev not in last_obs:
                last_obs[ev] = (float(taken_at), int(d["obs_f"]))
    pairs = []
    for ev, final in finals.items():
        if ev in last_obs:
            pairs.append((last_obs[ev][1], final))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--recent-days", type=int, default=90)
    ap.add_argument("--models", default="gfs_global,ecmwf_ifs025")
    ap.add_argument("--stations", default=",".join(GHCN_IDS))
    ap.add_argument("--kind", choices=["max", "min"], default="max")
    ap.add_argument("--ledger", default="data/ledger.db")
    ap.add_argument(
        "--gfs-bias", choices=["zero", "fit"], default="zero",
        help="write the fitted GFS member bias ('fit') or leave GFS members uncorrected "
        "('zero', default: the deterministic gfs_global archive is a weak proxy for GEFS)",
    )  # fmt: skip
    ap.add_argument("--out", default=None, help="write a weather: YAML fragment here")
    args = ap.parse_args()

    end = datetime.now(UTC).date() - timedelta(days=3)  # NCEI lags a few days
    start = end - timedelta(days=args.days)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    codes = [c.strip() for c in args.stations.split(",") if c.strip()]
    fits: list[StationFit] = []
    with httpx.Client(timeout=60, headers={"User-Agent": UA}, trust_env=False) as client:
        for code in codes:
            if code not in STATIONS or code not in GHCN_IDS:
                print(f"{code}: unknown station, skipped")
                continue
            try:
                fc = fetch_forecasts(client, code, models, start, end, args.kind)
                actual, name = fetch_actuals(client, code, start, end, args.kind)
            except Exception as e:
                print(f"{code}: fetch failed: {e!r}")
                continue
            full = fit_station(code, actual, fc)
            cut = (end - timedelta(days=args.recent_days)).isoformat()
            recent = fit_station(
                code, {d: v for d, v in actual.items() if d >= cut},
                {m: {d: v for d, v in f_.items() if d >= cut} for m, f_ in fc.items()},
            )  # fmt: skip
            use = recent if recent.n >= 60 else full
            fits.append(use)
            print(f"\n== {code} {STATIONS[code].name}  NCEI station: {name}")
            for label, f in ((f"last {args.days}d", full), (f"last {args.recent_days}d", recent)):
                per_model = "  ".join(
                    f"{model_key(m) or m}={b:+.2f}" for m, (_, b, _) in f.by_model.items()
                )
                print(f"   {label:<10} days={f.n:<4} bias={f.bias_f:+.1f}F  mae={f.mae}  "
                      f"resid_sd={f.resid_sd}  sigma_f={f.sigma_f}  {per_model}")  # fmt: skip
            print(f"   -> using the {'recent' if use is recent else 'full'} window: "
                  f"bias_f={use.bias_f} {use.bias_by_key()} sigma_f={use.sigma_f}")  # fmt: skip
            if not use.usable:
                print("   (not enough days to use)")

    pairs = offsets_from_ledger(Path(args.ledger))
    offsets = offset_table(pairs, DEFAULT_OBS_OFFSETS) if pairs else None
    print(f"\n== observation offsets: {len(pairs)} (hourly, official) pairs in the ledger")
    if offsets:
        print("   " + "  ".join(f"d={d:+d}: {p:.3f}" for d, p in sorted(offsets.items())))
    else:
        print("   none yet (needs days with a final CLI in the shadow log); prior kept")

    live: dict[str, LiveBias] = {
        code: live_bias(code, prs) for code, prs in live_bias_from_ledger(Path(args.ledger)).items()
    }
    print("\n== LIVE ensemble bias from the shadow log (raw member p50 day-ahead vs settled)")
    if live:
        for code, lb in sorted(live.items()):
            flag = "" if lb.usable else f"  (needs {MIN_DAYS_LIVE_BIAS} days; not used yet)"
            print(f"   {code}: n={lb.n} bias={lb.bias_f:+.1f}F mae={lb.mae}{flag}")
    else:
        print("   none yet (needs settled days with a day-ahead v2 snapshot)")

    text = calibration_yaml(fits, offsets, live, gfs_bias=args.gfs_bias)
    print("\n" + text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
