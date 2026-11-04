"""Telegram alerts + remote commands (Section 10). Raw Bot API over httpx; no extra deps.

Alerts:
  * kinds in alerts.instant_kinds (default risk / kill / error) are sent immediately.
  * fills, WS resyncs and other info are batched into one digest every
    alerts.fill_batch_minutes (default 15).
  * A bounded queue drops the OLDEST message when full. Telegram failures are counted and
    logged, never raised: Telegram must never block or crash trading.
  * The bot token never reaches logs: errors are reduced to the exception type and HTTP
    status (an httpx error string would contain the URL, and the URL contains the token).

Commands (only from the whitelisted TELEGRAM_CHAT_ID; everything else is ignored):
  /status /positions /orders /limits       read-only
  /pause                                    immediate (reduces risk)
  /kill                                     immediate: cancel all, halt, KILL file
  /resume                                   needs /confirm <code> (increases risk)
  /mode <market|category> <tilt|off>        "off" immediate; "tilt" needs /confirm <code>
  /confirm <code>                           one-time code, valid 120 s
Deliberately NOT exposed: enabling trading, raising limits, clearing a KILL, taker flatten.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
# Telegram puts the bot token in the URL path (/bot<id>:<secret>/...). httpx logs request
# URLs at INFO, so redact any such token from every httpx/httpcore record at the source.
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact_token(text: str) -> str:
    return _TOKEN_RE.sub("bot<redacted>", text)


class _TokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if _TOKEN_RE.search(msg):
            record.msg = redact_token(msg)
            record.args = ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_TokenFilter())
CONFIRM_TTL_S = 120
MAX_MSG = 3900  # Telegram limit is 4096 chars


def _safe_err(e: BaseException) -> str:
    """Error description that can never contain the bot token."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


class TelegramClient:
    def __init__(
        self, token: str, chat_id: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not token or not chat_id:
            raise ValueError("telegram token and chat id are required")
        self._base = f"{API}/bot{token}"
        self.chat_id = str(chat_id)
        self._client = httpx.AsyncClient(timeout=35, transport=transport, trust_env=False)

    def __repr__(self) -> str:
        return "TelegramClient(<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, text: str) -> None:
        r = await self._client.post(
            f"{self._base}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": text[:MAX_MSG],
                "disable_web_page_preview": True,
            },
        )
        r.raise_for_status()

    async def updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        r = await self._client.get(f"{self._base}/getUpdates", params=params)
        r.raise_for_status()
        data = r.json()
        res: list[dict[str, Any]] = data.get("result") or []
        return res


class Sender(Protocol):
    async def send(self, text: str) -> None: ...


@dataclass
class Notifier:
    """Alert queue with instant kinds and a periodic digest. Never raises to callers."""

    sender: Sender | None
    instant_kinds: frozenset[str]
    batch_seconds: float
    max_queue: int = 200
    clock: Callable[[], float] = time.monotonic
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    _instant: deque[str] = field(default_factory=deque)
    _digest: deque[str] = field(default_factory=deque)
    _last_digest: float = 0.0

    def __post_init__(self) -> None:
        self._last_digest = self.clock()

    ALWAYS_INSTANT = frozenset({"reply", "lifecycle"})  # command replies, start/stop

    def alert(self, kind: str, text: str) -> None:
        instant = kind in self.instant_kinds or kind in self.ALWAYS_INSTANT
        q = self._instant if instant else self._digest
        if len(q) >= self.max_queue:
            q.popleft()  # drop OLDEST
            self.dropped += 1
        q.append(f"[{kind.upper()}] {text}")

    async def _send(self, text: str) -> bool:
        if self.sender is None:
            return False
        try:
            await self.sender.send(text)
            self.sent += 1
            return True
        except Exception as e:
            self.failed += 1
            log.warning("telegram send failed", extra={"err": _safe_err(e)})
            return False

    async def flush(self, force_digest: bool = False) -> None:
        while self._instant:
            if not await self._send(self._instant[0]):
                return  # keep it queued; retry next flush
            self._instant.popleft()
        due = self.clock() - self._last_digest >= self.batch_seconds
        if self._digest and (due or force_digest):
            items = list(self._digest)
            body = f"Digest ({len(items)}):\n" + "\n".join(items)
            if await self._send(body):
                self._digest.clear()
                self._last_digest = self.clock()

    @property
    def pending(self) -> int:
        return len(self._instant) + len(self._digest)


