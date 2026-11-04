"""Command-line entry point. Phase 1: config validation and read-only exchange calls.

python -m kalshi_maker check-config
python -m kalshi_maker status
python -m kalshi_maker balance
python -m kalshi_maker markets [--series KXHIGHNY] [--status open] [--max 20]
python -m kalshi_maker market TICKER
python -m kalshi_maker orderbook TICKER [--depth 10]
python -m kalshi_maker run [--max-seconds N]   # Phase 2: books + ledger, NO orders
python -m kalshi_maker calibrate   # Phase 3: Brier / reliability / bias (after 3+ days)
python -m kalshi_maker fair        # Phase 3: weather fair value vs bid/ask, NO orders
python -m kalshi_maker clock          # local clock vs exchange (warns if drift > 1 s)
python -m kalshi_maker smoke          # all of the above in one go, for Phase 1 sign-off
python -m kalshi_maker cancel-bot-orders   # cancel resting orders with the bot prefix only
python -m kalshi_maker upgrade-tier   # request the Advanced API tier (asks first)
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
from .continuity import describe_clock_offset
from .logging_setup import setup_logging
from .money import TickGrid
from .rest import KalshiAPIError, KalshiREST, market_quote_summary
from .settings import (
    ConfigError,
    Settings,
    check_key_file,
    check_prod_gate,
    load_settings,
    startup_warning,
)


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
    if cmd == "run":
        from .runner import run_bot

        check_prod_gate(settings, settings.config.paths.data_dir / ".prod_confirmed")
        return await run_bot(settings, _signer(settings), args.max_seconds, args.config)
    if cmd == "calibrate":
        return await _calibrate(settings, args)
    if cmd == "fair":
        return await _fair(settings, args)
    if cmd == "check-config":
        print("config OK")
        print(_dump(settings.config.risk.model_dump()))
        return 0

    needs_auth = cmd in {"balance", "smoke", "cancel-bot-orders", "upgrade-tier"}
    async with _client(settings, needs_auth) as api:
        if cmd in {"status", "smoke"}:
            print("exchange status:", _dump(await api.exchange_status()))
        if cmd in {"clock", "smoke"}:
            rc = _report_clock(await api.check_clock(), settings)
            if cmd == "clock":
                return rc
        if cmd == "smoke":
            n = await api.refresh_endpoint_costs()
            print(f"endpoint costs: default={api.costs.default_cost}, {n} non-default loaded")
            tier = await api.apply_account_limits()
            print(
                f"api tier: {tier}  read {api.read_bucket.rate:g}/s"
                f" cap {api.read_bucket.capacity:g}  write {api.write_bucket.rate:g}/s"
                f" cap {api.write_bucket.capacity:g}"
            )
        if cmd == "cancel-bot-orders":
            res = await api.cancel_all_bot_orders()
            print(f"cancelled {len(res)} bot order(s); manual orders untouched")
            return 0
        if cmd == "upgrade-tier":
            ans = input("Request the permanent Advanced API tier from Kalshi? Type 'yes': ")
            if ans.strip().lower() != "yes":
                print("not requested")
                return 1
            print(_dump(await api.upgrade_api_usage_level()))
            return 0
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


async def _fair(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 3 shadow: fair value vs Kalshi bid/ask for today's and tomorrow's brackets."""
    from .ledger import Ledger
    from .weather.settlement import check_settlements
    from .weather.shadow import format_table, log_rows, make_engine, shadow_rows
    from .weather.sources import WeatherSources

    cfg = settings.config
    sources = WeatherSources()
    ledger = await Ledger.open(cfg.paths.data_dir / "ledger.db")
    # Real prices and real settlements live on PROD; read them publicly (no auth, no orders).
    prod = KalshiREST(cfg.hosts.prod.rest, cfg.rate_limits)
    try:
        async with _client(settings, False) as demo_api:
            engine = make_engine(cfg, sources)

            def booker(api: KalshiREST) -> Any:
                async def book(t: str) -> tuple[Decimal | None, Decimal | None]:
                    ob = await api.get_orderbook(t)
                    ask = None if ob.best_no_bid is None else 1 - ob.best_no_bid
                    return ob.best_yes_bid, ask

                return book

            rows = await shadow_rows(
                cfg, prod, engine, booker(prod), demo_book_for=booker(demo_api)
            )
            print(format_table(rows, cfg.weather.min_confidence))
            n = await log_rows(ledger, rows)
            print()
            print(f"logged {n} fair-value snapshot(s) to {cfg.paths.data_dir / 'ledger.db'}")
            print(
                "SHADOW MODE: no orders placed. bid/ask = PROD public book; demo b/a for reference."
            )
            if not args.no_settlement_check:
                chk = await check_settlements(prod, sources, cfg.markets.weather_series)
                print(
                    f"settlement check, prod, last 8 days: {chk.checked} checked, "
                    f"{chk.agreed} agree with NWS CLI, {len(chk.mismatches)} MISMATCH, "
                    f"skipped: {chk.skipped_no_cli} CLI aged out, {chk.skipped_no_value} no value; "
                    f"{chk.unparsed} unparsed"
                )
                for mm in chk.mismatches:
                    print(f"  MISMATCH {mm}")
                    await ledger.record_error("settlement_mismatch", mm)
    finally:
        await prod.aclose()
        await ledger.close()
        await sources.aclose()
    return 0


