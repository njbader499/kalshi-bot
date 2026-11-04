"""Phase 3 shadow mode: price weather brackets and log fair value vs Kalshi. NO orders.

Used by `python -m kalshi_maker fair` (one-shot table) and by the runner (every
weather.fair_interval_minutes).

Prices to compare against come from PROD public market data (read-only, unauthenticated):
demo books are thin and not real prices, so calibrating against them would be meaningless.
Demo bid/ask are shown alongside because Phase 4 quotes on demo.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..ledger import Ledger
from ..markets import discover_series_markets
from ..settings import AppConfig
from .contracts import RulesError, WeatherContract, dst_reminder, parse_contract
from .engine import FairValue, WeatherEngine
from .model import ModelParams
from .sources import WeatherSources

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Row:
    contract: WeatherContract
    fair: FairValue | None
    yes_bid: Decimal | None  # prod (real market)
    yes_ask: Decimal | None
    error: str | None = None
    demo_bid: Decimal | None = None
    demo_ask: Decimal | None = None

    @property
    def edge_note(self) -> str:
        if self.fair is None or self.yes_bid is None or self.yes_ask is None:
            return ""
        f = self.fair.p
        if f > self.yes_ask:
            return f"fair > ask by {f - self.yes_ask:.3f}"
        if f < self.yes_bid:
            return f"fair < bid by {self.yes_bid - f:.3f}"
        return "inside"


def make_engine(cfg: AppConfig, sources: WeatherSources) -> WeatherEngine:
    w = cfg.weather
    params = ModelParams(
        sigma_f=w.sigma_f, nws_weight=w.nws_weight, nws_sigma_f=w.nws_sigma_f, bias_f=w.bias_f
    )
    return WeatherEngine(sources, params, dst_rule_rechecked=w.dst_rule_rechecked)


def local_std_today(c: WeatherContract, now: datetime) -> date:
    return now.astimezone(timezone(timedelta(hours=c.station.lst_offset_hours))).date()


async def shadow_rows(
    cfg: AppConfig,
    api: Any,
    engine: WeatherEngine,
    book_for: Callable[[str], Any],
    days: tuple[int, ...] = (0, 1),
    now: datetime | None = None,
    demo_book_for: Callable[[str], Any] | None = None,
) -> list[Row]:
    """Price today's/tomorrow's brackets (station-local climate days) for configured series.

    `book_for(ticker)` returns (yes_bid, yes_ask) or awaitable thereof.
    """
    now = now or datetime.now(UTC)
    rem = dst_reminder(now.date())
    if rem:
        log.warning(rem)
    markets, _rejected = await discover_series_markets(
        api, cfg.markets.weather_series, cfg.exclusions, cfg.fees
    )
    contracts: list[WeatherContract] = []
    errors: dict[str, str] = {}
    for m in markets:
        try:
            c = parse_contract(m)
        except RulesError as e:
            errors[m.get("ticker", "?")] = str(e)
            log.warning("market not priced", extra={"ticker": m.get("ticker"), "why": str(e)})
            continue
        if (c.day - local_std_today(c, now)).days in days:
            contracts.append(c)
    fairs, perr = await engine.price(contracts)
    errors.update(perr)
    rows: list[Row] = []
    for c in sorted(contracts, key=lambda c: (c.series, c.day, c.lo if c.lo is not None else -999)):
        bid, ask = await _maybe_await(book_for(c.ticker))
        dbid = dask = None
        if demo_book_for is not None:
            try:
                dbid, dask = await _maybe_await(demo_book_for(c.ticker))
            except Exception:  # market may not exist on demo
                dbid = dask = None
        rows.append(Row(c, fairs.get(c.ticker), bid, ask, errors.get(c.ticker), dbid, dask))
    return rows


async def _maybe_await(x: Any) -> Any:
    if hasattr(x, "__await__"):
        return await x
    return x


async def log_rows(ledger: Ledger, rows: list[Row]) -> int:
    n = 0
    for r in rows:
        if r.fair is None:
            continue
        await ledger.record_fair_value(
            r.contract.ticker,
            str(r.fair.p),
            str(r.fair.confidence),
            r.fair.source,
            r.fair.as_of.isoformat(),
            None if r.yes_bid is None else str(r.yes_bid),
            None if r.yes_ask is None else str(r.yes_ask),
            detail={
                **r.fair.detail,
                "demo_bid": None if r.demo_bid is None else str(r.demo_bid),
                "demo_ask": None if r.demo_ask is None else str(r.demo_ask),
            },
        )
        n += 1
    return n


def format_table(rows: list[Row], min_conf: float) -> str:
    out: list[str] = []
    hdr = (
        f"{'market':<26} {'bracket':>8} {'fair':>6} {'conf':>5} {'bid':>6} {'ask':>6} "
        f"{'demo b/a':>11}  note (vs prod)"
    )
    cur_event = None
    for r in rows:
        ev = (r.contract.series, r.contract.day)
        if ev != cur_event:
            cur_event = ev
            d = r.fair.detail if r.fair else {}
            extra = _event_summary(d)
            out.append("")
            out.append(f"== {r.contract.series} {r.contract.station.name} "
                       f"{r.contract.day:%a %b %d} (climate day, LST)  {extra}")  # fmt: skip
            out.append(hdr)
        fair = "-" if r.fair is None else f"{r.fair.p:.2f}"
        conf = "-" if r.fair is None else f"{r.fair.confidence:.2f}"
        bid = "-" if r.yes_bid is None else f"{r.yes_bid:.2f}"
        ask = "-" if r.yes_ask is None else f"{r.yes_ask:.2f}"
        demo = f"{_fmt(r.demo_bid)}/{_fmt(r.demo_ask)}"
        note = r.error or r.edge_note
        if r.fair is not None and r.fair.confidence < min_conf:
            note = (note + " | low confidence").strip(" |")
        out.append(
            f"{r.contract.ticker:<26} {r.contract.label:>8} {fair:>6} {conf:>5} "
            f"{bid:>6} {ask:>6} {demo:>11}  {note}"
        )
    return "\n".join(out)


def _fmt(x: Decimal | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def _event_summary(d: Mapping[str, Any]) -> str:
    parts = []
    if "cli_final" in d:
        parts.append(f"CLI final={d['cli_final']}F")
    if "ens_p10_p50_p90" in d:
        parts.append(f"ens p10/50/90={d['ens_p10_p50_p90']} ({d.get('members')} members)")
    if d.get("nws") is not None:
        parts.append(f"NWS={d['nws']:.0f}F")
    if "bound" in d:
        parts.append(f"so far>={d['bound']}F")
    if "mode" in d:
        parts.append(f"mode={d['mode']}F")
    return " | ".join(parts)