class BotControl(Protocol):
    def status_text(self) -> str: ...
    def positions_text(self) -> str: ...
    def orders_text(self) -> str: ...
    def limits_text(self) -> str: ...
    async def pause(self, reason: str) -> str: ...
    async def resume(self) -> str: ...
    async def kill(self, reason: str) -> str: ...
    async def set_mode(self, scope: str, mode: str) -> str: ...


@dataclass
class _Pending:
    action: Callable[[], Awaitable[str]]
    label: str
    expires: float


class CommandRouter:
    def __init__(
        self,
        control: BotControl,
        chat_id: str,
        clock: Callable[[], float] = time.monotonic,
        code_fn: Callable[[], str] = lambda: secrets.token_hex(3),
    ) -> None:
        self.control = control
        self.chat_id = str(chat_id)
        self._clock = clock
        self._code_fn = code_fn
        self._pending: dict[str, _Pending] = {}
        self.ignored = 0

    def _authorized(self, update: dict[str, Any]) -> bool:
        msg = update.get("message") or {}
        return str((msg.get("chat") or {}).get("id")) == self.chat_id

    def _confirm(self, label: str, action: Callable[[], Awaitable[str]]) -> str:
        code = self._code_fn()
        self._pending = {c: p for c, p in self._pending.items() if p.expires > self._clock()}
        self._pending[code] = _Pending(action, label, self._clock() + CONFIRM_TTL_S)
        return f"{label} increases risk. Reply /confirm {code} within {CONFIRM_TTL_S}s."

    async def handle(self, update: dict[str, Any]) -> str | None:
        """Returns the reply text, or None to stay silent (unauthorized / not a command)."""
        if not self._authorized(update):
            self.ignored += 1
            return None
        text = str((update.get("message") or {}).get("text") or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        c = self.control
        if cmd == "/status":
            return c.status_text()
        if cmd == "/positions":
            return c.positions_text()
        if cmd == "/orders":
            return c.orders_text()
        if cmd == "/limits":
            return c.limits_text() + "\n(limits can only be changed in the config file)"
        if cmd == "/pause":
            return await c.pause("telegram /pause")
        if cmd == "/kill":
            return await c.kill("telegram /kill")
        if cmd == "/resume":
            return self._confirm("Resume quoting", c.resume)
        if cmd == "/mode":
            if len(args) != 2 or args[1] not in ("tilt", "off", "mm"):
                return "usage: /mode <market-ticker|category> <tilt|off>"
            scope, mode = args
            if mode == "mm":
                return "mm (market-making) mode isn't built yet (Phase 6)"
            if mode == "off":
                return await c.set_mode(scope, "off")

            async def enable() -> str:
                return await c.set_mode(scope, mode)

            return self._confirm(f"Enable {mode} for {scope}", enable)
        if cmd == "/confirm":
            if len(args) != 1:
                return "usage: /confirm <code>"
            p = self._pending.pop(args[0], None)
            if p is None or p.expires < self._clock():
                return "unknown or expired confirmation code"
            return await p.action()
        if cmd in ("/start", "/help"):
            return "commands: /status /positions /orders /limits /pause /resume /kill /mode"
        return f"unknown command {cmd}"


async def poll_commands(
    client: TelegramClient,
    router: CommandRouter,
    notifier: Notifier,
    stop: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_command: Callable[[str, bool], None] | None = None,
) -> None:
    """Long-poll getUpdates. Errors back off and retry; never raises except on cancel."""
    offset: int | None = None
    backoff = 1.0
    while not stop.is_set():
        try:
            ups = await client.updates(offset)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("telegram poll failed", extra={"err": _safe_err(e)})
            await sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        for u in ups:
            offset = int(u.get("update_id", 0)) + 1
            try:
                reply = await router.handle(u)
            except Exception as e:
                log.exception("telegram command failed")
                reply = f"command failed: {type(e).__name__}"
            if reply:
                notifier.alert("reply", reply)
            if on_command is not None and reply is not None:
                # command name only (never arguments such as confirmation codes)
                text = str((u.get("message") or {}).get("text") or "").split()
                name = text[0].split("@", 1)[0] if text else "?"
                with contextlib.suppress(Exception):
                    on_command(name, bool(reply))
        await notifier.flush()
