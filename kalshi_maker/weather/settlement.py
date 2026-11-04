"""Auto-flag disagreements between Kalshi's settlement and the NWS CLI proxy.

Owner decision (2026-11-04): the engine uses the NWS CLI report as the proxy for The
Weather Company's number. This module checks that assumption on every finalized market:

  * Kalshi side: GET /markets?series_ticker=&status=settled -> each market's `result`
    ("yes"/"no") and `expiration_value` (the settled temperature, e.g. "68.00";
    seen live 2026-11-04).
  * NWS side: the FINAL CLI report for the same station and climate day.

Flags (written to ledger `errors` as kind "settlement_mismatch"):
  * expiration_value != CLI value
  * expiration_value is not a whole number (breaks the integer-°F assumption)
  * result disagrees with what the CLI value implies under our strike semantics
Markets whose CLI report has aged out of the NWS listing (about 7 days), or whose
`expiration_value` is empty, are skipped and counted, not flagged.

Use PROD public market data for this check (read-only, no auth): demo's finalized markets
carry an empty `expiration_value` (seen 2026-11-04), so demo cannot verify anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import RulesError, parse_contract
from .sources import WeatherSources


@dataclass
class SettlementCheck:
    checked: int = 0
    agreed: int = 0
    skipped_no_cli: int = 0
    skipped_no_value: int = 0  # Kalshi hasn't published expiration_value (always so on demo)
    unparsed: int = 0
    mismatches: list[str] = field(default_factory=list)


async def check_settlements(
    api: Any, sources: WeatherSources, series: list[str], days: int = 8, now_s: float | None = None
) -> SettlementCheck:
    """Check markets settled in the last `days` (GET /markets min_settled_ts, verified param)."""
    since = int((now_s if now_s is not None else time.time()) - days * 86400)
    res = SettlementCheck()
    for s in series:
        async for m in api.iter_markets(
            series_ticker=s, status="settled", min_settled_ts=since, limit=200
        ):
            try:
                c = parse_contract(m)
            except RulesError:
                res.unparsed += 1
                continue
            all_reports = await sources.cli_reports(c.station)
            reports = [r for r in all_reports if r.day == c.day and r.final]
            cli_val = None
            if reports:
                cli_val = reports[-1].max_f if c.kind == "max" else reports[-1].min_f
            if cli_val is None:
                res.skipped_no_cli += 1
                continue
            ev_raw = m.get("expiration_value")
            if ev_raw in (None, ""):
                res.skipped_no_value += 1
                continue
            res.checked += 1
            problems: list[str] = []
            try:
                ev = Decimal(str(ev_raw))
            except (InvalidOperation, ValueError):
                ev = None
                problems.append(f"expiration_value {ev_raw!r} unreadable")
            if ev is not None:
                if ev != ev.to_integral_value():
                    problems.append(f"expiration_value {ev} is not a whole number")
                if ev != cli_val:
                    problems.append(f"Kalshi value {ev} != CLI {cli_val}")
            implied = "yes" if c.yes(cli_val) else "no"
            if m.get("result") not in ("yes", "no"):
                problems.append(f"unexpected result {m.get('result')!r}")
            elif m.get("result") != implied:
                problems.append(f"result {m.get('result')} but CLI {cli_val} implies {implied}")
            if problems:
                res.mismatches.append(f"{c.ticker} ({c.label}): " + "; ".join(problems))
            else:
                res.agreed += 1
    return res
