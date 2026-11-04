"""RiskManager: the single gate every order action passes through (Section 7).

Hard rules, checked in this order on every new order or exposure-increasing amend:
  1. trading_enabled (config, owner-controlled). False rejects EVERYTHING.
  2. Kill / drawdown kill / weekly stop / daily stop / API-error pause.
  3. Sanity: price strictly inside (0, 1), count > 0.
  4. Orders per minute (rolling 60 s).
  5. Per-order cost cap.
  6. Max resting orders, max markets quoted.
  7. Worst-case exposure caps: market, event, category, total capital deployed.
Cancels are ALWAYS allowed, even when halted.

Loss stops use an equity proxy = cumulative realized P&L - fees + conservative unrealized
(longs at best bid). Daily and weekly windows use UTC days and ISO weeks.
  * daily loss >= daily_loss_stop   -> halt until the next UTC day (and cancel all)
  * weekly loss >= weekly_loss_stop -> halt until the owner resumes manually
  * drawdown from peak >= max_drawdown -> KILL: manual reset only (`risk-reset`)
State is persisted atomically to <data_dir>/risk_state.json, so a restart can't clear a
halt.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .money import ONE, ZERO
from .portfolio import PortfolioState
from .settings import RiskLimits

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Decision:
    ok: bool
    reason: str = ""


OK = Decision(True)


@dataclass(frozen=True, slots=True)
class OrderIntent:
    ticker: str
    side: str  # bid | ask
    price: Decimal  # YES price
    count: Decimal

    @property
    def unit_cost(self) -> Decimal:
        return self.price if self.side == "bid" else ONE - self.price

    @property
    def cost(self) -> Decimal:
        return self.unit_cost * self.count


@dataclass
class RiskState:
    killed: bool = False
    kill_reason: str = ""
    weekly_halt: bool = False
    daily_halt_day: str = ""  # UTC date the daily stop applies to
    paused_until: float = 0.0
    day_key: str = ""
    day_start: str = "0"
    week_key: str = ""
    week_start: str = "0"
    peak: str = "0"
    events: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        trading_enabled: bool,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limits = limits
        self.trading_enabled = trading_enabled
        self.state_path = state_path
        self._clock = clock
        self._sent: deque[float] = deque()
        self.consecutive_api_errors = 0
        self.state = self._load()

    # ---- persistence ----
    def _load(self) -> RiskState:
        if self.state_path and self.state_path.exists():
            try:
                return RiskState(**json.loads(self.state_path.read_text(encoding="utf-8")))
            except Exception as e:  # corrupt state: fail SAFE (killed), never open
                log.error("risk state unreadable; starting KILLED", extra={"err": repr(e)})
                return RiskState(killed=True, kill_reason=f"risk state unreadable: {e!r}")
        return RiskState()

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _event(self, msg: str) -> str:
        stamp = datetime.fromtimestamp(self._clock(), UTC).isoformat(timespec="seconds")
        self.state.events = [*self.state.events[-49:], f"{stamp} {msg}"]
        log.warning("risk event", extra={"event": msg})
        return msg

    # ---- status ----
    def can_trade(self) -> Decision:
        if not self.trading_enabled:
            return Decision(False, "trading_enabled is false (owner switch)")
        s = self.state
        if s.killed:
            return Decision(False, f"KILLED: {s.kill_reason}")
        if s.weekly_halt:
            return Decision(False, "weekly loss stop: manual resume required")
        today = self._day()
        if s.daily_halt_day == today:
            return Decision(False, "daily loss stop: halted until next UTC day")
        if self._clock() < s.paused_until:
            return Decision(False, "paused after consecutive API errors")
        return OK

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).date().isoformat()

    def _week(self) -> str:
        y, w, _ = datetime.fromtimestamp(self._clock(), UTC).isocalendar()
        return f"{y}-W{w:02d}"

    # ---- the gate ----
    def check_new(self, it: OrderIntent, pf: PortfolioState) -> Decision:
        d = self.can_trade()
        if not d.ok:
            return d
        L = self.limits
        if not (ZERO < it.price < ONE):
            return Decision(False, f"price {it.price} outside (0, 1)")
        if it.count <= 0:
            return Decision(False, "count must be positive")
        now = self._clock()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= L.max_orders_per_minute:
            return Decision(False, f"max_orders_per_minute {L.max_orders_per_minute}")
        if it.cost > L.max_order_cost:
            return Decision(False, f"order cost {it.cost} > max_order_cost {L.max_order_cost}")
        if len(pf.resting) + 1 > L.max_resting_orders:
            return Decision(False, f"max_resting_orders {L.max_resting_orders}")
        quoted = pf.quoted_markets()
        if it.ticker not in quoted and len(quoted) >= L.max_markets_quoted:
            return Decision(False, f"max_markets_quoted {L.max_markets_quoted}")
        try:
            meta = pf.meta[it.ticker]
        except KeyError:
            return Decision(False, f"no event/category metadata for {it.ticker}")
        checks = (
            (pf.market_exposure(it.ticker), L.max_cost_per_market, "market"),
            (pf.event_exposure(meta.event), L.max_cost_per_event, "event"),
            (pf.category_exposure(meta.category), L.max_cost_per_category, "category"),
            (pf.total_exposure(), L.max_capital_deployed, "capital deployed"),
        )
        for cur, cap, name in checks:
            if cur + it.cost > cap:
                return Decision(False, f"{name} exposure {cur} + {it.cost} > cap {cap}")
        return OK

    def check_amend(
        self, pf: PortfolioState, order_id: str, new_price: Decimal, new_remaining: Decimal
    ) -> Decision:
        """An amend is judged as: remove the old order's exposure, add the new one's."""
        old = pf.resting.get(order_id)
        if old is None:
            return Decision(False, f"unknown order {order_id}")
        it = OrderIntent(old.ticker, old.side, new_price, new_remaining)
        if it.cost <= old.cost:
            d = self.can_trade()  # reducing exposure: allowed whenever trading is allowed
            if not d.ok:
                return d
            return OK if ZERO < new_price < ONE else Decision(False, "bad price")
        saved = pf.resting.pop(order_id)
        try:
            d = self.check_new(it, pf)
        finally:
            pf.resting[order_id] = saved
        return d

    def record_sent(self) -> None:
        self._sent.append(self._clock())

    # ---- API health ----
    def on_api_error(self) -> str | None:
        self.consecutive_api_errors += 1
        if self.consecutive_api_errors >= self.limits.max_consecutive_api_errors:
            self.consecutive_api_errors = 0
            self.state.paused_until = self._clock() + self.limits.api_error_pause_seconds
            self._save()
            return self._event(
                f"{self.limits.max_consecutive_api_errors} consecutive API errors: pausing "
                f"{self.limits.api_error_pause_seconds}s"
            )
        return None

    def on_api_ok(self) -> None:
        self.consecutive_api_errors = 0

    # ---- P&L stops ----
    def update_equity(self, equity: Decimal) -> list[str]:
        """Feed the equity proxy. Returns new risk events (each means: cancel all now)."""
        s, L, out = self.state, self.limits, []
        day, week = self._day(), self._week()
        if s.day_key != day:
            s.day_key, s.day_start = day, str(equity)
        if s.week_key != week:
            s.week_key, s.week_start = week, str(equity)
        peak = max(Decimal(s.peak), equity)
        s.peak = str(peak)
        if not s.killed and peak - equity >= L.max_drawdown:
            s.killed = True
            s.kill_reason = f"drawdown {peak - equity} >= {L.max_drawdown}"
            out.append(self._event("KILL: " + s.kill_reason))
        if not s.weekly_halt and Decimal(s.week_start) - equity >= L.weekly_loss_stop:
            s.weekly_halt = True
            out.append(self._event(f"weekly loss stop hit ({Decimal(s.week_start) - equity})"))
        if s.daily_halt_day != day and Decimal(s.day_start) - equity >= L.daily_loss_stop:
            s.daily_halt_day = day
            out.append(self._event(f"daily loss stop hit ({Decimal(s.day_start) - equity})"))
        self._save()
        return out

    # ---- manual controls ----
    def kill(self, reason: str) -> str:
        self.state.killed = True
        self.state.kill_reason = reason
        msg = self._event(f"KILL: {reason}")
        self._save()
        return msg

    def resume_weekly(self) -> str:
        """Owner-confirmed resume after a weekly stop (Telegram asks for confirmation)."""
        self.state.weekly_halt = False
        msg = self._event("weekly halt cleared by owner")
        self._save()
        return msg

    def reset_kill(self) -> str:
        """Only from the local CLI (`risk-reset`), never from Telegram."""
        self.state.killed = False
        self.state.kill_reason = ""
        # Re-base the peak on the next equity reading; resetting it to 0 would re-trigger the
        # kill immediately after a real drawdown (equity proxy is negative then).
        self.state.peak = "-Infinity"
        msg = self._event("KILL cleared by owner (local CLI)")
        self._save()
        return msg
