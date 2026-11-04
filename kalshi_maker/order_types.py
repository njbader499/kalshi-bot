"""Order request types for Kalshi's V2 order endpoints.

Verified against docs.kalshi.com (read directly 2026-11-04, OpenAPI 3.36.0):

  POST   /portfolio/events/orders                  Create Order (V2)   CreateOrderV2Request
  POST   /portfolio/events/orders/batched          Batch Create (V2)   {"orders": [...]}
  POST   /portfolio/events/orders/{id}/amend       Amend (V2)          price/count/expiry
  DELETE /portfolio/events/orders/{id}             Cancel (V2)         ?market_ticker= for routing
  DELETE /portfolio/events/orders/batched          Batch Cancel (V2)   {"orders": [{order_id,
                                                                         market_ticker}]}

The legacy POST /portfolio/orders (ticker/side yes|no/action buy|sell/type) has been
deprecatable since May 6, 2026. This bot uses only the V2 shape.

V2 semantics: `side` is the YES book side. `bid` = buy YES, `ask` = sell YES (economically
buy NO at 1 - price). `price` is always the YES price as a fixed-point dollar string, and
`count` is a fixed-point contract string with at most 2 decimals.

Safety invariants enforced here, so no caller can get them wrong:
  * post_only is always true. There is no parameter to turn it off.
  * time_in_force is always good_till_canceled with an expiration_time. The bot never
    sends IOC/FOK, which could take liquidity, and never sends an order without an
    exchange-side expiry.
  * price is on the market's tick grid and strictly inside (0, 1).
  * client_order_id always carries the bot prefix, so reconciliation can tell bot orders
    from manual ones.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .money import ONE, ZERO, TickGrid

BOT_COID_PREFIX = "kmb"
_COID_RE = re.compile(rf"^{BOT_COID_PREFIX}-[0-9a-f]{{32}}$")
PRICE_QUANTUM = Decimal("0.0001")  # request fields accept 2-4 decimals
COUNT_QUANTUM = Decimal("0.01")  # FixedPointCount: 0-2 decimals, min granularity 0.01


class BookSide(StrEnum):
    BID = "bid"  # buy YES
    ASK = "ask"  # sell YES == buy NO at (1 - price)


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


class OrderError(ValueError):
    pass


def new_client_order_id() -> str:
    """uuid4 with the bot prefix. Generate and persist it BEFORE sending the order."""
    return f"{BOT_COID_PREFIX}-{uuid.uuid4().hex}"


def is_bot_order(client_order_id: str | None) -> bool:
    return bool(client_order_id) and bool(_COID_RE.match(client_order_id or ""))


def to_book(outcome: Outcome, outcome_price: Decimal) -> tuple[BookSide, Decimal]:
    """Buy `outcome` at `outcome_price` -> (V2 book side, YES price).

    Buying NO at 0.30 is selling YES at 0.70 (docs: getting_started/order_direction).
    """
    if outcome is Outcome.YES:
        return BookSide.BID, outcome_price
    return BookSide.ASK, ONE - outcome_price


def fmt_price(p: Decimal) -> str:
    if p != p.quantize(PRICE_QUANTUM):
        raise OrderError(f"price {p} has more than 4 decimals")
    return f"{p:.4f}"


def fmt_count(c: Decimal) -> str:
    if c != c.quantize(COUNT_QUANTUM):
        raise OrderError(f"count {c} has more than 2 decimals")
    return f"{c:.2f}"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    ticker: str
    side: BookSide
    price: Decimal  # YES price
    count: Decimal
    client_order_id: str
    expiration_time: int  # unix seconds; required (exchange-side dead-man switch)
    # taker_at_cross cancels the *incoming* order on a self-cross and leaves resting
    # quotes (and their queue position) alone. post_only would reject a cross anyway.
    self_trade_prevention_type: str = "taker_at_cross"
    cancel_order_on_pause: bool = True
    order_group_id: str | None = None
    subaccount: int | None = None

    def validate(self, grid: TickGrid | None, now_s: int) -> None:
        if not self.ticker:
            raise OrderError("empty ticker")
        if not is_bot_order(self.client_order_id):
            raise OrderError("client_order_id must come from new_client_order_id()")
        if not (ZERO < self.price < ONE):
            raise OrderError(f"price {self.price} outside (0, 1)")
        if grid is None:
            raise OrderError("refusing to send an order without the market's tick grid")
        if not grid.is_valid(self.price):
            raise OrderError(f"price {self.price} is not on {self.ticker}'s tick grid")
        if self.count <= 0:
            raise OrderError(f"count {self.count} must be positive")
        if self.expiration_time <= now_s:
            raise OrderError("expiration_time must be in the future")
        if self.self_trade_prevention_type not in ("maker", "taker_at_cross"):
            raise OrderError(f"bad self_trade_prevention_type {self.self_trade_prevention_type}")
        fmt_price(self.price)
        fmt_count(self.count)

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": self.side.value,
            "count": fmt_count(self.count),
            "price": fmt_price(self.price),
            "time_in_force": "good_till_canceled",
            "expiration_time": int(self.expiration_time),
            "post_only": True,
            "self_trade_prevention_type": self.self_trade_prevention_type,
            "cancel_order_on_pause": self.cancel_order_on_pause,
            "reduce_only": False,
        }
        if self.order_group_id:
            body["order_group_id"] = self.order_group_id
        if self.subaccount is not None:
            body["subaccount"] = self.subaccount
        return body
