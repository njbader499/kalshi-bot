"""Adverse-selection marks: where the REAL market mid is 5, 30 and 60 minutes after each
fill (real fills on the trading venue, and prod-paper fills).

A maker's hidden cost is being filled right before the price moves against it. Per fill the
ledger keeps the prod mid at the fill and at the three horizons; `Ledger.adverse_selection`
reports the mean move against us per horizon, which the daily summary prints. Marks that
could not be taken within a few minutes of their due time (bot down, API down) are stored
as "na" so they stop being pending.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal

from .ledger import Ledger
from .rest import KalshiREST

log = logging.getLogger(__name__)

HORIZONS = (("mid_5m", 300), ("mid_30m", 1800), ("mid_60m", 3600))
GRACE_SECONDS = 240  # a due mark older than this is recorded as "na"


class MarkTracker:
    def __init__(
        self, prod: KalshiREST, ledger: Ledger, clock: Callable[[], float] = time.time
    ) -> None:
        self.prod = prod
        self.ledger = ledger
        self._clock = clock

    async def register(
        self,
        key: str,
        kind: str,
        ticker: str,
        side: str,
        price: Decimal,
        filled_at: float,
        mid_at_fill: Decimal | None,
    ) -> None:
        await self.ledger.upsert_fill_mark(
            key, kind, ticker, side, str(price), filled_at,
            mid_at_fill=None if mid_at_fill is None else str(mid_at_fill),
        )  # fmt: skip

    async def _mid(self, ticker: str) -> Decimal | None:
        ob = await self.prod.get_orderbook(ticker)
        b, nb = ob.best_yes_bid, ob.best_no_bid
        if b is None or nb is None:
            return None
        a = 1 - nb
        return (b + a) / 2 if a > b else None

    async def tick(self) -> int:
        """Take every due mark. Returns how many were written."""
        now = self._clock()
        pending = await self.ledger.pending_fill_marks(now - HORIZONS[0][1])
        mids: dict[str, Decimal | None] = {}
        n = 0
        for key, ticker, _side, filled_at, have in pending:
            for col, h in HORIZONS:
                if have.get(col) is not None:
                    continue
                due = filled_at + h
                if now < due:
                    continue
                if now - due > GRACE_SECONDS:
                    await self.ledger.upsert_fill_mark(
                        key, "", ticker, _side, "", filled_at, **{col: "na"}
                    )
                    n += 1
                    continue
                if ticker not in mids:
                    try:
                        mids[ticker] = await self._mid(ticker)
                    except Exception as e:
                        log.warning("mark fetch failed", extra={"ticker": ticker, "err": repr(e)})
                        mids[ticker] = None
                m = mids[ticker]
                if m is not None:
                    await self.ledger.upsert_fill_mark(
                        key, "", ticker, _side, "", filled_at, **{col: str(m)}
                    )
                    n += 1
        return n
