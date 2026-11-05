"""Async Kalshi REST client: signing, token-bucket rate limiting, safe retries, V2 orders.

Endpoint paths (relative to /trade-api/v2), verified against docs.kalshi.com 2026-11-04:
  GET    /exchange/status                         public
  GET    /markets  (cursor pagination)            public
  GET    /markets/{ticker}                        public
  GET    /markets/{ticker}/orderbook              public  -> orderbook_fp.{yes,no}_dollars
  GET    /series/{series_ticker}, /series         public
  GET    /events   (cursor pagination)            public
  GET    /account/endpoint_costs                  public  -> {default_cost, endpoint_costs[]}
  GET    /account/limits                          auth    -> {usage_tier, read, write, grants}
  POST   /account/api_usage_level/upgrade         auth    (CLI only, never automatic)
  GET    /portfolio/balance                       auth    own 1/s bucket
  GET    /portfolio/positions                     auth    -> market_positions, event_positions
  GET    /portfolio/orders, /portfolio/orders/{id}auth
  GET    /portfolio/fills                         auth
  V2 order writes: see order_types.py

Retry policy: GETs retry on 429/5xx/transport errors with full-jitter exponential backoff.
Writes are never blindly retried. An ambiguous create (timeout or 5xx) is resolved by
looking the order up by client_order_id first (create_order_idempotent).
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .auth import Signer
from .money import opt_decimal, to_decimal
from .order_types import OrderRequest, fmt_count, fmt_price, is_bot_order
from .settings import RateLimits

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
API_PREFIX = "/trade-api/v2"
BALANCE_PATH = "/portfolio/balance"
LOOKUP_ATTEMPTS = 4
LOOKUP_DELAY_S = 2.0


class KalshiAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class OrderOutcomeUnknown(Exception):
    """A create was sent but we couldn't tell if the exchange accepted it."""

    def __init__(self, client_order_id: str, cause: Exception) -> None:
        super().__init__(f"order {client_order_id} outcome unknown: {cause!r}")
        self.client_order_id = client_order_id
        self.cause = cause


class TokenBucket:
    """Continuous-refill token bucket with explicit capacity."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = self.capacity
        self._last = clock()

    def reconfigure(self, rate_per_sec: float, capacity: float) -> None:
        self._refill()
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = min(self._tokens, self.capacity)

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


def _template_match(template: str, path: str) -> bool:
    """Match "/portfolio/events/orders/:order_id" or "/cfbenchmarks/*rest" against a path."""
    t = template.strip("/").split("/")
    p = path.strip("/").split("/")
    for i, seg in enumerate(t):
        if seg.startswith("*"):
            return len(p) >= i + 1
        if i >= len(p):
            return False
        if seg.startswith(":"):
            continue
        if seg != p[i]:
            return False
    return len(t) == len(p)


class CostTable:
    """Per-endpoint token costs. Exact paths beat templates; templates beat the default."""

    def __init__(self, default_cost: int, costs: Mapping[str, int]) -> None:
        self.default_cost = default_cost
        self._exact: dict[tuple[str, str], int] = {}
        self._templates: list[tuple[str, str, int]] = []
        for key, cost in costs.items():
            self.set(key, cost)

    def set(self, key: str, cost: int) -> None:
        method, _, path = key.partition(" ")
        method = method.upper()
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX) :]
        if ":" in path or "*" in path:
            self._templates = [x for x in self._templates if (x[0], x[1]) != (method, path)]
            self._templates.append((method, path, cost))
        else:
            self._exact[(method, path)] = cost

    def cost(self, method: str, path: str) -> int:
        method = method.upper()
        if (method, path) in self._exact:
            return self._exact[(method, path)]
        for m, tpl, c in self._templates:
            if m == method and _template_match(tpl, path):
                return c
        return self.default_cost


def is_write(method: str, path: str) -> bool:
    """Write bucket: order placement, amends, cancels, order groups (docs: rate_limits)."""
    if method.upper() == "GET":
        return False
    return path.startswith(
        ("/portfolio/events/orders", "/portfolio/orders", "/portfolio/order_groups")
    )


@dataclass(frozen=True, slots=True)
class Balance:
    balance: Decimal  # dollars
    portfolio_value: Decimal | None  # dollars
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
        # Verified: portfolio_value is integer cents; there is no *_dollars variant.
        if d.get("portfolio_value") is not None:
            pv = to_decimal(d["portfolio_value"], "portfolio_value") / 100
        return cls(bal, pv, d)


@dataclass(frozen=True, slots=True)
class OrderbookLevels:
    """Raw REST orderbook: bids only on each side, as (price, size) Decimals, best last.

    NO levels are in NO-leg pricing (a NO bid at 0.30 is a YES ask at 0.70).
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


