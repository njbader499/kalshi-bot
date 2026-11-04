"""Market discovery and the Section 6 hard exclusions.

`exclusion_reason` returns a human-readable reason, or None when a market is allowed. It is
applied to every market before the bot watches or quotes it. Multivariate/combo markets
are caught three ways (ticker regex, `mve_*` fields, `market_type`), because one missed
combo was a real blowup cause.

Fields used (seen live 2026-11-04 on GET /markets and GET /series): ticker, event_ticker,
status, market_type, close_time, mve_collection_ticker, mve_selected_legs, price_ranges;
series: category, fee_type, fee_multiplier, settlement_sources.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .fees import MAKER_FEE_FACTOR
from .settings import Exclusions, Fees

ALLOWED_STATUSES = frozenset({"open", "active"})


def parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def exclusion_reason(
    market: Mapping[str, Any],
    series: Mapping[str, Any] | None,
    excl: Exclusions,
    fees: Fees,
    now: datetime,
) -> str | None:
    ticker = str(market.get("ticker") or "")
    if not ticker:
        return "no ticker"
    for pat in excl.banned_ticker_patterns:
        if re.search(pat, ticker, re.IGNORECASE):
            return f"ticker matches banned pattern {pat!r}"
    if market.get("mve_collection_ticker") or market.get("mve_selected_legs"):
        return "multivariate/combo market"
    if str(market.get("market_type") or "binary") != "binary":
        return f"market_type {market.get('market_type')!r} is not binary"
    status = str(market.get("status") or "")
    if status not in ALLOWED_STATUSES:
        return f"status {status!r}"
    if series is None:
        return "series metadata unknown (category/fees unverifiable)"
    banned = {c.lower() for c in excl.banned_categories}
    cats = {str(c).lower() for c in (series.get("categories") or [])}
    cats.add(str(series.get("category") or "").lower())
    hit = sorted(cats & banned)
    if hit:
        return f"banned category {hit[0]!r}"
    fee_type = series.get("fee_type")
    if fee_type not in MAKER_FEE_FACTOR:
        return f"unknown fee_type {fee_type!r}"
    if MAKER_FEE_FACTOR[fee_type] > 0 and series.get("ticker") not in fees.allowed_maker_fee_series:
        return f"series charges maker fees ({fee_type})"
    close = parse_ts(market.get("close_time"))
    if close is None:
        return "no close_time"
    if close - now < timedelta(minutes=excl.min_time_to_close_minutes):
        return f"closes in under {excl.min_time_to_close_minutes} min"
    if not market.get("price_ranges"):
        return "no price_ranges (tick grid unknown)"
    return None


async def discover_series_markets(
    api: Any,
    series_tickers: list[str],
    excl: Exclusions,
    fees: Fees,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Open, allowed markets in the given series. Returns (markets, {ticker: reason})."""
    now = now or datetime.now(UTC)
    allowed: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for st in series_tickers:
        series = await api.get_series(st)
        async for m in api.iter_markets(series_ticker=st, status="open", limit=200):
            why = exclusion_reason(m, series, excl, fees, now)
            if why:
                rejected[m.get("ticker", "?")] = why
            else:
                allowed.append(m)
    return allowed, rejected
