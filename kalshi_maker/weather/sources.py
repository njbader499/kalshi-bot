"""Weather data clients (async, cached). Field names verified live 2026-11-04.

  * Open-Meteo ensemble: GET https://ensemble-api.open-meteo.com/v1/ensemble
      ?latitude&longitude&hourly=temperature_2m&models=gfs_seamless,ecmwf_ifs025
      &temperature_unit=fahrenheit&timezone=GMT&past_days=1&forecast_days=N
    -> hourly.time ["2026-11-03T00:00", ...] (UTC), plus one series per member:
       temperature_2m_ncep_gefs_seamless (control), temperature_2m_memberNN_ncep_gefs_seamless
       (30), temperature_2m_ecmwf_ifs025_ensemble (control), ..._memberNN_ecmwf_ifs025_ensemble
       (50). 82 series in total.
  * NWS: /points/{lat},{lon} -> properties.forecastHourly and properties.forecast; either
    URL -> properties.periods[] {startTime (ISO with offset), temperature (int),
    temperatureUnit "F"}; the daily product's periods also carry isDaytime (True = the
    period whose temperature is the day's forecast HIGH, False = the night's LOW).
  * NWS observations: /stations/{id}/observations?start&end -> features[].properties
    {timestamp, temperature {value °C, qualityControl}}.
  * NWS CLI: /products/types/CLI/locations/{loc} -> @graph[] {@id, issuanceTime};
    each @id -> {productText}.

api.weather.gov rejects requests without a User-Agent (403), so one is always sent.
No proxies: trust_env=False.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .cli_reports import CliParseError, CliReport, parse_cli
from .stations import Station

log = logging.getLogger(__name__)

UA = "kalshi-maker/0.1 (personal research bot; contact via repo owner)"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS = "https://api.weather.gov"
BAD_QC = frozenset({"X", "Q"})  # NWS QC flags: X = rejected, Q = questioned


@dataclass
class EnsembleData:
    times: list[datetime]
    members: dict[str, list[float | None]]
    fetched_at: datetime


@dataclass
class _Cache:
    ttl: float
    store: dict[Any, tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: Any, now: float) -> Any:
        hit = self.store.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        return None

    def put(self, key: Any, value: Any, now: float) -> None:
        self.store[key] = (now, value)


def _utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class WeatherSources:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        forecast_days: int = 3,
    ) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=30, headers={"User-Agent": UA, "Accept": "application/geo+json"},
            trust_env=False,
        )  # fmt: skip
        self._clock = clock
        self.forecast_days = forecast_days
        self._ens = _Cache(1800)
        self._nws = _Cache(1800)
        self._points = _Cache(86400 * 7)
        self._obs = _Cache(300)
        self._cli_list = _Cache(900)
        self._cli_products: dict[str, CliReport | None] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = await self.client.get(url, params=params, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.json()

    # ---- ensemble ----
    async def ensemble(self, st: Station) -> EnsembleData:
        now = self._clock()
        hit = self._ens.get(st.cli_code, now)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        d = await self._get(
            ENSEMBLE_URL,
            {
                "latitude": st.lat,
                "longitude": st.lon,
                "hourly": "temperature_2m",
                "models": "gfs_seamless,ecmwf_ifs025",
                "temperature_unit": "fahrenheit",
                "timezone": "GMT",
                "past_days": 1,
                "forecast_days": self.forecast_days,
            },
        )
        if "hourly" not in d:
            raise ValueError(f"ensemble response without hourly: {str(d)[:200]}")
        h = d["hourly"]
        times = [_utc(t) for t in h["time"]]
        members = {
            k: [None if v is None else float(v) for v in vals]
            for k, vals in h.items()
            if k.startswith("temperature_2m")
        }
        units = d.get("hourly_units") or {}
        bad_units = {u for k, u in units.items() if k.startswith("temperature_2m") and u != "°F"}
        if bad_units:
            raise ValueError(f"ensemble units {bad_units}, expected °F")
        out = EnsembleData(times, members, datetime.now(UTC))
        self._ens.put(st.cli_code, out, now)
        return out

    # ---- NWS hourly forecast ----
    async def _point_urls(self, st: Station) -> dict[str, str]:
        now = self._clock()
        urls = self._points.get(st.cli_code, now)
        if urls is None:
            p = await self._get(f"{NWS}/points/{st.lat:.4f},{st.lon:.4f}")
            props = p["properties"]
            urls = {"hourly": props["forecastHourly"], "daily": props["forecast"]}
            self._points.put(st.cli_code, urls, now)
        return urls  # type: ignore[no-any-return]

    @staticmethod
    def _period_temp_f(per: Mapping[str, Any]) -> float | None:
        t = per.get("temperature")
        if isinstance(t, dict):  # some NWS responses use a quantitative value object
            t = t.get("value")
        if t is None:
            return None
        unit = per.get("temperatureUnit", "F")
        return float(t) if unit == "F" else float(t) * 9 / 5 + 32

    async def nws_hourly(self, st: Station) -> list[tuple[datetime, float]]:
        now = self._clock()
        hit = self._nws.get(st.cli_code, now)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        d = await self._get((await self._point_urls(st))["hourly"])
        out: list[tuple[datetime, float]] = []
        for per in d["properties"]["periods"]:
            tf = self._period_temp_f(per)
            if tf is not None:
                out.append((_utc(per["startTime"]), tf))
        self._nws.put(st.cli_code, out, now)
        return out

    async def nws_daily(self, st: Station) -> dict[date, tuple[float | None, float | None]]:
        """{local calendar date: (forecast high, forecast low)} from the NWS daily product.

        The high is the isDaytime period starting on that date; the low is the night period
        starting on that date (which runs into the next morning, as the NWS defines it)."""
        now = self._clock()
        key = (st.cli_code, "daily")
        hit = self._nws.get(key, now)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        d = await self._get((await self._point_urls(st))["daily"])
        out: dict[date, tuple[float | None, float | None]] = {}
        for per in d["properties"]["periods"]:
            tf = self._period_temp_f(per)
            if tf is None:
                continue
            local_day = datetime.fromisoformat(per["startTime"]).date()
            hi, lo = out.get(local_day, (None, None))
            if per.get("isDaytime", True):
                hi = tf if hi is None else max(hi, tf)
            else:
                lo = tf if lo is None else min(lo, tf)
            out[local_day] = (hi, lo)
        self._nws.put(key, out, now)
        return out

    # ---- observations ----
    async def observations(
        self, st: Station, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """(timestamp, °C) for valid observations in [start, end)."""
        now = self._clock()
        key = (st.obs_station, start, end)
        hit = self._obs.get(key, now)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        d = await self._get(
            f"{NWS}/stations/{st.obs_station}/observations",
            {"start": start.isoformat(), "end": end.isoformat()},
        )
        out: list[tuple[datetime, float]] = []
        for f in d.get("features") or []:
            p = f.get("properties") or {}
            t = p.get("temperature") or {}
            v = t.get("value")
            if v is None or t.get("qualityControl") in BAD_QC:
                continue
            if not str(t.get("unitCode") or "").endswith("degC"):
                continue
            ts = _utc(p["timestamp"])
            if start <= ts < end:
                out.append((ts, float(v)))
        self._obs.put(key, out, now)
        return out

    # ---- CLI reports ----
    async def cli_reports(self, st: Station) -> list[CliReport]:
        now = self._clock()
        hit = self._cli_list.get(st.cli_location, now)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        d = await self._get(f"{NWS}/products/types/CLI/locations/{st.cli_location}")
        reports: list[CliReport] = []
        for item in d.get("@graph") or []:
            pid = item["@id"]
            if pid not in self._cli_products:
                try:
                    prod = await self._get(pid)
                except httpx.HTTPError as e:
                    # A product can be listed before it is fetchable (404 seen live
                    # 2026-11-04). Skip it this round, don't cache the failure, retry later.
                    log.warning("cli product fetch failed", extra={"id": pid, "err": repr(e)})
                    continue
                try:
                    rep = parse_cli(prod.get("productText") or "", _utc(item["issuanceTime"]), pid)
                    self._cli_products[pid] = rep if rep.code == st.cli_code else None
                except CliParseError:
                    self._cli_products[pid] = None
            rep2 = self._cli_products[pid]
            if rep2 is not None:
                reports.append(rep2)
        self._cli_list.put(st.cli_location, reports, now)
        return reports