@dataclass(frozen=True, slots=True)
class ClockCheck:
    """Bounds on (server clock - local clock), in seconds, from an HTTP Date header.

    Date has 1 s resolution, so the true server time at receipt lies in [D, D+1).
    """

    lower: float
    upper: float

    def exceeds(self, threshold: float) -> bool:
        return self.lower > threshold or self.upper < -threshold

    @property
    def estimate(self) -> float:
        return (self.lower + self.upper) / 2


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
        self.costs = CostTable(limits.default_cost, limits.endpoint_costs)
        self.read_bucket = TokenBucket(
            limits.read_tokens_per_sec, limits.read_tokens_per_sec * limits.read_burst_seconds
        )
        self.write_bucket = TokenBucket(
            limits.write_tokens_per_sec, limits.write_tokens_per_sec * limits.write_burst_seconds
        )
        self.balance_bucket = TokenBucket(limits.balance_calls_per_sec, 1)
        self._sleep = sleep
        self.consecutive_errors = 0
        self.last_clock_check: ClockCheck | None = None
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

    # ---- limiter plumbing ----
    def _bucket_and_cost(self, method: str, path: str, items: int) -> tuple[TokenBucket, float]:
        if method == "GET" and path == BALANCE_PATH:
            return self.balance_bucket, 1.0
        bucket = self.write_bucket if is_write(method, path) else self.read_bucket
        return bucket, float(self.costs.cost(method, path) * max(1, items))

    def max_batch_items(self, method: str, path: str) -> int:
        """Batches bill per item and the whole batch must fit in the bucket at once."""
        bucket, per = self._bucket_and_cost(method, path, 1)
        return max(1, int(bucket.capacity // per))

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:  # Kalshi doesn't send it today (docs), but honour it if it appears
            try:
                return min(float(retry_after), self._limits.backoff_max_seconds)
            except ValueError:
                pass
        base = self._limits.backoff_base_seconds * (2**attempt)
        capped = min(base, self._limits.backoff_max_seconds)
        return random.uniform(0, capped)  # noqa: S311 - full jitter, not crypto

    def _note_clock(self, resp: httpx.Response, sent: float, received: float) -> None:
        date = resp.headers.get("Date")
        if not date:
            return
        try:
            server = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return
        # server time at receipt in [server, server + 1); local time in [sent, received]
        self.last_clock_check = ClockCheck(lower=server - received, upper=server + 1 - sent)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        auth: bool = False,
        items: int = 1,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        is_read = method == "GET"
        bucket, cost = self._bucket_and_cost(method, path, items)
        max_attempts = (self._limits.max_retries + 1) if is_read else 1
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            await bucket.take(cost)
            headers: dict[str, str] = {}
            if auth:
                if self._signer is None:
                    raise RuntimeError(f"{method} {path} needs auth but no signer configured")
                headers = self._signer.headers(method, self.base_path + path)
            sent = time.time()
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
            self._note_clock(resp, sent, time.time())
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

    # ---- limits / costs ----
    async def refresh_endpoint_costs(self) -> int:
        """Load non-default costs from the exchange (public endpoint). Returns count."""
        data = await self.request("GET", "/account/endpoint_costs")
        self.costs.default_cost = int(data.get("default_cost", self.costs.default_cost))
        n = 0
        for e in data.get("endpoint_costs") or []:
            self.costs.set(f"{e['method']} {e['path']}", int(e["cost"]))
            n += 1
        return n

    async def get_account_limits(self) -> dict[str, Any]:
        r: dict[str, Any] = await self.request("GET", "/account/limits", auth=True)
        return r

    async def apply_account_limits(self) -> str:
        """Size the read/write buckets from GET /account/limits. Returns the tier name."""
        lim = await self.get_account_limits()
        for name, bucket in (("read", self.read_bucket), ("write", self.write_bucket)):
            b = lim.get(name) or {}
            if b.get("refill_rate") and b.get("bucket_capacity"):
                bucket.reconfigure(float(b["refill_rate"]), float(b["bucket_capacity"]))
        return str(lim.get("usage_tier", "unknown"))

    async def upgrade_api_usage_level(self) -> dict[str, Any]:
        """Request the permanent Advanced tier. CLI-only; never called automatically."""
        r: dict[str, Any] = await self.request(
            "POST", "/account/api_usage_level/upgrade", auth=True
        )
        return r

    async def check_clock(self, samples: int = 6, spacing: float = 0.17) -> ClockCheck | None:
        """Bound the clock offset. Each Date header gives a ~1 s window; samples taken at
        different sub-second phases are intersected to narrow it."""
        lo, hi = float("-inf"), float("inf")
        for i in range(samples):
            if i:
                await asyncio.sleep(spacing)
            self.last_clock_check = None
            await self.exchange_status()
            c = self.last_clock_check
            if c is None:
                return None
            lo, hi = max(lo, c.lower), min(hi, c.upper)
        if lo > hi:  # inconsistent samples (clock stepped mid-check): report the last one
            return c
        self.last_clock_check = ClockCheck(lo, hi)
        return self.last_clock_check

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

    async def list_series(self, **params: Any) -> list[dict[str, Any]]:
        data = await self.request("GET", "/series", params=params)
        out: list[dict[str, Any]] = data.get("series") or []
        return out

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> OrderbookLevels:
        data = await self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        return OrderbookLevels.parse(data)

    def iter_trades(
        self, ticker: str, min_ts: int | None = None, max_ts: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Public trade tape for one market (GET /markets/trades; min_ts/max_ts are unix
        seconds). Field names are parsed defensively by paper.parse_trade; see the
        UNVERIFIED note there until a live call has confirmed them."""
        params: dict[str, Any] = {"ticker": ticker, "limit": 1000}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        if max_ts is not None:
            params["max_ts"] = int(max_ts)
        return self.paginate("/markets/trades", "trades", params)

    # ---- authenticated, read-only ----
    async def get_balance(self) -> Balance:
        return Balance.parse(await self.request("GET", BALANCE_PATH, auth=True))

    def iter_positions(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/positions", "market_positions", params, auth=True)

    def iter_orders(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/orders", "orders", params, auth=True)

    def iter_fills(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/portfolio/fills", "fills", params, auth=True)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        data = await self.request("GET", f"/portfolio/orders/{order_id}", auth=True)
        o: dict[str, Any] = data["order"]
        return o

    async def find_order_by_client_id(
        self, ticker: str, client_order_id: str, min_ts: int | None = None
    ) -> dict[str, Any] | None:
        """There is no lookup-by-client_order_id endpoint for direct members, so scan this
        market's orders (resting and recent) and match on client_order_id."""
        async for o in self.iter_orders(ticker=ticker, min_ts=min_ts, limit=200):
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    # ---- V2 order writes ----
    async def create_order(
        self, order: OrderRequest, grid: Any, now_s: int | None = None
    ) -> dict[str, Any]:
        order.validate(grid, now_s if now_s is not None else int(time.time()))
        r: dict[str, Any] = await self.request(
            "POST", "/portfolio/events/orders", json=order.to_json(), auth=True
        )
        return r

    async def create_order_idempotent(
        self, order: OrderRequest, grid: Any, now_s: int | None = None
    ) -> dict[str, Any]:
        """Create; if the outcome is ambiguous, look up by client_order_id. Never blind-resend.

        Returns the create response, or {"order_id", "client_order_id", "recovered": True}
        when the order was found after an ambiguous failure. Raises OrderOutcomeUnknown if the
        lookup can't find it either. The caller then treats it as possibly live, and the
        next reconciliation (or its exchange expiry) resolves it.
        """
        sent_at = int(time.time()) - 5
        try:
            return await self.create_order(order, grid, now_s)
        except KalshiAPIError as e:
            if e.status == 409:  # duplicate client_order_id => it already exists
                pass
            elif e.status < 500:
                raise  # definite rejection (400/401/403/429): nothing was created
            ambiguous: Exception = e
        except httpx.TransportError as e:
            ambiguous = e
        # REST reads lag the matching engine by up to a few seconds (seen live on demo:
        # GET /portfolio/orders/{id} returned 404 right after a 201 create), so a single
        # miss doesn't prove the order is absent. Poll a few times before giving up.
        found = None
        for i in range(LOOKUP_ATTEMPTS):
            if i:
                await self._sleep(LOOKUP_DELAY_S)
            found = await self.find_order_by_client_id(order.ticker, order.client_order_id, sent_at)
            if found is not None:
                break
        if found is not None:
            return {
                "order_id": found.get("order_id"),
                "client_order_id": order.client_order_id,
                "recovered": True,
                "order": found,
            }
        raise OrderOutcomeUnknown(order.client_order_id, ambiguous)

    async def batch_create_orders(
        self, orders: Iterable[OrderRequest], grids: Mapping[str, Any], now_s: int | None = None
    ) -> list[dict[str, Any]]:
        now = now_s if now_s is not None else int(time.time())
        lst = list(orders)
        for o in lst:
            o.validate(grids.get(o.ticker), now)
        path = "/portfolio/events/orders/batched"
        if len(lst) > self.max_batch_items("POST", path):
            raise ValueError(f"batch of {len(lst)} exceeds bucket capacity; split it")
        data = await self.request(
            "POST", path, json={"orders": [o.to_json() for o in lst]}, auth=True, items=len(lst)
        )
        out: list[dict[str, Any]] = data.get("orders") or []
        return out

    async def amend_order(
        self,
        order_id: str,
        *,
        ticker: str,
        side: str,
        price: Decimal,
        count: Decimal,
        expiration_time: int | None = None,
        client_order_id: str | None = None,
        updated_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """`count` is the new TOTAL (already filled + desired resting). Sending the current
        price and count with only a new expiration_time keeps queue position (docs)."""
        if expiration_time is not None and expiration_time <= int(time.time()):
            raise ValueError("expiration_time must be in the future")
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "price": fmt_price(price),
            "count": fmt_count(count),
        }
        if expiration_time is not None:
            body["expiration_time"] = int(expiration_time)
        if client_order_id:
            body["client_order_id"] = client_order_id
        if updated_client_order_id:
            body["updated_client_order_id"] = updated_client_order_id
        r: dict[str, Any] = await self.request(
            "POST", f"/portfolio/events/orders/{order_id}/amend", json=body, auth=True
        )
        return r

    async def cancel_order(self, order_id: str, ticker: str) -> dict[str, Any]:
        r: dict[str, Any] = await self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": ticker},
            auth=True,
        )
        return r

    async def batch_cancel_orders(self, orders: Iterable[tuple[str, str]]) -> list[dict[str, Any]]:
        """orders: (order_id, market_ticker) pairs. Splits to fit the write bucket."""
        lst = list(orders)
        path = "/portfolio/events/orders/batched"
        size = self.max_batch_items("DELETE", path)
        out: list[dict[str, Any]] = []
        for i in range(0, len(lst), size):
            chunk = lst[i : i + size]
            body = {"orders": [{"order_id": oid, "market_ticker": t} for oid, t in chunk]}
            data = await self.request("DELETE", path, json=body, auth=True, items=len(chunk))
            out.extend(data.get("orders") or [])
        return out

    async def cancel_all_bot_orders(self) -> list[dict[str, Any]]:
        """Cancel every resting order whose client_order_id has the bot prefix.

        Deliberately NOT the exchange's DELETE /portfolio/events/orders (cancel-all), which
        would also cancel the owner's manual orders.
        """
        targets: list[tuple[str, str]] = []
        async for o in self.iter_orders(status="resting", limit=1000):
            if is_bot_order(o.get("client_order_id")):
                targets.append((o["order_id"], o["ticker"]))
        if not targets:
            return []
        return await self.batch_cancel_orders(targets)
