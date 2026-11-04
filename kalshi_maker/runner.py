"""Phase 2 orchestrator: live order books + ledger + laptop safety. Places NO orders.

  python -m kalshi_maker run [--max-seconds N]

Startup, and every safety-gate trip after that, runs the same recovery:
reachable -> cancel bot orders -> re-snapshot books -> reconcile.
Quoting code (Phase 4) will check `gate.can_quote` before every order action.

Stops cleanly on Ctrl+C, a KILL file in the repo root, or --max-seconds. Shutdown always
cancels bot orders (none exist in Phase 2, but the path is exercised).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth import Signer
from .continuity import (
    Cause,
    ContinuityMonitor,
    Discontinuity,
    GateState,
    RecoverySteps,
    SafetyGate,
    describe_clock_offset,
)
from .ledger import Ledger
from .markets import discover_series_markets
from .power import keep_awake
from .reconcile import ReconcileReport, reconcile
from .rest import KalshiREST
from .settings import Settings
from .ws import KalshiWS, WSCallbacks

log = logging.getLogger(__name__)

CLOCK_CHECK_SECONDS = 600
SNAPSHOT_WAIT_SECONDS = 20.0


def say(msg: str) -> None:
    """Operator-facing line on stdout (structured logs go to stderr/file)."""
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')}Z  {msg}", flush=True)


@dataclass
class Runner:
    settings: Settings
    api: KalshiREST
    ledger: Ledger
    signer: Signer
    kill_file: Path
    max_seconds: float | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int = 0

    def __post_init__(self) -> None:
        self.events: asyncio.Queue[tuple[str, Mapping[str, Any]]] = asyncio.Queue(maxsize=10000)
        self.ws: KalshiWS | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[bool] | None = None
        self.last_report: ReconcileReport | None = None
        safety = self.settings.config.safety
        self.monitor = ContinuityMonitor(safety.clock_jump_seconds, safety.loop_stall_seconds)
        self.gate = SafetyGate(
            RecoverySteps(self._reachable, self._cancel, self._resnapshot, self._reconcile),
            on_event=lambda kind, msg: say(f"[{kind}] {msg}"),
        )

    # ---- recovery steps ----
    async def _reachable(self) -> None:
        await self.api.exchange_status()
        if self.ws is None or not self.ws.connected:
            raise ConnectionError("websocket not connected yet")

    async def _cancel(self) -> object:
        res = await self.api.cancel_all_bot_orders()
        if res:
            say(f"cancelled {len(res)} bot order(s)")
        return res

    async def _resnapshot(self) -> None:
        assert self.ws is not None
        await self.ws.request_resnapshot()
        deadline = time.monotonic() + SNAPSHOT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if all(b.valid for b in self.ws.books.values()):
                return
            await asyncio.sleep(0.2)
        bad = [t for t, b in self.ws.books.items() if not b.valid]
        raise TimeoutError(f"no snapshot for {bad}")

    async def _reconcile(self) -> None:
        self.last_report = await reconcile(self.api, self.ledger)
        r = self.last_report
        say(
            f"reconciled: balance ${r.balance}, {len(r.nonzero_positions)} open position(s), "
            f"{r.manual_resting} manual resting order(s) (untouched), "
            f"{r.new_fills} new fill(s), {r.new_settlements} new settlement(s)"
        )

    # ---- websocket wiring ----
    def _callbacks(self) -> WSCallbacks:
        def enqueue(kind: str, m: Mapping[str, Any]) -> None:
            try:
                self.events.put_nowait((kind, m))
            except asyncio.QueueFull:
                log.error("event queue full; dropping (REST reconcile will backfill)")

        def on_conn(up: bool, why: str) -> None:
            if up:
                say("websocket connected")
            else:
                say(f"websocket down: {why}")
                self.gate.trip(Discontinuity(Cause.NETWORK, why))

        def on_gap(why: str) -> None:
            say(f"book resync: {why}")

        return WSCallbacks(
            on_fill=lambda m: enqueue("fill", m),
            on_user_order=lambda m: enqueue("user_order", m),
            on_connection=on_conn,
            on_gap=on_gap,
        )

    async def _start_ws(self, tickers: list[str]) -> None:
        await self._stop_ws()
        self.ws = KalshiWS(self.settings.hosts.ws, self.signer, tickers, self._callbacks())
        self._ws_task = asyncio.create_task(self.ws.run(self.stop), name="ws")

    async def _stop_ws(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
            self._ws_task = None

    async def _drain_events(self) -> None:
        while not self.events.empty():
            kind, m = self.events.get_nowait()
            try:
                if kind == "fill":
                    if await self.ledger.record_fill(m, source="ws"):
                        say(f"FILL {m.get('market_ticker')} {m.get('book_side')} "
                            f"{m.get('count_fp')} @ {m.get('yes_price_dollars')}")  # fmt: skip
                else:
                    await self.ledger.record_order_event(m)
            except Exception as e:
                log.exception("ledger write failed")
                await self.ledger.record_error("ledger_write", repr(e), m)

    # ---- discovery ----
    async def _discover(self) -> list[str]:
        cfg = self.settings.config
        markets, rejected = await discover_series_markets(
            self.api, cfg.markets.weather_series, cfg.exclusions, cfg.fees
        )
        tickers = sorted(m["ticker"] for m in markets)
        say(
            f"watching {len(tickers)} market(s) in {', '.join(cfg.markets.weather_series)}"
            f" ({len(rejected)} excluded)"
        )
        for t, why in sorted(rejected.items())[:10]:
            log.info("excluded", extra={"ticker": t, "reason": why})
        return tickers

    # ---- checks ----
    async def _clock_check(self) -> None:
        thr = self.settings.config.safety.clock_drift_warn_seconds
        try:
            chk = await self.api.check_clock()
        except Exception as e:
            log.warning("clock check failed", extra={"err": repr(e)})
            return
        if chk is None:
            return
        warn = describe_clock_offset(chk.lower, chk.upper, thr)
        if warn:
            say(f"WARNING clock drift: {warn}. Run 'w32tm /resync' (admin).")
        lead = self.ws.stats.clock_lead_lower_bound if self.ws else None
        if lead is not None and lead > thr:
            say(f"WARNING clock drift: exchange is at least {lead:.2f}s ahead (from WS).")

    def _status_line(self) -> str:
        assert self.ws is not None
        books = self.ws.books
        valid = sum(b.valid for b in books.values())
        two_sided = [b for b in books.values() if b.valid and b.mid is not None]
        sample = ", ".join(
            f"{b.ticker.split('-', 1)[-1]} {b.best_yes_bid}/{b.best_yes_ask}"
            for b in sorted(two_sided, key=lambda b: b.ticker)[:3]
        )
        s = self.ws.stats
        return (
            f"gate={self.gate.state.value} books {valid}/{len(books)} valid, "
            f"{len(two_sided)} two-sided | msgs {s.messages} gaps {s.gaps} "
            f"reconnects {max(0, s.connects - 1)} | {sample}"
        )

    # ---- main loop ----
    async def run(self) -> int:
        cfg = self.settings.config
        if self.kill_file.exists():
            say(f"KILL file present at {self.kill_file}; delete it to start. Not starting.")
            return 10
        n = await self.api.refresh_endpoint_costs()
        tier = await self.api.apply_account_limits()
        say(f"api tier {tier}; {n} non-default endpoint costs")
        await self._clock_check()
        tickers = await self._discover()
        if not tickers:
            say("no allowed markets to watch; exiting")
            return 1
        await self._start_ws(tickers)

        t_start = time.monotonic()
        next_status = t_start + 5
        next_clock = t_start + CLOCK_CHECK_SECONDS
        next_discover = t_start + cfg.markets.rediscover_minutes * 60
        try:
            while not self.stop.is_set():
                await asyncio.sleep(1.0)
                now = time.monotonic()
                for d in self.monitor.tick():
                    say(f"discontinuity: {d.cause.value} ({d.detail})")
                    self.gate.trip(d)
                if self.kill_file.exists():
                    say(f"KILL file found at {self.kill_file}; cancelling bot orders and stopping")
                    self.exit_code = 10
                    break
                if self.gate.state is not GateState.RUNNING and (
                    self._recovery is None or self._recovery.done()
                ):
                    self._recovery = asyncio.create_task(self.gate.recover(), name="recovery")
                await self._drain_events()
                if now >= next_status:
                    say(self._status_line())
                    next_status = now + cfg.markets.status_log_seconds
                if now >= next_clock:
                    await self._clock_check()
                    next_clock = now + CLOCK_CHECK_SECONDS
                if now >= next_discover:
                    next_discover = now + cfg.markets.rediscover_minutes * 60
                    new = await self._discover()
                    if new and self.ws and new != sorted(self.ws.books):
                        say("market set changed; restarting websocket")
                        await self._start_ws(new)
                if self.max_seconds is not None and now - t_start >= self.max_seconds:
                    say(f"--max-seconds {self.max_seconds:g} reached; stopping")
                    break
        finally:
            await self.shutdown()
        return self.exit_code

    async def shutdown(self) -> None:
        self.stop.set()
        if self._recovery is not None and not self._recovery.done():
            self._recovery.cancel()
        try:
            res = await asyncio.shield(self.api.cancel_all_bot_orders())
            say(f"shutdown: cancelled {len(res)} bot order(s)")
        except Exception as e:
            say(f"shutdown: cancel bot orders FAILED ({e!r}); exchange expiry will clear them")
        await self._stop_ws()
        await self._drain_events()


async def run_bot(settings: Settings, signer: Signer, max_seconds: float | None) -> int:
    cfg = settings.config
    ledger = await Ledger.open(cfg.paths.data_dir / "ledger.db")
    try:
        cfg_path = Path("config/config.yaml")
        if cfg_path.exists():
            await ledger.record_config(cfg_path.read_text(encoding="utf-8"))
        async with KalshiREST(settings.hosts.rest, cfg.rate_limits, signer=signer) as api:
            with keep_awake(cfg.safety.prevent_sleep) as awake:
                say(f"sleep prevention {'ON' if awake else 'off'}; ENV={settings.env.value}; "
                    "Phase 2 shadow mode: NO orders are placed")  # fmt: skip
                runner = Runner(settings, api, ledger, signer, cfg.paths.kill_file, max_seconds)
                return await runner.run()
    finally:
        await ledger.close()
