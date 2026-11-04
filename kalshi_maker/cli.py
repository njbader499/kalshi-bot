"""Command-line entry point. Phase 1: config validation and read-only exchange calls.

python -m kalshi_maker check-config
python -m kalshi_maker status
python -m kalshi_maker balance
python -m kalshi_maker markets [--series KXHIGHNY] [--status open] [--max 20]
python -m kalshi_maker market TICKER
python -m kalshi_maker orderbook TICKER [--depth 10]
python -m kalshi_maker smoke          # all of the above in one go, for Phase 1 sign-off
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from .auth import Signer
from .logging_setup import setup_logging
from .money import TickGrid
from .rest import KalshiAPIError, KalshiREST, market_quote_summary
from .settings import ConfigError, Settings, check_key_file, load_settings, startup_warning


def _dump(obj: Any) -> str:
    return json.dumps(
        obj, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else repr(o)
    )


def _signer(settings: Settings) -> Signer:
    s = settings.secrets
    if s.KALSHI_KEY_ID is None or s.KALSHI_PRIVATE_KEY_PATH is None:
        raise ConfigError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in .env")
    check_key_file(s.KALSHI_PRIVATE_KEY_PATH)
    return Signer.from_file(s.KALSHI_KEY_ID.get_secret_value(), s.KALSHI_PRIVATE_KEY_PATH)


def _client(settings: Settings, auth: bool) -> KalshiREST:
    return KalshiREST(
        settings.hosts.rest,
        settings.config.rate_limits,
        signer=_signer(settings) if auth else None,
    )


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    cmd = args.cmd
    if cmd == "check-config":
        print("config OK")
        print(_dump(settings.config.risk.model_dump()))
        return 0

    needs_auth = cmd in {"balance", "smoke"}
    async with _client(settings, needs_auth) as api:
        if cmd in {"status", "smoke"}:
            print("exchange status:", _dump(await api.exchange_status()))
        if cmd in {"balance", "smoke"}:
            bal = await api.get_balance()
            print(f"balance: ${bal.balance}  portfolio_value: {bal.portfolio_value}")
        if cmd in {"markets", "smoke"}:
            n = 0
            params: dict[str, Any] = {"status": getattr(args, "status", "open")}
            if getattr(args, "series", None):
                params["series_ticker"] = args.series
            first: dict[str, Any] | None = None
            async for m in api.iter_markets(**params):
                first = first or m
                q = market_quote_summary(m)
                print(
                    f"{m.get('ticker'):<40} bid={q['yes_bid']} ask={q['yes_ask']} "
                    f"close={m.get('close_time')}"
                )
                n += 1
                if n >= getattr(args, "max", 10):
                    break
            print(f"({n} markets shown)")
            if cmd == "smoke" and first is not None:
                args.ticker = first["ticker"]
        if cmd in {"market", "smoke"} and getattr(args, "ticker", None):
            m = await api.get_market(args.ticker)
            try:
                grid = TickGrid.from_market(m)
                print(f"tick grid for {args.ticker}: {grid.ranges}")
            except Exception as e:
                print(f"tick grid unavailable for {args.ticker}: {e}")
            if cmd == "market":
                print(_dump(m))
        if cmd in {"orderbook", "smoke"} and getattr(args, "ticker", None):
            ob = await api.get_orderbook(args.ticker, getattr(args, "depth", None))
            print(
                f"orderbook {args.ticker}: best YES bid={ob.best_yes_bid} "
                f"best NO bid={ob.best_no_bid}"
            )
            print(f"  yes bids (best last): {[(str(p), str(s)) for p, s in ob.yes_bids[-5:]]}")
            print(f"  no bids  (best last): {[(str(p), str(s)) for p, s in ob.no_bids[-5:]]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalshi_maker")
    p.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    sub.add_parser("status")
    sub.add_parser("balance")
    m = sub.add_parser("markets")
    m.add_argument("--series")
    m.add_argument("--status", default="open")
    m.add_argument("--max", type=int, default=20)
    mk = sub.add_parser("market")
    mk.add_argument("ticker")
    ob = sub.add_parser("orderbook")
    ob.add_argument("ticker")
    ob.add_argument("--depth", type=int)
    sm = sub.add_parser("smoke")
    sm.add_argument("--max", type=int, default=5)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config, args.env_file)
    except ConfigError as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        return 2
    lc = settings.config.logging
    setup_logging(lc.level, settings.config.paths.log_dir, lc.max_bytes, lc.backup_count)
    print(startup_warning(settings), file=sys.stderr)
    try:
        return asyncio.run(_run(args, settings))
    except ConfigError as e:
        print(f"REFUSING TO START: {e}", file=sys.stderr)
        return 2
    except KalshiAPIError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"network error talking to {settings.hosts.rest}: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
