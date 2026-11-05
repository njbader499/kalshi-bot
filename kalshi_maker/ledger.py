"""SQLite ledger (WAL). The books of record for fills, settlements and snapshots.

Rules:
  * Money and sizes are stored as TEXT decimal strings exactly as the exchange sent them,
    never REAL. Every row keeps the raw exchange JSON next to the parsed fields.
  * fills, settlements, order_events and all *_snapshots tables are APPEND-ONLY, enforced
    by triggers that abort any UPDATE or DELETE. Corrections are new rows, never edits.
  * Fills are keyed by trade_id (REST `fill_id` == `trade_id`; WS sends `trade_id`), so
    the same fill arriving over WS and REST is stored once. If the two copies disagree on
    count, price or fee, the mismatch goes to `errors` for alerting, and the first copy stays.
  * synchronous=FULL: a laptop can lose power, and a fill must not vanish.

Field names verified against docs (2026-11-04): REST GET /portfolio/fills, WS `fill`,
WS `user_order`, REST GET /portfolio/settlements.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 1

_TABLES = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    order_id        TEXT UNIQUE,
    ticker          TEXT NOT NULL,
    book_side       TEXT NOT NULL,
    price           TEXT NOT NULL,      -- YES price
    count           TEXT NOT NULL,
    expiration_time INTEGER,
    status          TEXT NOT NULL,      -- intent|sent|resting|canceled|executed|unknown|rejected
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS order_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     REAL NOT NULL,
    order_id        TEXT,
    client_order_id TEXT,
    ticker          TEXT,
    status          TEXT,
    reason          TEXT,
    ts_ms           INTEGER,
    raw             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    trade_id        TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    client_order_id TEXT,
    ticker          TEXT NOT NULL,
    outcome_side    TEXT,
    book_side       TEXT,
    count           TEXT NOT NULL,
    yes_price       TEXT NOT NULL,
    fee_cost        TEXT NOT NULL,
    is_taker        INTEGER NOT NULL,
    ts_ms           INTEGER,
    source          TEXT NOT NULL,      -- ws | rest
    recorded_at     REAL NOT NULL,
    raw             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_ticker_ts ON fills (ticker, ts_ms);

CREATE TABLE IF NOT EXISTS settlements (
    ticker          TEXT NOT NULL,
    settled_time    TEXT NOT NULL,
    event_ticker    TEXT,
    market_result   TEXT,
    yes_count       TEXT,
    no_count        TEXT,
    yes_total_cost  TEXT,
    no_total_cost   TEXT,
    revenue_cents   INTEGER,
    fee_cost        TEXT,
    recorded_at     REAL NOT NULL,
    raw             TEXT NOT NULL,
    PRIMARY KEY (ticker, settled_time)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at        REAL NOT NULL,
    ticker          TEXT NOT NULL,
    position        TEXT NOT NULL,
    exposure        TEXT,
    realized_pnl    TEXT,
    fees_paid       TEXT,
    raw             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at        REAL NOT NULL,
    balance         TEXT NOT NULL,
    portfolio_value TEXT,
    raw             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fair_value_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at        REAL NOT NULL,
    ticker          TEXT NOT NULL,
    fair            TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    yes_bid         TEXT,
    yes_ask         TEXT,
    detail          TEXT
);

CREATE TABLE IF NOT EXISTS order_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     REAL NOT NULL,
    action          TEXT NOT NULL,      -- create|amend|cancel|rejected|*_failed|create_unknown
    ticker          TEXT NOT NULL,
    side            TEXT,
    price           TEXT,
    count           TEXT,
    reason          TEXT,
    dry_run         INTEGER NOT NULL    -- 1 = trading disabled: recorded, never sent
);

CREATE TABLE IF NOT EXISTS config_versions (
    sha256          TEXT PRIMARY KEY,
    first_seen      REAL NOT NULL,
    content         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     REAL NOT NULL,
    mode            TEXT NOT NULL,      -- pess | opt
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,      -- bid (buy YES) | ask (buy NO)
    price           TEXT NOT NULL,      -- YES price
    count           TEXT NOT NULL,
    trade_id        TEXT NOT NULL,
    trade_ts        REAL NOT NULL,
    queue_ahead     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fill_marks (
    fill_key        TEXT PRIMARY KEY,   -- real:<trade_id> | paper:<mode>:<trade_id>:<side>
    kind            TEXT NOT NULL,      -- real | paper_pess | paper_opt
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           TEXT NOT NULL,
    filled_at       REAL NOT NULL,
    mid_at_fill     TEXT,
    mid_5m          TEXT,
    mid_30m         TEXT,
    mid_60m         TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at     REAL NOT NULL,
    kind            TEXT NOT NULL,
    detail          TEXT NOT NULL,
    raw             TEXT
);
"""