async def _calibrate(settings: Settings, args: argparse.Namespace) -> int:
    """Brier / reliability / per-station bias from the shadow log vs PROD outcomes."""
    from .weather.calibration import (
        build_report,
        fetch_outcomes,
        format_report,
        join,
        read_snapshots,
        write_csv,
    )

    cfg = settings.config
    db = args.db or (cfg.paths.data_dir / "ledger.db")
    if not Path(db).exists():
        raise ConfigError(f"ledger not found: {db}")
    snaps = read_snapshots(Path(db))
    print(f"read {len(snaps)} fair-value snapshots from {db}")
    async with KalshiREST(cfg.hosts.prod.rest, cfg.rate_limits) as prod:  # public, read-only
        markets = await fetch_outcomes(prod, {s["ticker"] for s in snaps})
    rows = join(snaps, markets)
    print(format_report(build_report(rows, min_days=args.min_days)))
    print()
    print(
        f"--- only rows with confidence >= {cfg.weather.min_confidence} "
        "(what Phase 4 would quote) ---"
    )
    print(
        format_report(
            build_report(rows, min_days=args.min_days, min_conf=cfg.weather.min_confidence)
        )
    )
    if args.csv:
        write_csv(rows, Path(args.csv))
        print(f"wrote {len(rows)} joined rows to {args.csv}")
    return 0


def _report_clock(chk: Any, settings: Settings) -> int:
    if chk is None:
        print("clock: exchange sent no Date header; drift unknown")
        return 0
    thr = settings.config.safety.clock_drift_warn_seconds
    print(
        f"clock: exchange - local in [{chk.lower:+.2f}s, {chk.upper:+.2f}s] "
        f"(Date header has 1 s resolution)"
    )
    warn = describe_clock_offset(chk.lower, chk.upper, thr)
    if warn:
        print(f"WARNING: {warn}. Resync Windows time: w32tm /resync  (admin PowerShell)")
        return 3
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
    rn = sub.add_parser("run", help="Phase 2: live books + ledger + safety (no orders)")
    rn.add_argument("--max-seconds", type=float, default=None)
    cal = sub.add_parser("calibrate", help="Phase 3 calibration report (run after 3+ days)")
    cal.add_argument("--db", type=Path, default=None, help="ledger.db (default: data_dir)")
    cal.add_argument("--min-days", type=int, default=3)
    cal.add_argument("--csv", type=Path, default=None, help="also write the joined rows")
    fv = sub.add_parser("fair", help="Phase 3 shadow: weather fair value vs Kalshi (no orders)")
    fv.add_argument("--no-settlement-check", action="store_true")
    sub.add_parser("clock")
    sub.add_parser("cancel-bot-orders")
    sub.add_parser("upgrade-tier")
    sm = sub.add_parser("smoke")
    sm.add_argument("--max", type=int, default=5)
    # Default to an allowed weather series: the unfiltered market list is dominated by
    # multivariate (KXMV*) combos, which are banned and 404 on single-market lookup.
    sm.add_argument("--series", default="KXHIGHNY")
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
