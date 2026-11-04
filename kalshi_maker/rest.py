"""Async Kalshi REST client: signing, token-bucket rate limiting, safe retries.

Phase 1 scope: read-only endpoints. Write endpoints (orders) arrive in Phase 4 with
client_order_id idempotency; this client never auto-retries a non-GET request.

Endpoint paths (relative to /trade-api/v2) used here:
  GET /exchange/status                       public
  GET /markets  (cursor pagination)          public
  GET /markets/{ticker}                      public
  GET /markets/{ticker}/orderbook            public  -> orderbook_fp.{yes,no}_dollars
  GET /series/{series_ticker}                public
  GET /events   (cursor pagination)          public
  GET /portfolio/balance                     auth    -> balance (cents int), balance_dollars (str)
  GET /portfolio/positions                   auth
  GET /portfolio/orders                      auth
  GET /portfolio/fills                       auth
  GET /account/limits                        auth    UNVERIFIED path (seen in search summary)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .auth import Signer
from .money import opt_decimal, to_decimal
from .settings import RateLimits

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class KalshiAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class TokenBucket:
    """Continuous-refill token bucket. capacity = rate * burst_seconds."""

    def __init__(
        self,
        rate_per_sec: float,
        burst_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = self.rate * burst_seconds
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_take(self, cost: float) -> float:
        """Take tokens if available; else return seconds to wait (and take nothing)."""
        if cost > self.capacity:
            raise ValueError(f"cost {cost} exceeds bucket capacity {self.capacity}")
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return 0.0
        return (cost - self._tokens) / self.rate

    async def take(self, cost: float) -> None:
        async with self._lock:
            while True:
                wait = self.try_take(cost)
                if wait <= 0:
                    return
                await asyncio.sleep(wait)


@dataclass(frozen=True, slots=True)
class Balance:
    balance: Decimal  # dollars
    portfolio_value: Decimal | None
    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, d: Mapping[str, Any]) -> Balance:
        if d.get("balance_dollars") not in (None, ""):
            bal = to_decimal(d["balance_dollars"], "balance_dollars")
        elif "balance" in d:  # legacy integer cents; truncates sub-cent amounts
            bal = to_decimal(d["balance"], "balance") / 100
        else:
            raise ValueError("balance response has neither balance_dollars nor balance")
        pv: Decimal | None = None
        if d.get("portfolio_value_dollars") not in (None, ""):  # UNVERIFIED field name
            pv = to_decimal(d["portfolio_value_dollars"], "portfolio_value_dollars")
        elif "portfolio_value" in d:
            pv = to_decimal(d["portfolio_value"], "portfolio_value") / 100
        return cls(bal, pv, d)


@dataclass(frozen=True, slots=True)
class OrderbookLevels:
    """Raw REST orderbook: bids only on each side, as (price, size) Decimals, best last.

    Conversion to asks (YES ask = 1 - best NO bid) lives in book.py (Phase 2).
    """

    yes_bids: tuple[tuple[Decimal, Decimal], ...]
    no_bids: tuple[tuple[Decimal, Decimal], ...]

    @classmethod
    def parse(cls, d: Mapping[str, Any]) -> OrderbookLevels:
        ob = d.get("orderbook_fp")
        if not isinstance(ob, Mapping):
            raise ValueError("orderbook response missing 'orderbook_fp'")

        def side(name: str) -> tuple[tuple[Decimal, Decimal], ...]:
            levels = ob.get(name) or []
            out = [
                (to_decimal(p, f"{name}.price"), to_decimal(s, f"{name}.size")) for p, s in levels
            ]
            out.sort(key=lambda x: x[0])
            return tuple(out)

        return cls(side("yes_dollars"), side("no_dollars"))

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[-1][0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return self.no_bids[-1][0] if self.no_bids else None


def market_quote_summary(m: Mapping[str, Any]) -> dict[str, Decimal | None]:
    return {
        "yes_bid": opt_decimal(m.get("yes_bid_dollars"), "yes_bid_dollars"),
        "yes_ask": opt_decimal(m.get("yes_ask_dollars"), "yes_ask_dollars"),
        "yes_bid_size": opt_decimal(m.get("yes_bid_size_fp"), "yes_bid_size_fp"),
        "yes_ask_size": opt_decimal(m.get("yes_ask_size_fp"), "yes_ask_size_fp"),
    }


class KalshiREST:
    def __init__(
        self,
        base_url: str,
        limits: RateLimits,
        signer: Signer | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.base_path = httpx.URL(self.base_url).path  # e.g. /trade-api/v2
        self._signer = signer
        self._limits = limits
        burst = limits.burst_seconds
        self._read = TokenBucket(limits.read_tokens_per_sec, burst)
        self._write = TokenBucket(limits.write_tokens_per_sec, burst)
        self._sleep = sleep
        self.consecutive_errors = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "kalshi-maker/0.1"},
        )

    async def __aenter__(self) -> KalshiREST:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _cost(self, method: str, path: str) -> int:
        key = f"{method.upper()} {path}"
        return self._limits.endpoint_costs.get(key, self._limits.default_cost)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self._limits.backoff_max_seconds)
            except ValueError:
                pass
        base = self._limits.backoff_base_seconds * (2**attempt)
        capped = min(base, self._limits.backoff_max_seconds)
        return random.uniform(0, capped)  # noqa: S311 - full jitter, not crypto

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        auth: bool = False,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        is_read = method == "GET"
        bucket = self._read if is_read else self._write
        max_attempts = (self._limits.max_retries + 1) if is_read else 1
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            await bucket.take(self._cost(method, path))
            headers: dict[str, str] = {}
            if auth:
                if self._signer is None:
                    raise RuntimeError(f"{method} {path} needs auth but no signer configured")
                headers = self._signer.headers(method, self.base_path + path)
            try:
                resp = await self._client.request(
                    method, path, params=clean_params, json=json, headers=headers
                )
            except httpx.TransportError as e:
                self.consecutive_errors += 1
                last_exc = e
                log.warning("transport error", extra={"path": path, "err": type(e).__name__})
                if attempt + 1 < max_attempts:
                    await self._sleep(self._backoff(attempt, None))
                    continue
                raise
            if resp.status_code < 400:
                self.consecutive_errors = 0
                return resp.json() if resp.content else {}
            self.consecutive_errors += 1
            err = KalshiAPIError(resp.status_code, method, path, resp.text)
            last_exc = err
            if resp.status_code in RETRYABLE_STATUS and attempt + 1 < max_attempts:
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.warning(
                    "retryable http error",
                    extra={"path": path, "status": resp.status_code, "delay": round(delay, 3)},
                )
                await self._sleep(delay)
                continue
            raise err
        assert last_exc is not None
        raise last_exc

    async def paginate(
        self,
        path: str,
        key: str,
        params: Mapping[str, Any] | None = None,
        auth: bool = False,
        max_pages: int = 1000,
    ) -> AsyncIterator[dict[str, Any]]:
        p = dict(params or {})
        for _ in range(max_pages):
            data = await self.request("GET", path, params=p, auth=auth)
            for item in data.get(key) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            p["cursor"] = cursor
        raise RuntimeError(f"pagination of {path} exceeded {max_pages} pages")

    # ---- public market data ----
    async def exchange_status(self) -> dict[str, Any]:
        r: dict[str, Any] = await self.request("GET", "/exchange/status")
        return r

    async def get_market(self, ticker: str) -> dict[str, Any]:
        data = await self.request("GET", f"/markets/{ticker}")
        m: dict[str, Any] = data["market"]
        return m

    def iter_markets(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        params.setdefault("limit", 1000)
        return self.paginate("/markets", "markets", params)

    def iter_events(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        params.setdefault("limit", 200)
        return self.paginate("/events", "events", params)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        data = await self.request("GET", f"/series/{series_ticker}")
        s: dict[str, Any] = data["series"]
        return s

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> OrderbookLevels:
        data = await self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return OrderbookLevels.parse(data)

    # ---- authenticated, read-only ----
    async def get_balance(self) -> Balance:
        return Balance.parse(await self.request("GET", "/portfolio/balance", auth=True))

    def iter_positions(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/positions", "market_positions", params, auth=True)

    def iter_orders(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/orders", "orders", params, auth=True)

    def iter_fills(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/fills", "fills", params, auth=True)

    async def get_account_limits(self) -> dict[str, Any]:  # UNVERIFIED path
        r: dict[str, Any] = await self.request("GET", "/account/limits", auth=True)
        return r