APPEND_ONLY = (
    "fills",
    "settlements",
    "order_events",
    "position_snapshots",
    "balance_snapshots",
    "fair_value_snapshots",
    "order_actions",
    "paper_fills",
    "config_versions",
    "errors",
)


_MARK_COLS = ("mid_at_fill", "mid_5m", "mid_30m", "mid_60m")
_MARK_UPDATES = {c: f"UPDATE fill_marks SET {c} = ? WHERE fill_key = ?" for c in _MARK_COLS}  # noqa: S608
_MARK_SELECTS = {
    c: (
        f"SELECT side, mid_at_fill, {c} FROM fill_marks WHERE kind LIKE ? AND "  # noqa: S608
        f"filled_at >= ? AND mid_at_fill IS NOT NULL AND {c} IS NOT NULL"
    )
    for c in ("mid_5m", "mid_30m", "mid_60m")
}


def _triggers() -> str:
    out = []
    for t in APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(out)


async def _migrate(db: aiosqlite.Connection) -> None:
    """Additive, idempotent schema changes for ledgers created by older versions."""
    async with db.execute("PRAGMA table_info(fair_value_snapshots)") as cur:
        cols = {row[1] async for row in cur}
    if "detail" not in cols:  # added in Phase 3
        await db.execute("ALTER TABLE fair_value_snapshots ADD COLUMN detail TEXT")


def _j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def _s(v: Any) -> str | None:
    """Exchange value -> TEXT. Refuses floats so money never round-trips through binary."""
    if v is None:
        return None
    if isinstance(v, float):
        raise TypeError(f"refusing to store float {v!r} in the ledger")
    return str(v)


