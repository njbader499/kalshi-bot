"""Laptop safety: detect sleep/wake, loop stalls, wall-clock jumps and network drops, and
gate quoting until a full recovery has run.

Policy (owner's instruction, 2026-11-04):
  * Any of: sleep/wake, a Wi-Fi/network drop, or a wall-clock jump > clock_jump_seconds
    => stop quoting immediately. As soon as the exchange is reachable again: cancel ALL bot
    orders, re-snapshot order books, reconcile with the exchange, and only then resume.
  * Every resting order carries an exchange-side expiration (order_expiry_seconds), so a
    laptop that never wakes still has its quotes die on their own.

Detection:
  * tick() is called from the main loop about once a second. A monotonic gap larger than
    loop_stall_seconds means the process was frozen (sleep, debugger or a hung loop). On
    Windows time.monotonic() (GetTickCount64) keeps counting through sleep, so a suspend
    shows up as a gap. QueryUnbiasedInterruptTime, which stops during sleep, tells sleep
    apart from a stall.
  * |Δwall - Δmonotonic| > clock_jump_seconds => the wall clock was stepped (NTP correction,
    manual change, or a resync after a long sleep).
  * Network: the REST/WS layers call network_down() / network_up().
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)


class Cause(StrEnum):
    SUSPEND = "suspend"
    STALL = "stall"
    CLOCK_JUMP = "clock_jump"
    NETWORK = "network"
    STARTUP = "startup"


@dataclass(frozen=True, slots=True)
class Discontinuity:
    cause: Cause
    detail: str


def _unbiased_probe() -> Callable[[], float] | None:
    """Seconds of system uptime excluding sleep (Windows), else None."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel32.QueryUnbiasedInterruptTime
    fn.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
    fn.restype = ctypes.c_int

    def probe() -> float:
        v = ctypes.c_ulonglong()
        if not fn(ctypes.byref(v)):
            raise OSError("QueryUnbiasedInterruptTime failed")
        return v.value / 1e7  # 100 ns units

    return probe


class ContinuityMonitor:
    def __init__(
        self,
        clock_jump_seconds: float,
        stall_seconds: float,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        unbiased: Callable[[], float] | bool | None = True,
    ) -> None:
        self.clock_jump_seconds = clock_jump_seconds
        self.stall_seconds = stall_seconds
        self._wall = wall
        self._mono = mono
        self._unbiased = _unbiased_probe() if unbiased is True else (unbiased or None)
        self._last_wall = wall()
        self._last_mono = mono()
        self._last_unbiased = self._unbiased() if self._unbiased else None

    def tick(self) -> list[Discontinuity]:
        w, m = self._wall(), self._mono()
        u = self._unbiased() if self._unbiased else None
        dw, dm = w - self._last_wall, m - self._last_mono
        out: list[Discontinuity] = []
        if dm > self.stall_seconds:
            slept = None
            if u is not None and self._last_unbiased is not None:
                slept = dm - (u - self._last_unbiased)
            if slept is not None and slept > self.stall_seconds / 2:
                out.append(Discontinuity(Cause.SUSPEND, f"system suspended ~{slept:.0f}s"))
            else:
                out.append(Discontinuity(Cause.STALL, f"loop frozen {dm:.1f}s"))
        if abs(dw - dm) > self.clock_jump_seconds:
            out.append(Discontinuity(Cause.CLOCK_JUMP, f"wall clock moved {dw - dm:+.1f}s"))
        self._last_wall, self._last_mono, self._last_unbiased = w, m, u
        return out


class GateState(StrEnum):
    NEEDS_RECOVERY = "needs_recovery"
    RECOVERING = "recovering"
    RUNNING = "running"


@dataclass
class RecoverySteps:
    """Callbacks run in order during recovery. Each must raise on failure."""

    reachable: Callable[[], Awaitable[None]]  # e.g. GET /exchange/status
    cancel_bot_orders: Callable[[], Awaitable[object]]
    resnapshot: Callable[[], Awaitable[None]]
    reconcile: Callable[[], Awaitable[None]]


class SafetyGate:
    """Quoting is allowed only in RUNNING. Starts in NEEDS_RECOVERY, so a fresh start runs
    the same cancel/snapshot/reconcile sequence as a wake from sleep."""

    def __init__(
        self,
        steps: RecoverySteps,
        retry_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.steps = steps
        self.retry_seconds = retry_seconds
        self._sleep = sleep
        self._on_event = on_event or (lambda kind, msg: None)
        self.state = GateState.NEEDS_RECOVERY
        self.causes: list[Discontinuity] = [Discontinuity(Cause.STARTUP, "fresh start")]
        self.generation = 0  # bumps on every trip; order code can drop stale work

    @property
    def can_quote(self) -> bool:
        return self.state is GateState.RUNNING

    def trip(self, d: Discontinuity) -> None:
        """Stop quoting now. Recovery runs on the next recover() call."""
        if self.state is GateState.RUNNING:
            log.warning("safety gate tripped", extra={"cause": d.cause.value, "detail": d.detail})
            self._on_event("risk", f"quoting paused: {d.cause.value} ({d.detail})")
        self.state = GateState.NEEDS_RECOVERY
        self.causes.append(d)
        self.generation += 1

    async def recover(self, max_attempts: int | None = None) -> bool:
        """Run recovery until it succeeds (or max_attempts). True when RUNNING again."""
        attempt = 0
        while self.state is not GateState.RUNNING:
            attempt += 1
            gen = self.generation
            self.state = GateState.RECOVERING
            try:
                await self.steps.reachable()
                await self.steps.cancel_bot_orders()
                await self.steps.resnapshot()
                await self.steps.reconcile()
            except Exception as e:
                log.warning("recovery step failed", extra={"err": repr(e), "attempt": attempt})
                self.state = GateState.NEEDS_RECOVERY
                if max_attempts is not None and attempt >= max_attempts:
                    return False
                await self._sleep(self.retry_seconds)
                continue
            if self.generation != gen:  # tripped again mid-recovery: go round again
                self.state = GateState.NEEDS_RECOVERY
                continue
            causes = ", ".join(c.cause.value for c in self.causes)
            self.causes.clear()
            self.state = GateState.RUNNING
            log.info("safety gate recovered", extra={"after": causes})
            self._on_event("info", f"quoting resumed after: {causes}")
        return True


def describe_clock_offset(lower: float, upper: float, threshold: float) -> str | None:
    """Warning text if local clock drift is certainly beyond threshold, else None."""
    if lower > threshold:
        return f"local clock is at least {lower:.2f}s BEHIND the exchange (warn > {threshold}s)"
    if upper < -threshold:
        return f"local clock is at least {-upper:.2f}s AHEAD of the exchange (warn > {threshold}s)"
    return None
