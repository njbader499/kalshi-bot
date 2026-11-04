"""Orchestrator: live order books + ledger + laptop safety + weather shadow pricing.
Places NO orders (Phases 2-3).

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
from decimal import Decimal
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
from .instance_lock import AlreadyRunning, InstanceLock
from .killswitch import install_signal_kill, write_kill_file
from .ledger import Ledger
from .markets import discover_series_markets
from .power import keep_awake
from .reconcile import ReconcileReport, reconcile
from .rest import KalshiREST
from .settings import Settings
from .telegram import CommandRouter, Notifier, TelegramClient, poll_commands
from .trading import TradingLoop, describe_mode
from .watchdog import Watchdog, sync_cancel_bot_orders
from .weather.settlement import check_settlements
from .weather.shadow import log_rows, make_engine, shadow_rows
from .weather.sources import WeatherSources
from .ws import KalshiWS, WSCallbacks

log = logging.getLogger(__name__)

CLOCK_CHECK_SECONDS = 600
SNAPSHOT_WAIT_SECONDS = 20.0
MAX_CONSECUTIVE_TICK_ERRORS = 30
EXIT_KILL = 10
EXIT_ALREADY_RUNNING = 11


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
    prod_public: KalshiREST | None = None  # read-only real prices/settlements (Phase 3)
    notifier: Notifier | None = None
    telegram: TelegramClient | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int = 0

    def __post_init__(self) -> None:
        self.events: asyncio.Queue[tuple[str, Mapping[str, Any]]] = asyncio.Queue(maxsize=10000)
        self.ws: KalshiWS | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[bool] | None = None
        self.last_report: ReconcileReport | None = None
        self.weather_sources = WeatherSources()
        self.engine = make_engine(self.settings.config, self.weather_sources)
        self._shadow_task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._trade_task: asyncio.Task[None] | None = None
        self._notify_task: asyncio.Task[None] | None = None
        self._first_order_alerted = False
        self._poll_task: asyncio.Task[None] | None = None
        if self.notifier is None:  # no Telegram configured: alerts go nowhere
            self.notifier = Notifier(None, frozenset(), 3600)
        self.watchdog: Watchdog | None = None
        self.trading = TradingLoop(self.settings.config, self.ledger, api=self.api)
        safety = self.settings.config.safety
        self.monitor = ContinuityMonitor(safety.clock_jump_seconds, safety.loop_stall_seconds)
        self.gate = SafetyGate(
            RecoverySteps(self._reachable, self._cancel, self._resnapshot, self._reconcile),
            on_event=lambda kind, msg: self.alert(kind, msg),
        )

    def alert(self, kind: str, text: str) -> None:
        """Operator line + Telegram (instant for risk/kill/error/lifecycle, else digest)."""
        say(f"[{kind}] {text}")
        if self.notifier is not None:
            self.notifier.alert(kind, text)

    # ---- BotControl (Telegram commands) ----
    def status_text(self) -> str:
        tl = self.trading
        books = self.ws.books if self.ws else {}
        eq = tl.equity_proxy(books)
        rs = tl.risk.state
        day_pnl = eq - Decimal(rs.day_start) if rs.day_key else Decimal(0)
        cats: dict[str, Decimal] = {}
        for t in tl.pf._all_tickers():
            c = tl.pf.meta[t].category if t in tl.pf.meta else "?"
            cats[c] = cats.get(c, Decimal(0)) + tl.pf.market_exposure(t)
        lines = [
            describe_mode(self.settings.config),
            f"gate: {self.gate.state.value}  paused: {tl.paused}  "
            f"risk: {tl.risk.can_trade().reason or 'ok'}",
            f"equity proxy: {eq:.2f}  P&L today: {day_pnl:+.2f}",
            f"exposure by category: {', '.join(f'{k} {v:.2f}' for k, v in cats.items()) or 'none'}",
            f"markets quoted: {', '.join(sorted(tl.pf.quoted_markets())) or 'none'}",
            self._status_line() if self.ws else "websocket not started",
        ]
        return "\n".join(lines)

    def positions_text(self) -> str:
        ps = [p for p in self.trading.pf.positions.values() if p.qty != 0]
        if not ps:
            return "no open positions"
        return "\n".join(f"{p.ticker}: {p.qty:+} (cost {p.cost:.2f})" for p in ps)

    def orders_text(self) -> str:
        os_ = list(self.trading.pf.resting.values())
        tag = " (DRY)" if not self.trading.live else ""
        if not os_:
            return "no resting bot orders" + tag
        return "\n".join(f"{o.ticker} {o.side} {o.remaining} @ {o.price}{tag}" for o in os_)

    def limits_text(self) -> str:
        L = self.settings.config.risk
        return "\n".join(f"{k}: {v}" for k, v in L.model_dump().items())

    async def pause(self, reason: str) -> str:
        self.trading.paused = True
        n = await self.trading.cancel_all(f"paused: {reason}")
        self.alert("risk", f"PAUSED ({reason}); cancelled {n} order(s)")
        return f"paused; cancelled {n} order(s). /resume to quote again."

    async def resume(self) -> str:
        self.trading.paused = False
        msg = "resumed quoting"
        if self.trading.live and self.trading.risk.state.weekly_halt:
            msg += "; " + self.trading.risk.resume_weekly()
        self.alert("risk", msg)
        return msg

    async def kill(self, reason: str) -> str:
        write_kill_file(self.kill_file, reason)
        if self.trading.live:
            self.trading.risk.kill(reason)
        n = await self.trading.cancel_all(f"KILL: {reason}")
        self.alert("kill", f"KILL ({reason}): cancelled {n} order(s); stopping")
        return f"KILLED: cancelled {n} order(s). Bot stops now; local restart only."

    async def set_mode(self, scope: str, mode: str) -> str:
        tl = self.trading
        cats = {m.category.lower(): m.category for m in tl.pf.meta.values()}
        if scope in tl.pf.meta:
            (tl.off_markets.add if mode == "off" else tl.off_markets.discard)(scope)
        elif scope.lower() in cats:
            c = cats[scope.lower()]
            (tl.off_categories.add if mode == "off" else tl.off_categories.discard)(c)
        else:
            return f"unknown market or category: {scope}"
        self.alert("risk", f"mode {scope} -> {mode}")
        return f"{scope}: {mode}"

    # ---- recovery steps ----
    async def _reachable(self) -> None:
        await self.api.exchange_status()
        if self.ws is None or not self.ws.connected:
            raise ConnectionError("websocket not connected yet")

    async def _cancel(self) -> object:
        res = await self.api.cancel_all_bot_orders()
        self.trading.on_all_cancelled()  # exchange is flat on bot orders now
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
        self.trading.load_positions(r.positions_raw)
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
            self.alert("info", f"book resync: {why}")

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
                    if self.trading.live:
                        self.trading.om.on_fill(m)
                    if await self.ledger.record_fill(m, source="ws"):
                        self.alert("fill", f"{m.get('market_ticker')} {m.get('book_side')} "
                            f"{m.get('count_fp')} @ {m.get('yes_price_dollars')}")  # fmt: skip
                else:
                    await self.ledger.record_order_event(m)
                    if self.trading.live:
                        note = self.trading.om.on_user_order(m)
                        if note:
                            self.alert("info", note)
            except Exception as e:
                log.exception("ledger write failed")
                await self.ledger.record_error("ledger_write", repr(e), m)

    # ---- discovery ----
    async def _discover(self) -> list[str]:
        cfg = self.settings.config
        markets, rejected = await discover_series_markets(
            self.api, cfg.markets.weather_series, cfg.exclusions, cfg.fees
        )
        for mk in markets:  # each market carries its series' category (risk bucket)
            self.trading.update_markets([mk], str(mk.get("_category") or "unknown"))
        tickers = sorted(m["ticker"] for m in markets)
        say(
            f"watching {len(tickers)} market(s) in {', '.join(cfg.markets.weather_series)}"
            f" ({len(rejected)} excluded)"
        )
        for t, why in sorted(rejected.items())[:10]:
            log.info("excluded", extra={"ticker": t, "reason": why})
        return tickers

    # ---- Phase 3 shadow pricing (background; never blocks the safety loop) ----
    async def _shadow_once(self) -> None:
        prod = self.prod_public
        if prod is None:
            return

        async def book(t: str) -> tuple[Any, Any]:
            ob = await prod.get_orderbook(t)
            return ob.best_yes_bid, (None if ob.best_no_bid is None else 1 - ob.best_no_bid)

        def demo_book(t: str) -> tuple[Any, Any]:
            b = self.ws.books.get(t) if self.ws else None
            if b is None or not b.valid:
                return None, None
            return b.best_yes_bid, b.best_yes_ask

        try:
            rows = await shadow_rows(
                self.settings.config, prod, self.engine, book, demo_book_for=demo_book
            )
            n = await log_rows(self.ledger, rows)
            priced = [r for r in rows if r.fair is not None]
            for row in priced:
                assert row.fair is not None
                self.trading.update_fair(
                    row.contract.ticker, row.fair.p, row.fair.confidence,
                    row.fair.as_of.timestamp(),
                )  # fmt: skip
            say(f"shadow: logged {n} fair values ({len(rows) - len(priced)} not priced)")
        except Exception as e:
            log.exception("shadow pricing failed")
            await self.ledger.record_error("shadow_pricing", repr(e))

    async def _settlement_once(self) -> None:
        prod = self.prod_public
        if prod is None:
            return
        try:
            chk = await check_settlements(
                prod, self.weather_sources, self.settings.config.markets.weather_series
            )
            for mm in chk.mismatches:
                self.alert("error", f"SETTLEMENT MISMATCH vs NWS CLI: {mm}")
                await self.ledger.record_error("settlement_mismatch", mm)
            say(
                f"settlement check: {chk.agreed}/{chk.checked} agree with NWS CLI, "
                f"{len(chk.mismatches)} mismatch"
            )
        except Exception as e:
            log.exception("settlement check failed")
            await self.ledger.record_error("settlement_check", repr(e))

    async def _trade_once(self) -> None:
        if self.ws is None:
            return
        try:
            events = await self.trading.step(self.ws.books, self.gate.can_quote)
            om = self.trading.om
            if self.trading.live and om.creates and not self._first_order_alerted:
                self._first_order_alerted = True
                q = om.last_created
                what = (
                    f"{q.ticker} {'buy YES' if q.side == 'bid' else 'buy NO'} "
                    f"{q.count} @ YES price {q.price}"
                    if q
                    else "?"
                )
                self.alert(
                    "lifecycle", f"FIRST LIVE ORDER placed on {self.settings.env.value}: {what}"
                )
            for e in events:
                self.alert("risk", f"RISK STOP: {e} (all bot orders cancelled)")
                await self.ledger.record_error("risk_stop", e)
        except Exception as e:
            log.exception("trading step failed")
            await self.ledger.record_error("trading_step", repr(e))
            self.alert("error", f"trading step failed: {type(e).__name__}")

    def _watchdog_cancel_fn(self) -> Any:
        if not self.trading.live:
            return lambda: 0  # dry mode: nothing on the exchange to cancel
        rest_url, signer = self.settings.hosts.rest, self.signer
        return lambda: sync_cancel_bot_orders(rest_url, signer)

    def _spawn(self, attr: str, coro_fn: Any) -> None:
        t = getattr(self, attr)
        if t is None or t.done():
            setattr(self, attr, asyncio.create_task(coro_fn()))

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
            self.alert("error", f"clock drift: {warn}. Run 'w32tm /resync' (admin).")
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
        next_shadow = t_start + 20
        next_settle = t_start + 30
        next_trade = t_start + 25
        tick_errors = 0
        self.alert("lifecycle", f"bot started ({describe_mode(cfg)})")
        if self.telegram is not None and self.notifier is not None:
            router = CommandRouter(self, self.telegram.chat_id)
            self._poll_task = asyncio.create_task(
                poll_commands(
                    self.telegram,
                    router,
                    self.notifier,
                    self.stop,
                    on_command=lambda c, ok: say(f"telegram: {c} handled, reply queued"),
                ),
                name="telegram",
            )
        self.watchdog = Watchdog(
            self._watchdog_cancel_fn(),
            stall_seconds=60,
            on_fire=lambda msg: self.notifier.alert("kill", msg) if self.notifier else None,
        )
        self.watchdog.start()
        try:
            while not self.stop.is_set():
                await asyncio.sleep(1.0)
                now = time.monotonic()
                if self.watchdog is not None:
                    self.watchdog.heartbeat()
                if self.notifier is not None and self.notifier.pending:
                    self._spawn("_notify_task", self.notifier.flush)
                # Safety-critical steps first, outside the error-tolerant block.
                for d in self.monitor.tick():
                    say(f"discontinuity: {d.cause.value} ({d.detail})")
                    self.gate.trip(d)
                if self.kill_file.exists():
                    self.alert(
                        "kill", f"KILL file found at {self.kill_file}; cancelling and stopping"
                    )
                    self.exit_code = EXIT_KILL
                    break
                if self.gate.state is not GateState.RUNNING and (
                    self._recovery is None or self._recovery.done()
                ):
                    self._recovery = asyncio.create_task(self.gate.recover(), name="recovery")
                if self.max_seconds is not None and now - t_start >= self.max_seconds:
                    say(f"--max-seconds {self.max_seconds:g} reached; stopping")
                    break
                if now >= next_trade:
                    next_trade = now + cfg.trading.loop_seconds
                    self._spawn("_trade_task", self._trade_once)
                try:
                    (
                        next_status,
                        next_clock,
                        next_discover,
                        next_shadow,
                        next_settle,
                    ) = await self._housekeeping(
                        now, next_status, next_clock, next_discover, next_shadow, next_settle
                    )
                    tick_errors = 0
                except Exception as e:
                    # One flaky API call must not kill a long-running collector. Many in a
                    # row means something is really wrong: exit non-zero so the supervisor
                    # restarts the process cleanly.
                    tick_errors += 1
                    log.exception("housekeeping failed")
                    with contextlib.suppress(Exception):
                        await self.ledger.record_error("housekeeping", repr(e))
                    if tick_errors >= MAX_CONSECUTIVE_TICK_ERRORS:
                        self.alert("error", f"{tick_errors} consecutive loop errors; restarting")
                        self.exit_code = 1
                        break
        finally:
            await self.shutdown()
        return self.exit_code

    async def _housekeeping(
        self,
        now: float,
        next_status: float,
        next_clock: float,
        next_discover: float,
        next_shadow: float,
        next_settle: float,
    ) -> tuple[float, float, float, float, float]:
        cfg = self.settings.config
        await self._drain_events()
        if now >= next_status:
            next_status = now + cfg.markets.status_log_seconds
            say(self._status_line())
        if now >= next_clock:
            next_clock = now + CLOCK_CHECK_SECONDS
            await self._clock_check()
        if now >= next_shadow:
            next_shadow = now + cfg.weather.fair_interval_minutes * 60
            self._spawn("_shadow_task", self._shadow_once)
        if now >= next_settle:
            next_settle = now + cfg.weather.settlement_check_minutes * 60
            self._spawn("_settle_task", self._settlement_once)
        if now >= next_discover:
            next_discover = now + cfg.markets.rediscover_minutes * 60  # even on failure
            new = await self._discover()
            if new and self.ws and new != sorted(self.ws.books):
                say("market set changed; restarting websocket")
                await self._start_ws(new)
        return next_status, next_clock, next_discover, next_shadow, next_settle

    async def shutdown(self) -> None:
        self.stop.set()
        if self._recovery is not None and not self._recovery.done():
            self._recovery.cancel()
        with contextlib.suppress(Exception):
            await self.trading.cancel_all("shutdown")
        try:
            res = await asyncio.shield(self.api.cancel_all_bot_orders())
            say(f"shutdown: cancelled {len(res)} bot order(s)")
        except Exception as e:
            say(f"shutdown: cancel bot orders FAILED ({e!r}); exchange expiry will clear them")
        if self.watchdog is not None:
            self.watchdog.stop()
        for t in (
            self._shadow_task, self._settle_task, self._trade_task, self._poll_task,
            self._notify_task,
        ):  # fmt: skip
            if t is not None and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        await self._stop_ws()
        await self._drain_events()
        await self.weather_sources.aclose()
        self.alert("lifecycle", f"bot stopped (exit code {self.exit_code})")
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.notifier.flush(force_digest=True), timeout=10)
        if self.telegram is not None:
            await self.telegram.aclose()


async def run_bot(
    settings: Settings,
    signer: Signer,
    max_seconds: float | None,
    config_path: Path = Path("config/config.yaml"),
) -> int:
    cfg = settings.config
    lock = InstanceLock(cfg.paths.data_dir / "run.lock")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        say(f"{e}; refusing to start a second bot")
        return EXIT_ALREADY_RUNNING
    try:
        install_signal_kill(settings.config.paths.kill_file)
        return await _run_locked(settings, signer, max_seconds, config_path)
    finally:
        lock.release()


async def _run_locked(
    settings: Settings, signer: Signer, max_seconds: float | None, config_path: Path
) -> int:
    cfg = settings.config
    ledger = await Ledger.open(cfg.paths.data_dir / "ledger.db")
    try:
        if config_path.exists():
            await ledger.record_config(config_path.read_text(encoding="utf-8"))
        prod_public = KalshiREST(cfg.hosts.prod.rest, cfg.rate_limits)  # no signer: public
        async with (
            KalshiREST(settings.hosts.rest, cfg.rate_limits, signer=signer) as api,
            prod_public,
        ):
            with keep_awake(cfg.safety.prevent_sleep) as awake:
                say(f"sleep prevention {'ON' if awake else 'off'}; ENV={settings.env.value}; "
                    "orders: " + describe_mode(cfg))  # fmt: skip
                notifier, tg = _make_notifier(settings)
                runner = Runner(
                    settings,
                    api,
                    ledger,
                    signer,
                    cfg.paths.kill_file,
                    max_seconds,
                    prod_public=prod_public,
                    notifier=notifier,
                    telegram=tg,
                )
                return await runner.run()
    finally:
        await ledger.close()


def _make_notifier(settings: Settings) -> tuple[Notifier | None, TelegramClient | None]:
    sec, a = settings.secrets, settings.config.alerts
    token = sec.TELEGRAM_TOKEN.get_secret_value() if sec.TELEGRAM_TOKEN else ""
    chat = (sec.TELEGRAM_CHAT_ID or "").strip()
    if not token or not chat:
        say("telegram: not configured (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID empty); alerts off")
        return None, None
    client = TelegramClient(token, chat)
    say("telegram: alerts + commands on (whitelisted chat only)")
    return Notifier(client, frozenset(a.instant_kinds), a.fill_batch_minutes * 60), client
