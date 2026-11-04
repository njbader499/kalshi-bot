"""Phase 5: Telegram alerts/commands, watchdog, kill switch, and runner control wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from kalshi_maker.auth import Signer
from kalshi_maker.killswitch import install_signal_kill, write_kill_file
from kalshi_maker.ledger import Ledger
from kalshi_maker.order_types import new_client_order_id
from kalshi_maker.rest import KalshiREST
from kalshi_maker.settings import Secrets, Settings, load_config
from kalshi_maker.telegram import CommandRouter, Notifier, TelegramClient, poll_commands
from kalshi_maker.watchdog import Watchdog, sync_cancel_bot_orders

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
TOKEN = "123456:SECRET-TOKEN-abc"
CHAT = "4242"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeSender:
    def __init__(self, fail: int = 0) -> None:
        self.msgs: list[str] = []
        self.fail = fail

    async def send(self, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise httpx.ConnectError("boom")
        self.msgs.append(text)


# ---------- notifier ----------


async def test_instant_kinds_go_now_fills_are_batched() -> None:
    c = Clock()
    s = FakeSender()
    n = Notifier(s, frozenset({"risk", "kill", "error"}), batch_seconds=900, clock=c)
    n.alert("risk", "daily stop")
    n.alert("fill", "A bid 1 @ 0.70")
    n.alert("fill", "B ask 1 @ 0.30")
    await n.flush()
    assert s.msgs == ["[RISK] daily stop"]
    c.t += 901
    await n.flush()
    assert len(s.msgs) == 2 and s.msgs[1].startswith("Digest (2):") and "B ask" in s.msgs[1]


async def test_lifecycle_and_replies_always_instant() -> None:
    s = FakeSender()
    n = Notifier(s, frozenset(), batch_seconds=900, clock=Clock())
    n.alert("lifecycle", "bot started")
    n.alert("reply", "paused")
    await n.flush()
    assert s.msgs == ["[LIFECYCLE] bot started", "[REPLY] paused"]


async def test_queue_drops_oldest_and_failures_never_raise() -> None:
    s = FakeSender(fail=1)
    n = Notifier(s, frozenset({"risk"}), batch_seconds=900, max_queue=3, clock=Clock())
    for i in range(5):
        n.alert("risk", f"m{i}")
    assert n.dropped == 2
    await n.flush()  # first send fails: kept, retried next time, no exception
    assert n.failed == 1 and s.msgs == []
    await n.flush()
    assert s.msgs == ["[RISK] m2", "[RISK] m3", "[RISK] m4"]


async def test_no_sender_is_a_noop() -> None:
    n = Notifier(None, frozenset({"risk"}), 1)
    n.alert("risk", "x")
    await n.flush()
    assert n.pending == 1  # nothing sent, nothing raised


# ---------- telegram client: token never leaks ----------


async def test_client_sends_and_errors_never_contain_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(500)

    tg = TelegramClient(TOKEN, CHAT, transport=httpx.MockTransport(handler))
    assert TOKEN not in repr(tg)
    n = Notifier(tg, frozenset({"error"}), 60)
    n.alert("error", "hello")
    with caplog.at_level(logging.DEBUG):
        await n.flush()
    assert json.loads(seen[0].content) == {
        "chat_id": CHAT, "text": "[ERROR] hello", "disable_web_page_preview": True,
    }  # fmt: skip
    assert n.failed == 1
    for rec in caplog.records:
        assert TOKEN not in rec.getMessage() and TOKEN not in str(rec.__dict__)
    await tg.aclose()


# ---------- commands ----------


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status_text(self) -> str:
        return "STATUS"

    def positions_text(self) -> str:
        return "POS"

    def orders_text(self) -> str:
        return "ORDERS"

    def limits_text(self) -> str:
        return "LIMITS"

    async def pause(self, reason: str) -> str:
        self.calls.append("pause")
        return "paused"

    async def resume(self) -> str:
        self.calls.append("resume")
        return "resumed"

    async def kill(self, reason: str) -> str:
        self.calls.append("kill")
        return "killed"

    async def set_mode(self, scope: str, mode: str) -> str:
        self.calls.append(f"mode {scope} {mode}")
        return "ok"


def upd(text: str, chat: str = CHAT, uid: int = 1, date: int | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {"chat": {"id": int(chat)}, "text": text}
    if date is not None:
        m["date"] = date
    return {"update_id": uid, "message": m}


async def test_only_whitelisted_chat_is_obeyed() -> None:
    fc = FakeControl()
    r = CommandRouter(fc, CHAT)
    assert await r.handle(upd("/kill", chat="999")) is None
    assert fc.calls == [] and r.ignored == 1
    assert await r.handle(upd("/status@kalshi_bot")) == "STATUS"


async def test_read_only_and_risk_reducing_commands_are_immediate() -> None:
    fc = FakeControl()
    r = CommandRouter(fc, CHAT)
    assert await r.handle(upd("/positions")) == "POS"
    assert await r.handle(upd("/orders")) == "ORDERS"
    assert "config file" in (await r.handle(upd("/limits")) or "")
    assert await r.handle(upd("/pause")) == "paused"
    assert await r.handle(upd("/kill")) == "killed"
    assert await r.handle(upd("/mode KXHIGHNY-26NOV05-T70 off")) == "ok"
    assert fc.calls == ["pause", "kill", "mode KXHIGHNY-26NOV05-T70 off"]


async def test_risk_increasing_commands_need_confirmation() -> None:
    c = Clock()
    fc = FakeControl()
    r = CommandRouter(fc, CHAT, clock=c, code_fn=lambda: "ab12cd")
    reply = await r.handle(upd("/resume"))
    assert "/confirm ab12cd" in (reply or "") and fc.calls == []
    assert await r.handle(upd("/confirm wrong")) == "unknown or expired confirmation code"
    assert await r.handle(upd("/confirm ab12cd")) == "resumed"
    assert await r.handle(upd("/confirm ab12cd")) == "unknown or expired confirmation code"
    await r.handle(upd("/mode weather tilt"))
    c.t += 121  # expired
    assert "expired" in (await r.handle(upd("/confirm ab12cd")) or "")
    assert fc.calls == ["resume"]


async def test_dangerous_things_are_not_exposed() -> None:
    r = CommandRouter(FakeControl(), CHAT)
    for cmd in ("/enable_trading", "/raise_limits", "/reset_kill", "/flatten"):
        assert (await r.handle(upd(cmd)) or "").startswith("unknown command")
    assert "Phase 6" in (await r.handle(upd("/mode X mm")) or "")


async def test_poll_loop_backs_off_on_errors_and_replies() -> None:
    calls = {"n": 0}
    stop = asyncio.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getUpdates"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502)
            if calls["n"] == 2:
                return httpx.Response(200, json={"ok": True, "result": [upd("/status", uid=7)]})
            stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True})

    tg = TelegramClient(TOKEN, CHAT, transport=httpx.MockTransport(handler))
    sender = FakeSender()
    n = Notifier(sender, frozenset(), 60)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    handled: list[tuple[str, bool]] = []
    await asyncio.wait_for(
        poll_commands(
            tg, CommandRouter(FakeControl(), CHAT), n, stop, sleep=fake_sleep,
            on_command=lambda c, ok: handled.append((c, ok)),
        ),
        5,
    )  # fmt: skip
    assert sleeps == [1.0] and sender.msgs == ["[REPLY] STATUS"]
    assert handled == [("/status", True)]
    await tg.aclose()


# ---------- watchdog ----------


def test_watchdog_fires_once_per_stall_and_rearms() -> None:
    c = Clock()
    fired: list[str] = []
    cancels = {"n": 0}

    def cancel() -> int:
        cancels["n"] += 1
        return 2

    w = Watchdog(cancel, stall_seconds=60, on_fire=fired.append, clock=c)
    c.t += 30
    assert not w.check()
    c.t += 31
    assert w.check() and "cancelled 2" in fired[0]
    c.t += 30
    assert not w.check()  # same stall: once only
    w.heartbeat()
    c.t += 61
    assert w.check() and cancels["n"] == 2


def test_watchdog_survives_cancel_failure() -> None:
    c = Clock()
    fired: list[str] = []

    def boom() -> int:
        raise httpx.ConnectError("down")

    w = Watchdog(boom, stall_seconds=60, on_fire=fired.append, clock=c)
    c.t += 61
    assert w.check() and "FAILED" in fired[0] and "expiry" in fired[0]


def test_watchdog_thread_starts_and_stops() -> None:
    w = Watchdog(lambda: 0, stall_seconds=60, check_seconds=0.01)
    w.start()
    w.stop()
    assert w._thread is not None and not w._thread.is_alive()


def test_sync_cancel_only_touches_bot_orders() -> None:
    bot = new_client_order_id()
    deleted: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert "KALSHI-ACCESS-SIGNATURE" in req.headers
        if req.method == "GET":
            return httpx.Response(200, json={"orders": [
                {"order_id": "b1", "ticker": "T", "client_order_id": bot},
                {"order_id": "m1", "ticker": "T", "client_order_id": "manual"},
            ], "cursor": ""})  # fmt: skip
        assert req.url.path == "/trade-api/v2/portfolio/events/orders/batched"
        deleted.extend(json.loads(req.content)["orders"])
        return httpx.Response(200, json={"orders": []})

    n = sync_cancel_bot_orders(
        "https://external-api.demo.kalshi.co/trade-api/v2",
        Signer("k", ed25519.Ed25519PrivateKey.generate()),
        transport=httpx.MockTransport(handler),
    )
    assert n == 1 and deleted == [{"order_id": "b1", "market_ticker": "T"}]


# ---------- kill switch ----------


def test_kill_file_appends_reason(tmp_path: Path) -> None:
    k = tmp_path / "KILL"
    write_kill_file(k, "telegram /kill")
    write_kill_file(k, "signal SIGTERM")
    assert k.read_text().count("\n") == 2 and "SIGTERM" in k.read_text()


def test_sigterm_routes_to_kill(tmp_path: Path) -> None:
    k = tmp_path / "KILL"
    got: list[str] = []
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM,)}
    if sys.platform == "win32":
        old[signal.SIGBREAK] = signal.getsignal(signal.SIGBREAK)
    try:
        hooked = install_signal_kill(k, got.append)
        assert int(signal.SIGTERM) in hooked
        signal.raise_signal(signal.SIGTERM)
        assert k.exists() and got == ["signal SIGTERM"]
    finally:
        for s, h in old.items():
            signal.signal(s, h)


# ---------- runner control (real Runner, fake network) ----------


async def test_runner_control_pause_mode_kill(tmp_path: Path) -> None:
    from kalshi_maker.runner import Runner

    cfg = load_config(EXAMPLE)
    cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={
        "data_dir": tmp_path, "kill_file": tmp_path / "KILL"})})  # fmt: skip
    settings = Settings(cfg, Secrets(_env_file=None))  # type: ignore[call-arg]
    api = KalshiREST(
        cfg.hosts.demo.rest, cfg.rate_limits,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )  # fmt: skip
    lg = await Ledger.open(tmp_path / "l.db")
    signer = Signer("k", ed25519.Ed25519PrivateKey.generate())
    r = Runner(settings, api, lg, signer, cfg.paths.kill_file, None)
    r.trading.update_markets(
        [{"ticker": "KXHIGHNY-26NOV05-T70", "event_ticker": "KXHIGHNY-26NOV05",
          "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}],
        "Climate and Weather",
    )  # fmt: skip
    assert "DRY" in r.status_text() or "dry" in r.status_text().lower()
    assert "no resting bot orders" in r.orders_text()
    assert (await r.pause("test")).startswith("paused") and r.trading.paused
    assert "resumed" in await r.resume() and not r.trading.paused
    assert await r.set_mode("KXHIGHNY-26NOV05-T70", "off") == "KXHIGHNY-26NOV05-T70: off"
    assert "KXHIGHNY-26NOV05-T70" in r.trading.off_markets
    assert await r.set_mode("climate and weather", "off") == "climate and weather: off"
    assert "Climate and Weather" in r.trading.off_categories
    assert "unknown" in await r.set_mode("NOPE", "off")
    assert "KILLED" in await r.kill("test")
    assert cfg.paths.kill_file.exists()
    assert r._watchdog_cancel_fn()() == 0  # dry mode: the watchdog never calls the exchange
    assert "max_order_cost" in r.limits_text()
    await r.weather_sources.aclose()
    await api.aclose()
    await lg.close()


def test_equity_decimal_types() -> None:
    assert D("0.70") + D("0.30") == 1


def test_json_formatter_redacts_telegram_token() -> None:
    from kalshi_maker.logging_setup import JsonFormatter

    rec = logging.makeLogRecord(
        {"msg": "POST https://api.telegram.org/bot%s/sendMessage", "args": (TOKEN,)}
    )
    out = JsonFormatter().format(rec)
    assert TOKEN not in out and "bot<redacted>" in out


async def test_stale_commands_from_before_start_are_ignored() -> None:
    """A /kill handled just before a restart is re-delivered by Telegram; it must not
    kill the fresh process (seen live 2026-11-04)."""
    fc = FakeControl()
    r = CommandRouter(fc, CHAT, not_before=1_800_000_000.0)
    assert await r.handle(upd("/kill", date=1_799_999_990)) is None
    assert fc.calls == [] and r.stale == 1
    assert await r.handle(upd("/kill", date=1_800_000_005)) == "killed"
    assert fc.calls == ["kill"]