class Ledger:
    def __init__(self, db: aiosqlite.Connection, clock: Any = time.time) -> None:
        self.db = db
        self._clock = clock

    @classmethod
    async def open(cls, path: Path) -> Ledger:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=FULL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_TABLES + _triggers())
        await _migrate(db)
        await db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self.db.close()

    async def journal_mode(self) -> str:
        async with self.db.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else ""

    # ---- fills ----
    async def record_fill(self, f: Mapping[str, Any], source: str) -> bool:
        """Insert a fill from WS (`fill` msg) or REST (/portfolio/fills). True if new."""
        trade_id = f.get("trade_id") or f.get("fill_id")
        if not trade_id:
            await self.record_error("fill_without_id", "fill has no trade_id/fill_id", f)
            return False
        ticker = f.get("market_ticker") or f.get("ticker")
        row = (
            trade_id,
            f.get("order_id"),
            f.get("client_order_id") or None,
            ticker,
            f.get("outcome_side"),
            f.get("book_side"),
            _s(f.get("count_fp")),
            _s(f.get("yes_price_dollars")),
            _s(f.get("fee_cost")),
            1 if f.get("is_taker") else 0,
            f.get("ts_ms") or (int(f["ts"]) * 1000 if isinstance(f.get("ts"), int) else None),
            source,
            self._clock(),
            _j(f),
        )
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO fills (trade_id, order_id, client_order_id, ticker, "
            "outcome_side, book_side, count, yes_price, fee_cost, is_taker, ts_ms, source, "
            "recorded_at, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        inserted = cur.rowcount == 1
        if not inserted:
            await self._check_duplicate_fill(trade_id, f, source)
        elif f.get("is_taker"):
            await self._error_nocommit(
                "taker_fill", f"fill {trade_id} was a TAKER fill; maker-only bot", f
            )
        await self.db.commit()
        return inserted

    async def _check_duplicate_fill(self, trade_id: str, f: Mapping[str, Any], src: str) -> None:
        async with self.db.execute(
            "SELECT count, yes_price, fee_cost, order_id FROM fills WHERE trade_id = ?",
            (trade_id,),
        ) as cur:
            old = await cur.fetchone()
        if old is None:
            return
        from decimal import Decimal

        def same(a: str | None, b: Any) -> bool:
            if a is None or b is None:
                return True  # a missing field on one copy isn't a contradiction
            return Decimal(a) == Decimal(str(b))

        mism = []
        if not same(old[0], f.get("count_fp")):
            mism.append(f"count {old[0]} vs {f.get('count_fp')}")
        if not same(old[1], f.get("yes_price_dollars")):
            mism.append(f"yes_price {old[1]} vs {f.get('yes_price_dollars')}")
        if not same(old[2], f.get("fee_cost")):
            mism.append(f"fee {old[2]} vs {f.get('fee_cost')}")
        if old[3] and f.get("order_id") and old[3] != f.get("order_id"):
            mism.append(f"order_id {old[3]} vs {f.get('order_id')}")
        if mism:
            await self._error_nocommit(
                "fill_mismatch", f"fill {trade_id} ({src}) disagrees: " + "; ".join(mism), f
            )

    async def fill_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM fills") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def latest_fill_ts_ms(self) -> int | None:
        async with self.db.execute("SELECT MAX(ts_ms) FROM fills") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ---- orders ----
    async def record_order_event(self, o: Mapping[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO order_events (recorded_at, order_id, client_order_id, ticker, status, "
            "reason, ts_ms, raw) VALUES (?,?,?,?,?,?,?,?)",
            (
                self._clock(),
                o.get("order_id"),
                o.get("client_order_id") or None,
                o.get("ticker"),
                o.get("status"),
                o.get("last_update_reason"),
                o.get("last_updated_ts_ms") or o.get("created_ts_ms"),
                _j(o),
            ),
        )
        coid = o.get("client_order_id")
        if coid and o.get("status"):
            await self.db.execute(
                "UPDATE orders SET status = ?, order_id = COALESCE(order_id, ?), updated_at = ?,"
                " raw = ? WHERE client_order_id = ?",
                (o.get("status"), o.get("order_id"), self._clock(), _j(o), coid),
            )
        await self.db.commit()

    async def record_order_intent(
        self,
        client_order_id: str,
        ticker: str,
        book_side: str,
        price: str,
        count: str,
        expiration_time: int,
    ) -> None:
        """Persist BEFORE sending, so a crash mid-send leaves a findable record."""
        now = self._clock()
        await self.db.execute(
            "INSERT INTO orders (client_order_id, ticker, book_side, price, count, "
            "expiration_time, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (client_order_id, ticker, book_side, price, count, expiration_time, "intent", now, now),
        )
        await self.db.commit()

    # ---- settlements & snapshots ----
    async def record_settlement(self, s: Mapping[str, Any]) -> bool:
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO settlements (ticker, settled_time, event_ticker, market_result,"
            " yes_count, no_count, yes_total_cost, no_total_cost, revenue_cents, fee_cost,"
            " recorded_at, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s.get("ticker"),
                s.get("settled_time"),
                s.get("event_ticker"),
                s.get("market_result"),
                _s(s.get("yes_count_fp")),
                _s(s.get("no_count_fp")),
                _s(s.get("yes_total_cost_dollars")),
                _s(s.get("no_total_cost_dollars")),
                s.get("revenue"),
                _s(s.get("fee_cost")),
                self._clock(),
                _j(s),
            ),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def snapshot_positions(self, positions: Iterable[Mapping[str, Any]]) -> int:
        now = self._clock()
        n = 0
        for p in positions:
            await self.db.execute(
                "INSERT INTO position_snapshots (taken_at, ticker, position, exposure, "
                "realized_pnl, fees_paid, raw) VALUES (?,?,?,?,?,?,?)",
                (
                    now,
                    p.get("ticker"),
                    _s(p.get("position_fp")) or "0",
                    _s(p.get("market_exposure_dollars")),
                    _s(p.get("realized_pnl_dollars")),
                    _s(p.get("fees_paid_dollars")),
                    _j(p),
                ),
            )
            n += 1
        await self.db.commit()
        return n

    async def snapshot_balance(self, raw: Mapping[str, Any], balance: str, pv: str | None) -> None:
        await self.db.execute(
            "INSERT INTO balance_snapshots (taken_at, balance, portfolio_value, raw) "
            "VALUES (?,?,?,?)",
            (self._clock(), balance, pv, _j(raw)),
        )
        await self.db.commit()

    async def record_fair_value(
        self,
        ticker: str,
        fair: str,
        confidence: str,
        source: str,
        as_of: str,
        yes_bid: str | None,
        yes_ask: str | None,
        detail: Any = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO fair_value_snapshots (taken_at, ticker, fair, confidence, source, "
            "as_of, yes_bid, yes_ask, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self._clock(), ticker, fair, confidence, source, as_of, yes_bid, yes_ask,
                None if detail is None else _j(detail),
            ),
        )  # fmt: skip
        await self.db.commit()

    async def record_order_action(
        self,
        action: str,
        ticker: str,
        side: str,
        price: str | None,
        count: str | None,
        reason: str,
        dry_run: bool,
    ) -> None:
        await self.db.execute(
            "INSERT INTO order_actions (recorded_at, action, ticker, side, price, count, reason,"
            " dry_run) VALUES (?,?,?,?,?,?,?,?)",
            (self._clock(), action, ticker, side, price, count, reason, 1 if dry_run else 0),
        )
        await self.db.commit()

    async def record_config(self, content: str) -> str:
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        await self.db.execute(
            "INSERT OR IGNORE INTO config_versions (sha256, first_seen, content) VALUES (?,?,?)",
            (h, self._clock(), content),
        )
        await self.db.commit()
        return h

    async def record_paper_fill(self, f: Any, recorded_at: float) -> None:
        await self.db.execute(
            "INSERT INTO paper_fills (recorded_at, mode, ticker, side, price, count, trade_id, "
            "trade_ts, queue_ahead) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                recorded_at, f.mode, f.ticker, f.side, str(f.price), str(f.count), f.trade_id,
                float(f.trade_ts), str(f.queue_ahead),
            ),
        )  # fmt: skip
        await self.db.commit()

    async def upsert_fill_mark(
        self, key: str, kind: str, ticker: str, side: str, price: str, filled_at: float,
        **marks: str | None,
    ) -> None:  # fmt: skip
        """Create the row on the first call; later calls only fill in the given marks."""
        await self.db.execute(
            "INSERT OR IGNORE INTO fill_marks (fill_key, kind, ticker, side, price, filled_at) "
            "VALUES (?,?,?,?,?,?)",
            (key, kind, ticker, side, price, filled_at),
        )
        for col, val in marks.items():
            if col not in _MARK_COLS:
                raise ValueError(f"unknown mark column {col}")
            if val is not None:
                stmt = _MARK_UPDATES[col]  # fixed statements: no user input in the SQL text
                await self.db.execute(stmt, (val, key))
        await self.db.commit()

    async def pending_fill_marks(
        self, before: float
    ) -> list[tuple[str, str, str, float, dict[str, str | None]]]:
        """Rows filled before `before` with a horizon mark missing:
        (fill_key, ticker, side, filled_at, {mid_5m, mid_30m, mid_60m})."""
        q = (
            "SELECT fill_key, ticker, side, filled_at, mid_5m, mid_30m, mid_60m FROM fill_marks "
            "WHERE filled_at <= ? AND (mid_5m IS NULL OR mid_30m IS NULL OR mid_60m IS NULL) "
            "ORDER BY filled_at"
        )
        async with self.db.execute(q, (before,)) as cur:
            return [
                (
                    str(r[0]), str(r[1]), str(r[2]), float(r[3]),
                    {"mid_5m": r[4], "mid_30m": r[5], "mid_60m": r[6]},
                )
                async for r in cur
            ]  # fmt: skip

    async def adverse_selection(self, kind_prefix: str, since: float) -> dict[str, tuple[int, str]]:
        """Mean move of the mid AGAINST us per horizon: for a YES buy (bid) that is
        mid_at_fill - mid_h; for a NO buy (ask) it is mid_h - mid_at_fill. Positive = the
        market moved against the fill. Returns {horizon: (n, mean)} as strings."""
        out: dict[str, tuple[int, str]] = {}
        for col, q in _MARK_SELECTS.items():
            n = 0
            tot = Decimal(0)
            async with self.db.execute(q, (kind_prefix + "%", since)) as cur:
                async for side, m0, mh in cur:
                    if str(mh) == "na":
                        continue
                    move = Decimal(str(m0)) - Decimal(str(mh))
                    tot += move if side == "bid" else -move
                    n += 1
            out[col] = (n, str((tot / n).quantize(Decimal("0.0001"))) if n else "-")
        return out

    async def _error_nocommit(self, kind: str, detail: str, raw: Any = None) -> None:
        await self.db.execute(
            "INSERT INTO errors (recorded_at, kind, detail, raw) VALUES (?,?,?,?)",
            (self._clock(), kind, detail, None if raw is None else _j(raw)),
        )

    async def record_error(self, kind: str, detail: str, raw: Any = None) -> None:
        await self._error_nocommit(kind, detail, raw)
        await self.db.commit()

    async def errors(self, kind: str | None = None) -> list[tuple[str, str]]:
        q = "SELECT kind, detail FROM errors"
        args: tuple[Any, ...] = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        async with self.db.execute(q + " ORDER BY id", args) as cur:
            return [(str(r[0]), str(r[1])) async for r in cur]
