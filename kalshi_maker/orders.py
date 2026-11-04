"""Order manager: diff desired quotes against live bot orders, send the minimal changes.

Per market, each loop:
  * nothing wanted (or quoting not allowed)  -> cancel every bot order there
  * wanted, none resting                     -> create (after RiskManager.check_new)
  * wanted, resting on the other side        -> cancel it, create the new one
  * wanted, resting on the same side         -> keep it; amend the price only if it moved
                                                >= hysteresis_ticks AND the amend cooldown
                                                has passed (amends lose queue priority)
  * duplicates                               -> cancel extras
Cancels are always allowed. Creates/amends go through the RiskManager.

Two executors:
  * DryRunExecutor: records what it WOULD do in the ledger (`order_actions`, dry_run=1)
    and simulates resting orders so the diff logic behaves exactly as live. Never touches
    the exchange. Used whenever trading_enabled is false.
  * LiveExecutor: sends V2 orders. It REFUSES to be constructed unless trading_enabled
    is true. That's the second, independent half of the owner's switch; the first is the
    RiskManager.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Protocol

from .expiry import new_expiration
from .ledger import Ledger
from .money import TickGrid
from .order_types import BookSide, OrderRequest, fmt_count, fmt_price, new_client_order_id
from .portfolio import PortfolioState, RestingOrder
from .rest import KalshiREST, OrderOutcomeUnknown
from .risk import OrderIntent, RiskManager
from .settings import TiltCfg, TradingCfg
from .strategy.tilt import DesiredQuote

log = logging.getLogger(__name__)


class TradingDisabled(RuntimeError):
    pass


class Executor(Protocol):
    dry_run: bool

    async def create(self, req: OrderRequest, grid: TickGrid) -> str: ...  # -> order_id
    async def amend(self, o: RestingOrder, price: Decimal, total: Decimal, exp: int) -> None: ...
    async def cancel(self, o: RestingOrder) -> None: ...


class DryRunExecutor:
    dry_run = True

    async def create(self, req: OrderRequest, grid: TickGrid) -> str:
        req.validate(grid, int(time.time()))  # same validation as live
        return f"dry-{uuid.uuid4().hex[:12]}"

    async def amend(self, o: RestingOrder, price: Decimal, total: Decimal, exp: int) -> None:
        return None

    async def cancel(self, o: RestingOrder) -> None:
        return None


class LiveExecutor:
    dry_run = False

    def __init__(self, api: KalshiREST, trading: TradingCfg) -> None:
        if not trading.trading_enabled:
            raise TradingDisabled("LiveExecutor requires trading.trading_enabled: true")
        self.api = api

    async def create(self, req: OrderRequest, grid: TickGrid) -> str:
        r = await self.api.create_order_idempotent(req, grid)
        return str(r["order_id"])

    async def amend(self, o: RestingOrder, price: Decimal, total: Decimal, exp: int) -> None:
        await self.api.amend_order(
            o.order_id, ticker=o.ticker, side=o.side, price=price, count=total,
            expiration_time=exp,
        )  # fmt: skip

    async def cancel(self, o: RestingOrder) -> None:
        await self.api.cancel_order(o.order_id, o.ticker)


class OrderManager:
    def __init__(
        self,
        executor: Executor,
        risk: RiskManager,
        pf: PortfolioState,
        ledger: Ledger | None,
        trading: TradingCfg,
        tilt: TiltCfg,
        expiry_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ex = executor
        self.risk = risk
        self.pf = pf
        self.ledger = ledger
        self.trading = trading
        self.tilt = tilt
        self.expiry_seconds = expiry_seconds
        self._clock = clock
        self.filled: dict[str, Decimal] = {}  # order_id -> filled so far (amend needs total)
        self._last_amend: dict[str, float] = {}
        self.blocked: set[str] = set()  # markets with an order of unknown outcome
        self.creates = 0  # successful creates this session
        self.last_created: DesiredQuote | None = None
        self.refresh_seconds = max(30, expiry_seconds * 2 // 5)  # 240 s for a 600 s expiry

    async def _log(self, action: str, ticker: str, side: str, price: Decimal | None,
                   count: Decimal | None, reason: str) -> None:  # fmt: skip
        if self.ledger is None:
            return
        await self.ledger.record_order_action(
            action, ticker, side, None if price is None else fmt_price(price),
            None if count is None else fmt_count(count), reason, self.ex.dry_run,
        )  # fmt: skip

    # ---- primitives ----
    async def _cancel(self, o: RestingOrder, reason: str) -> None:
        try:
            await self.ex.cancel(o)
        except Exception as e:  # already gone (filled/expired) or transient: reconcile fixes
            log.warning("cancel failed", extra={"order_id": o.order_id, "err": repr(e)})
            self.risk.on_api_error()
            await self._log("cancel_failed", o.ticker, o.side, o.price, o.remaining, repr(e))
            return
        self.risk.on_api_ok()
        self.pf.remove_resting(o.order_id)
        await self._log("cancel", o.ticker, o.side, o.price, o.remaining, reason)

    async def _create(self, q: DesiredQuote, grid: TickGrid) -> None:
        d = self.risk.check_new(OrderIntent(q.ticker, q.side, q.price, q.count), self.pf)
        if not d.ok:
            await self._log("rejected", q.ticker, q.side, q.price, q.count, d.reason)
            return
        coid = new_client_order_id()
        exp = new_expiration(self._clock(), self.expiry_seconds)
        req = OrderRequest(q.ticker, BookSide(q.side), q.price, q.count, coid, exp)
        if self.ledger is not None and not self.ex.dry_run:  # persist BEFORE sending
            await self.ledger.record_order_intent(
                coid, q.ticker, q.side, fmt_price(q.price), fmt_count(q.count), exp
            )
        self.risk.record_sent()
        try:
            oid = await self.ex.create(req, grid)
        except OrderOutcomeUnknown as e:
            self.blocked.add(q.ticker)  # may be live; reconcile/expiry resolves it
            self.risk.on_api_error()
            await self._log("create_unknown", q.ticker, q.side, q.price, q.count, repr(e))
            return
        except Exception as e:
            self.risk.on_api_error()
            await self._log("create_failed", q.ticker, q.side, q.price, q.count, repr(e))
            return
        self.risk.on_api_ok()
        self.pf.add_resting(RestingOrder(oid, coid, q.ticker, q.side, q.price, q.count, exp))
        self.filled[oid] = Decimal(0)
        self.creates += 1
        self.last_created = q
        await self._log("create", q.ticker, q.side, q.price, q.count, q.reason)

    async def _amend(self, o: RestingOrder, q: DesiredQuote) -> None:
        now = self._clock()
        if now - self._last_amend.get(o.order_id, 0) < self.trading.min_seconds_between_amends:
            return
        d = self.risk.check_amend(self.pf, o.order_id, q.price, q.count)
        if not d.ok:
            await self._log("rejected_amend", o.ticker, o.side, q.price, q.count, d.reason)
            return
        total = self.filled.get(o.order_id, Decimal(0)) + q.count
        exp = new_expiration(now, self.expiry_seconds)
        self.risk.record_sent()
        try:
            await self.ex.amend(o, q.price, total, exp)
        except Exception as e:
            self.risk.on_api_error()
            await self._log("amend_failed", o.ticker, o.side, q.price, q.count, repr(e))
            return
        self.risk.on_api_ok()
        self._last_amend[o.order_id] = now
        await self._log("amend", o.ticker, o.side, q.price, q.count, f"from {o.price}")
        o.price, o.remaining, o.expiration_time = q.price, q.count, exp

    # ---- the diff ----
    async def sync(
        self,
        desired: Mapping[str, DesiredQuote | None],
        grids: Mapping[str, TickGrid],
        can_quote: bool,
    ) -> None:
        tickers = set(desired) | self.pf.quoted_markets()
        for t in sorted(tickers):
            live = self.pf.resting_for(t)
            want = desired.get(t) if can_quote and t not in self.blocked else None
            if want is None:
                for o in live:
                    await self._cancel(o, "no longer wanted" if can_quote else "quoting disabled")
                continue
            same = [o for o in live if o.side == want.side]
            for o in [o for o in live if o.side != want.side] + same[1:]:
                await self._cancel(o, "wrong side / duplicate")
            grid = grids.get(t)
            if grid is None:
                for o in same[:1]:
                    await self._cancel(o, "no tick grid")
                continue
            if not same:
                await self._create(want, grid)
                continue
            o = same[0]
            tick = grid.tick_at(o.price)
            moved = abs(want.price - o.price)
            if moved >= tick * max(1, self.tilt.hysteresis_ticks) or o.remaining != want.count:
                await self._amend(o, want)

    async def cancel_all(self, reason: str) -> int:
        n = 0
        for o in list(self.pf.resting.values()):
            await self._cancel(o, reason)
            n += 1
        return n

    def on_fill(self, msg: Mapping[str, Any]) -> None:
        """WS `fill` -> POSITIONS only. Resting size and filled count come from the
        `user_order` messages (absolute values), so out-of-order delivery can't
        double-count. Until that update arrives, exposure is over- not under-stated."""
        count = Decimal(str(msg.get("count_fp") or "0"))
        price = Decimal(str(msg.get("yes_price_dollars") or "0"))
        side = str(msg.get("book_side") or "")
        fee = Decimal(str(msg.get("fee_cost") or "0"))
        if side not in ("bid", "ask") or count <= 0:
            return
        self.pf.apply_fill(str(msg.get("market_ticker") or ""), side, price, count, fee)

    def on_user_order(self, msg: Mapping[str, Any]) -> str | None:
        """WS `user_order` (verified fields: order_id, status resting|canceled|executed,
        remaining_count_fp, fill_count_fp, last_update_reason). Returns a note when a
        tracked order left the book."""
        oid = str(msg.get("order_id") or "")
        o = self.pf.resting.get(oid)
        if o is None:
            return None
        if msg.get("fill_count_fp") not in (None, ""):
            self.filled[oid] = Decimal(str(msg["fill_count_fp"]))
        status = str(msg.get("status") or "")
        if status in ("canceled", "executed"):
            self.pf.remove_resting(oid)
            return f"{o.ticker} {o.side} order {status} ({msg.get('last_update_reason') or '-'})"
        if msg.get("remaining_count_fp") not in (None, ""):
            o.remaining = Decimal(str(msg["remaining_count_fp"]))
            if o.remaining <= 0:
                self.pf.remove_resting(oid)
        return None

    async def refresh_expiries(self) -> int:
        """Expiry-only amends (same price + total count: keeps queue priority) for orders
        with less than expiry - refresh_seconds left. Never extends anything if trading
        is not allowed right now: orders then die on their own (dead-man behaviour)."""
        if not self.risk.can_trade().ok:
            return 0
        now = self._clock()
        threshold = self.expiry_seconds - self.refresh_seconds
        n = 0
        for o in list(self.pf.resting.values()):
            if o.expiration_time - now > threshold:
                continue
            exp = new_expiration(now, self.expiry_seconds)
            total = self.filled.get(o.order_id, Decimal(0)) + o.remaining
            self.risk.record_sent()
            try:
                await self.ex.amend(o, o.price, total, exp)
            except Exception as e:
                self.risk.on_api_error()
                await self._log(
                    "expiry_refresh_failed", o.ticker, o.side, o.price, o.remaining, repr(e)
                )
                continue
            self.risk.on_api_ok()
            o.expiration_time = exp
            n += 1
        return n
