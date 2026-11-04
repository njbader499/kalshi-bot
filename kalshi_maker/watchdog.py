"""Dead-man watchdog (Section 7): if the main loop stalls > 60 s, cancel all bot orders.

A stalled asyncio loop can't cancel anything itself, so the watchdog is a separate OS
thread with its own synchronous HTTP client and signer. It never shares objects with the
event loop, only a heartbeat timestamp (a float, written atomically under the GIL).

  heartbeat()          called by the main loop every tick
  thread               every check_seconds: if now - last_beat > stall_seconds and it
                       hasn't fired for this stall yet -> run cancel_fn() once, call on_fire.
                       It re-arms when heartbeats resume.

The cancel function cancels ONLY orders whose client_order_id carries the bot prefix
(GET /portfolio/orders?status=resting -> DELETE /portfolio/events/orders/batched in chunks
of 50, which fits the Basic write bucket at 2 tokens per cancel). It never uses the
exchange's cancel-all, which would also cancel the owner's manual orders. Exchange-side
order expiry remains the last line of defence if even this fails.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from .auth import Signer
from .order_types import is_bot_order

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(
        self,
        cancel_fn: Callable[[], int],
        stall_seconds: float = 60.0,
        check_seconds: float = 5.0,
        on_fire: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cancel_fn = cancel_fn
        self.stall_seconds = stall_seconds
        self.check_seconds = check_seconds
        self.on_fire = on_fire or (lambda msg: None)
        self._clock = clock
        self._beat = clock()
        self._fired = False
        self.fire_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def heartbeat(self) -> None:
        self._beat = self._clock()
        self._fired = False  # re-arm

    def check(self) -> bool:
        """One watchdog check. Returns True if it fired now."""
        stalled = self._clock() - self._beat
        if stalled <= self.stall_seconds or self._fired:
            return False
        self._fired = True
        self.fire_count += 1
        try:
            n = self.cancel_fn()
            msg = f"main loop stalled {stalled:.0f}s: watchdog cancelled {n} bot order(s)"
        except Exception as e:
            msg = (
                f"main loop stalled {stalled:.0f}s: watchdog cancel FAILED "
                f"({type(e).__name__}); exchange-side expiry will clear quotes"
            )
        log.error(msg)
        with contextlib.suppress(Exception):  # alerting must never break the watchdog
            self.on_fire(msg)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.check_seconds):
            self.check()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.check_seconds + 1)


def sync_cancel_bot_orders(
    base_url: str, signer: Signer, transport: httpx.BaseTransport | None = None
) -> int:
    """Synchronous, thread-safe cancel of every resting bot order. Returns the count."""
    base_path = httpx.URL(base_url).path
    with httpx.Client(base_url=base_url, timeout=10, transport=transport, trust_env=False) as c:

        def req(method: str, path: str, **kw: Any) -> Any:
            h = signer.headers(method, base_path + path)
            r = c.request(method, path, headers=h, **kw)
            r.raise_for_status()
            return r.json() if r.content else {}

        targets: list[tuple[str, str]] = []
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {"status": "resting", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = req("GET", "/portfolio/orders", params=params)
            for o in data.get("orders") or []:
                if is_bot_order(o.get("client_order_id")):
                    targets.append((o["order_id"], o["ticker"]))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        for i in range(0, len(targets), 50):
            chunk = targets[i : i + 50]
            req(
                "DELETE",
                "/portfolio/events/orders/batched",
                json={"orders": [{"order_id": oid, "market_ticker": t} for oid, t in chunk]},
            )
        return len(targets)
