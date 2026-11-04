"""Daily P&L summary to Telegram at alerts.daily_summary_time (default 08:00) in
alerts.daily_summary_tz (default Europe/Prague: the owner is in Prague).

  * Sent once per local day. If the bot was down at 08:00, it's sent at the next start.
    The last-sent date and equity are persisted in <data_dir>/daily_summary.json, so a
    restart never re-sends.
  * Equity = exchange balance + portfolio_value (both from GET /portfolio/balance), i.e.
    Kalshi's own valuation. The change is measured against the previous summary.
  * Counts come from the ledger (last 24 h): fills, contracts, fees, live order creates,
    settlements and settlement revenue. Money is summed as Decimal from TEXT.
Numbers are reported as measured. No claim of edge.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .ledger import Ledger


@dataclass
class SummaryState:
    last_date: str = ""
    last_equity: str = ""


def load_state(path: Path) -> SummaryState:
    try:
        return SummaryState(**json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return SummaryState()


def save_state(path: Path, st: SummaryState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st.__dict__), encoding="utf-8")
    os.replace(tmp, path)


def due(now_utc: datetime, at: str, tz: str, last_date: str) -> date | None:
    """The local date to summarise, if it's at/after `at` local time and not yet sent."""
    local = now_utc.astimezone(ZoneInfo(tz))
    hh, mm = (int(x) for x in at.split(":"))
    if local.time() >= time(hh, mm) and last_date != local.date().isoformat():
        return local.date()
    return None


def fmt_money(x: Decimal) -> str:
    return f"${x:,.2f}" if x >= 0 else f"-${-x:,.2f}"


def build_text(
    day: date,
    env: str,
    equity: Decimal,
    balance: Decimal,
    positions_value: Decimal,
    prev_equity: Decimal | None,
    counts: dict[str, Any],
    open_positions: int,
    worst_case_exposure: Decimal,
    deployed_cap: Decimal,
    risk_status: str,
    settlement_check: str,
) -> str:
    change = "n/a (first summary)" if prev_equity is None else fmt_money(equity - prev_equity)
    return "\n".join(
        [
            f"Daily summary {day:%a %d %b %Y} ({env.upper()})",
            f"Equity {fmt_money(equity)} = balance {fmt_money(balance)} "
            f"+ positions {fmt_money(positions_value)}",
            f"Change since last summary: {change}",
            f"Last 24h: {counts['fills']} fill(s), {counts['contracts']} contract(s), "
            f"fees {fmt_money(counts['fees'])}; {counts['creates']} order(s) placed; "
            f"{counts['settlements']} settlement(s), revenue {fmt_money(counts['revenue'])}",
            f"Open positions: {open_positions}; worst-case exposure "
            f"{fmt_money(worst_case_exposure)} of {fmt_money(deployed_cap)} cap",
            f"Risk: {risk_status}",
            f"Settlements vs NWS CLI: {settlement_check}",
        ]
    )


async def ledger_counts(ledger: Ledger, since: float) -> dict[str, Any]:
    db = ledger.db
    async with db.execute(
        "SELECT count, fee_cost FROM fills WHERE recorded_at >= ?", (since,)
    ) as cur:
        rows = [r async for r in cur]
    async with db.execute(
        "SELECT COUNT(*) FROM order_actions "
        "WHERE action='create' AND dry_run=0 AND recorded_at >= ?",
        (since,),
    ) as cur:
        creates = (await cur.fetchone() or [0])[0]
    async with db.execute(
        "SELECT revenue_cents FROM settlements WHERE recorded_at >= ?", (since,)
    ) as cur:
        setl = [r async for r in cur]
    return {
        "fills": len(rows),
        "contracts": sum((Decimal(c) for c, _ in rows), Decimal(0)),
        "fees": sum((Decimal(f) for _, f in rows), Decimal(0)),
        "creates": int(creates),
        "settlements": len(setl),
        "revenue": sum((Decimal(r[0] or 0) for r in setl), Decimal(0)) / 100,
    }
