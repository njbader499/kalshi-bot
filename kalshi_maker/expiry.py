"""Exchange-side order expiry: the bot's dead-man switch that needs no laptop.

Every bot order is created with expiration_time = now + order_expiry_seconds (default
10 min). While the bot is alive and the SafetyGate is RUNNING, ExpiryKeeper pushes the
expiry forward with an *expiry-only amend*: the same price and total count plus a new
expiration_time. The docs say this keeps queue position. If the bot dies, the laptop
sleeps, or Wi-Fi drops, refreshes stop and the exchange cancels the quotes within
order_expiry_seconds.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


def new_expiration(now_s: float, expiry_seconds: int) -> int:
    return int(now_s) + int(expiry_seconds)


@dataclass
class LiveOrder:
    order_id: str
    ticker: str
    side: str  # V2 book side
    price: Decimal  # YES price
    total_count: Decimal  # filled + resting (what amend's `count` means)
    expiration_time: int  # unix seconds


AmendFn = Callable[..., Awaitable[dict[str, Any]]]


class ExpiryKeeper:
    def __init__(
        self,
        amend: AmendFn,
        expiry_seconds: int,
        refresh_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if refresh_seconds * 2 > expiry_seconds:
            raise ValueError("refresh_seconds must be <= expiry_seconds / 2")
        self._amend = amend
        self.expiry_seconds = expiry_seconds
        self.refresh_seconds = refresh_seconds
        self._clock = clock

    def due(self, orders: list[LiveOrder]) -> list[LiveOrder]:
        """Orders whose remaining life is below expiry - refresh."""
        now = self._clock()
        threshold = self.expiry_seconds - self.refresh_seconds
        return [o for o in orders if o.expiration_time - now <= threshold]

    async def refresh(self, orders: list[LiveOrder], can_quote: Callable[[], bool]) -> int:
        """Extend expiries of due orders. Stops at once if the gate closes. Returns count."""
        n = 0
        for o in self.due(orders):
            if not can_quote():
                break
            new_exp = new_expiration(self._clock(), self.expiry_seconds)
            try:
                await self._amend(
                    o.order_id,
                    ticker=o.ticker,
                    side=o.side,
                    price=o.price,
                    count=o.total_count,
                    expiration_time=new_exp,
                )
            except Exception as e:  # the order may have filled or been cancelled; reconcile
                log.warning("expiry refresh failed", extra={"order_id": o.order_id, "err": repr(e)})
                continue
            o.expiration_time = new_exp
            n += 1
        return n
