"""Kalshi WebSocket client: order books with seq tracking, own fills and orders.

Verified against docs.kalshi.com/asyncapi.yaml (2026-11-04):
  * One connection, authenticated at the handshake (sign GET /trade-api/ws/v2).
  * subscribe: {"id", "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}
    -> {"type": "subscribed", "id", "msg": {"channel", "sid"}}
  * orderbook_delta channel: `orderbook_snapshot` first, then `orderbook_delta`; both carry
    top-level `sid` and `seq`. Snapshot msg: market_ticker, clock, yes_dollars_fp,
    no_dollars_fp ([price, count] strings; keys absent when empty). Delta msg:
    market_ticker, clock, price_dollars, delta_fp (signed), side (yes|no),
    client_order_id (only when our order caused it).
  * One orderbook_delta subscription per connection. A repeated `subscribe` adds markets but
    does NOT resync them. On a seq gap: update_subscription {action: get_snapshot,
    market_tickers} (or unsubscribe/resubscribe) and REPLACE each book with its snapshot.
  * `use_yes_price: true` reports NO levels in YES-leg pricing (Kalshi will make it the
    default and then remove the flag); book.py converts back.
  * `fill` and `user_order` messages have no seq. After any reconnect, REST
    reconciliation backfills whatever was missed.
  * Kalshi pings every 10 s; the websockets library answers automatically.

Proxy: connections always go direct (proxy=None). No proxy or VPN support, by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .auth import Signer
from .book import BookError, OrderBook

log = logging.getLogger(__name__)

BOOK_CHANNEL = "orderbook_delta"
FILL_CHANNEL = "fill"
ORDERS_CHANNEL = "user_orders"


class Conn(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


ConnectFn = Callable[[str, dict[str, str]], Awaitable[Conn]]


async def default_connect(url: str, headers: dict[str, str]) -> Conn:
    from websockets.asyncio.client import connect

    conn: Conn = await connect(
        url,
        additional_headers=headers,
        proxy=None,  # never route through a proxy
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=16 * 2**20,
        user_agent_header="kalshi-maker/0.1",
    )
    return conn


@dataclass
class WSStats:
    connects: int = 0
    gaps: int = 0
    resnapshots: int = 0
    messages: int = 0
    unknown: int = 0
    # (sending_ts - local receipt) for recent messages. Network latency only pushes it
    # down, so the window max is a LOWER bound on how far the exchange clock is ahead of ours.
    leads: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    @property
    def clock_lead_lower_bound(self) -> float | None:
        return max(self.leads) if self.leads else None


@dataclass
class _Sub:
    channel: str
    sid: int
    last_seq: int | None = None


@dataclass
class WSCallbacks:
    on_fill: Callable[[Mapping[str, Any]], None] = lambda m: None
    on_user_order: Callable[[Mapping[str, Any]], None] = lambda m: None
    on_connection: Callable[[bool, str], None] = lambda up, why: None
    on_gap: Callable[[str], None] = lambda why: None


@dataclass
class KalshiWS:
    url: str
    signer: Signer
    tickers: list[str]
    callbacks: WSCallbacks = field(default_factory=WSCallbacks)
    connect: ConnectFn = default_connect
    use_yes_price: bool = True
    user_channels: bool = True
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    snapshot_timeout: float = 10.0
    mono: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __post_init__(self) -> None:
        self.books: dict[str, OrderBook] = {t: OrderBook(t) for t in self.tickers}
        self.stats = WSStats()
        self.connected = False
        self._conn: Conn | None = None
        self._next_id = 1
        self._pending: dict[int, str] = {}  # command id -> channel
        self._subs: dict[int, _Sub] = {}  # sid -> sub
        self._book_sid: int | None = None
        self._awaiting_snapshot: dict[str, float] = {}  # ticker -> mono when requested

    # ---- commands ----
    def _cmd(self, cmd: str, params: dict[str, Any], channel: str = "") -> dict[str, Any]:
        cid = self._next_id
        self._next_id += 1
        if channel:
            self._pending[cid] = channel
        return {"id": cid, "cmd": cmd, "params": params}

    def subscribe_commands(self) -> list[dict[str, Any]]:
        out = []
        if self.tickers:
            out.append(
                self._cmd(
                    "subscribe",
                    {
                        "channels": [BOOK_CHANNEL],
                        "market_tickers": list(self.tickers),
                        "use_yes_price": self.use_yes_price,
                    },
                    BOOK_CHANNEL,
                )
            )
        if self.user_channels:
            out.append(self._cmd("subscribe", {"channels": [FILL_CHANNEL]}, FILL_CHANNEL))
            out.append(self._cmd("subscribe", {"channels": [ORDERS_CHANNEL]}, ORDERS_CHANNEL))
        return out

    def snapshot_command(self, tickers: Iterable[str]) -> dict[str, Any] | None:
        ts = sorted(set(tickers))
        if self._book_sid is None or not ts:
            return None
        now = self.mono()
        for t in ts:
            self._awaiting_snapshot[t] = now
        self.stats.resnapshots += 1
        return self._cmd(
            "update_subscription",
            {"sids": [self._book_sid], "market_tickers": ts, "action": "get_snapshot"},
        )

    def resnapshot_all(self) -> dict[str, Any] | None:
        """Invalidate every book and request fresh snapshots (used by safety recovery)."""
        for b in self.books.values():
            b.invalidate()
        return self.snapshot_command(self.books)

    # ---- message handling (pure: returns commands to send) ----
    def handle(self, raw: str | bytes, recv_wall: float | None = None) -> list[dict[str, Any]]:
        self.stats.messages += 1
        try:
            m = json.loads(raw)
        except ValueError:
            log.warning("ws: non-JSON message ignored")
            return []
        if not isinstance(m, dict):
            return []
        sts = m.get("sending_ts_ms")
        if isinstance(sts, int):
            self.stats.leads.append(
                sts / 1000 - (recv_wall if recv_wall is not None else self.wall())
            )
        typ = m.get("type")
        if typ == "subscribed":
            return self._on_subscribed(m)
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            return self._on_book(m, typ)
        if typ == "fill":
            self.callbacks.on_fill(m.get("msg") or {})
            return []
        if typ == "user_order":
            self.callbacks.on_user_order(m.get("msg") or {})
            return []
        if typ == "error":
            msg = m.get("msg") or {}
            log.error("ws error", extra={"code": msg.get("code"), "detail": msg.get("msg")})
            ch = self._pending.pop(m.get("id", -1), None)
            if ch:
                raise ConnectionError(f"subscribe {ch} failed: {msg}")
            return []
        if typ in ("ok", "unsubscribed", "list_subscriptions"):
            return []
        self.stats.unknown += 1  # forward compatibility: ignore unknown types
        return []

    def _on_subscribed(self, m: Mapping[str, Any]) -> list[dict[str, Any]]:
        msg = m.get("msg") or {}
        ch = msg.get("channel") or self._pending.get(m.get("id", -1), "")
        self._pending.pop(m.get("id", -1), None)
        sid = msg.get("sid")
        if not isinstance(sid, int):
            return []
        self._subs[sid] = _Sub(ch, sid)
        if ch == BOOK_CHANNEL:
            self._book_sid = sid
        return []

    def _on_book(self, m: Mapping[str, Any], typ: str) -> list[dict[str, Any]]:
        sid, seq = m.get("sid"), m.get("seq")
        msg = m.get("msg") or {}
        if not isinstance(sid, int) or not isinstance(seq, int):
            log.warning("ws: book message without sid/seq ignored")
            return []
        sub = self._subs.setdefault(sid, _Sub(BOOK_CHANNEL, sid))
        if self._book_sid is None:
            self._book_sid = sid
        out: list[dict[str, Any]] = []
        if sub.last_seq is not None and seq != sub.last_seq + 1:
            # Gap (or duplicate/reorder): every book on this sid is now untrustworthy.
            self.stats.gaps += 1
            why = f"seq gap on sid {sid}: expected {sub.last_seq + 1}, got {seq}"
            log.warning("ws: " + why)
            self.callbacks.on_gap(why)
            for b in self.books.values():
                b.invalidate()
            cmd = self.snapshot_command(self.books)
            if cmd:
                out.append(cmd)
        sub.last_seq = seq
        ticker = msg.get("market_ticker")
        book = self.books.get(ticker) if isinstance(ticker, str) else None
        if book is None:
            return out  # a market we don't track (or no ticker): seq already accounted
        now = self.mono()
        clock = int(msg.get("clock") or 0)
        if typ == "orderbook_snapshot":
            book.replace(
                msg.get("yes_dollars_fp") or [],
                msg.get("no_dollars_fp") or [],
                clock,
                now,
                no_in_yes_price=self.use_yes_price,
            )
            self._awaiting_snapshot.pop(ticker, None)  # type: ignore[arg-type]
            return out
        if not book.valid:
            return out  # waiting for a snapshot: drop deltas, never apply on a stale book
        try:
            book.apply_delta(
                str(msg.get("side")),
                msg.get("price_dollars"),
                msg.get("delta_fp"),
                clock,
                now,
                no_in_yes_price=self.use_yes_price,
            )
        except (BookError, ValueError) as e:
            log.warning("ws: bad delta, resyncing", extra={"ticker": ticker, "err": str(e)})
            book.invalidate()
            cmd = self.snapshot_command([ticker])  # type: ignore[list-item]
            if cmd:
                out.append(cmd)
        return out

    def overdue_snapshots(self) -> list[str]:
        now = self.mono()
        return [t for t, at in self._awaiting_snapshot.items() if now - at > self.snapshot_timeout]

    # ---- connection loop ----
    async def _send(self, cmds: Iterable[dict[str, Any]]) -> None:
        if self._conn is None:
            return
        for c in cmds:
            await self._conn.send(json.dumps(c))

    async def request_resnapshot(self) -> None:
        cmd = self.resnapshot_all()
        if cmd:
            await self._send([cmd])

    def _reset_session(self) -> None:
        self._subs.clear()
        self._pending.clear()
        self._book_sid = None
        self._awaiting_snapshot.clear()
        self._next_id = 1
        for b in self.books.values():
            b.invalidate()

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            self._reset_session()
            try:
                headers = self.signer.ws_headers(self.url)
                self._conn = await self.connect(self.url, headers)
                self.stats.connects += 1
                attempt = 0
                self.connected = True
                self.callbacks.on_connection(True, "connected")
                await self._send(self.subscribe_commands())
                await self._pump(stop)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws connection lost", extra={"err": repr(e)})
                if self.connected:
                    self.callbacks.on_connection(False, repr(e))
                else:
                    self.callbacks.on_connection(False, f"connect failed: {e!r}")
            finally:
                self.connected = False
                for b in self.books.values():
                    b.invalidate()
                if self._conn is not None:
                    with contextlib.suppress(Exception):  # closing a dead socket may fail
                        await self._conn.close()
                    self._conn = None
            if stop.is_set():
                break
            delay = min(self.backoff_max, self.backoff_base * 2**attempt)
            attempt += 1
            await self.sleep(random.uniform(delay / 2, delay))  # noqa: S311

    async def _pump(self, stop: asyncio.Event) -> None:
        assert self._conn is not None
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(self._conn.recv(), timeout=1.0)
            except TimeoutError:
                overdue = self.overdue_snapshots()
                if overdue:
                    raise ConnectionError(f"snapshot overdue for {overdue}") from None
                continue
            cmds = self.handle(raw, recv_wall=self.wall())
            if cmds:
                await self._send(cmds)
