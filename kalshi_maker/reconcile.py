"""Reconciliation against the exchange (the source of truth).

Runs on every start and after every safety-gate trip, AFTER bot orders are cancelled:
  1. balance snapshot
  2. unsettled positions snapshot
  3. resting orders: count bot vs manual. A bot order still resting at this point means
     the cancel step failed, so raise, and the gate retries the whole recovery.
     Manual orders are counted and never touched.
  4. fills since the last recorded fill (with overlap) -> ledger (dedup by trade_id)
  5. settlements over the same window -> ledger
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .ledger import Ledger
from .order_types import is_bot_order

log = logging.getLogger(__name__)

FILL_OVERLAP_S = 3600
FIRST_RUN_LOOKBACK_S = 30 * 86400


class ReconcileError(Exception):
    pass


@dataclass
class ReconcileReport:
    balance: str = ""
    positions: int = 0
    nonzero_positions: dict[str, str] = field(default_factory=dict)
    positions_raw: list[dict[str, Any]] = field(default_factory=list)
    bot_resting: int = 0
    manual_resting: int = 0
    new_fills: int = 0
    seen_fills: int = 0
    new_settlements: int = 0
    took_s: float = 0.0


async def reconcile(api: Any, ledger: Ledger, now: float | None = None) -> ReconcileReport:
    t0 = time.monotonic()
    now = now if now is not None else time.time()
    rep = ReconcileReport()

    bal = await api.get_balance()
    rep.balance = str(bal.balance)
    pv = None if bal.portfolio_value is None else str(bal.portfolio_value)
    await ledger.snapshot_balance(bal.raw, str(bal.balance), pv)

    positions = [p async for p in api.iter_positions(settlement_status="unsettled", limit=1000)]
    rep.positions = await ledger.snapshot_positions(positions)
    rep.positions_raw = [dict(p) for p in positions]
    for p in positions:
        pos = str(p.get("position_fp") or "0")
        if pos not in ("0", "0.00"):
            rep.nonzero_positions[str(p.get("ticker"))] = pos

    async for o in api.iter_orders(status="resting", limit=1000):
        if is_bot_order(o.get("client_order_id")):
            rep.bot_resting += 1
        else:
            rep.manual_resting += 1
    if rep.bot_resting:
        raise ReconcileError(f"{rep.bot_resting} bot order(s) still resting after cancel")

    last_ms = await ledger.latest_fill_ts_ms()
    min_ts = (
        int(last_ms / 1000) - FILL_OVERLAP_S
        if last_ms is not None
        else int(now) - FIRST_RUN_LOOKBACK_S
    )
    async for f in api.iter_fills(min_ts=min_ts, limit=1000):
        rep.seen_fills += 1
        if await ledger.record_fill(f, source="rest"):
            rep.new_fills += 1

    async for s in api.paginate(
        "/portfolio/settlements", "settlements", {"min_ts": min_ts, "limit": 1000}, auth=True
    ):
        if await ledger.record_settlement(s):
            rep.new_settlements += 1

    rep.took_s = round(time.monotonic() - t0, 2)
    log.info(
        "reconciled",
        extra={"report": {k: v for k, v in rep.__dict__.items() if k != "positions_raw"}},
    )
    return rep
