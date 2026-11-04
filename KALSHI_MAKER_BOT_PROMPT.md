# MEGA PROMPT — Kalshi Maker Bot (paste this whole file into Claude Code)

You are building a production-grade, **maker-only** automated trading bot for **Kalshi** (CFTC-regulated event-contract exchange). I am the owner and only user. Read this entire document before writing any code. Follow it literally. When something here conflicts with Kalshi's live documentation, **the live docs win**. Tell me about the conflict and update the code to match the docs.

---

## 0. Ground rules for you (Claude Code)

1. **Verify every API detail against the live docs before coding it.** Sources, in priority order:
   - OpenAPI / REST reference: https://docs.kalshi.com (api-reference pages, e.g. `/api-reference/portfolio/create-order`)
   - WebSocket AsyncAPI spec: https://docs.kalshi.com/asyncapi.yaml
   - Environments: https://docs.kalshi.com/getting_started/api_environments.md
   - Rate limits: https://docs.kalshi.com/getting_started/rate_limits
   - Auth: https://docs.kalshi.com/getting_started/api_keys
   - Fee schedule: kalshi.com fee schedule (check per-series fee multipliers)
   - The official Python SDK, if it exists and is current. Do **not** copy 2024-era tutorials or random GitHub repos. Field names and hosts have changed.
2. **Do not invent field names.** If you can't confirm a field, stop and ask me, or write a clearly marked `# UNVERIFIED` stub plus a test that fails loudly.
3. **Build in phases (Section 12).** Run tests at the end of every phase. Do not move to live trading code paths until the paper/demo phases pass.
4. **Ask me questions when a decision is mine** (Section 14). Don't guess on money, limits or credentials.
5. **Never** handle my real credentials in chat. I put them in `.env` myself.
6. Keep a `DECISIONS.md` log of every non-obvious choice and every doc conflict you found.

---

## 1. Who I am / context that changes the design

- US citizen, **Nevada (Reno) address** on the account. Kalshi geofences Nevada users out of **sports, politics and entertainment** contracts (court order, in force since Aug 12, 2026). The bot must **never** select those categories. Hard-code a category deny-list **and** handle the API rejecting them gracefully.
- **Polymarket US is blocked in Nevada.** No cross-venue arbitrage. Kalshi only.
- Kalshi eligibility is based on **physical location**. VPNs violate the terms. The bot must have a `LOCATION_OK` flag in config, defaulting to `false`. When it's false, the bot refuses to place real orders (demo still works). I flip it myself when I'm physically in the US. Print a clear startup warning about this.
- Accounting/finance student. I care about **clean books**. Every fill must go to a ledger I can turn into tax records (Section 9).
- Bankroll: **$500**. Tight risk limits (Section 7).
- Runs 24/7 on a **cheap cloud VPS** (Hetzner CX22 / DigitalOcean $6 droplet, Ubuntu 24.04). It must also run locally for development.
- Alerts and remote control through **Telegram** (Section 10).

---

## 2. The strategy thesis (why maker-only)

Research basis, so you understand the *why*:
- Across 72.1M Kalshi trades, **makers earned about +1.12% excess return and takers about −1.12%** (Becker). The gap is ~2.2–2.7pp in weather/crypto/sports and only ~0.17pp in finance/econ.
- **Favorite–longshot bias:** cheap contracts are overpriced. 5¢ contracts won about 2% of the time, while 95¢ contracts won about 98% (Bürgi/Deng/Whelan, CEPR). A 1¢ YES had −41% EV and a 1¢ NO had +23%. NO beat YES at 69 of 99 price levels.
- Prices are close to perfectly calibrated **within ~1 hour of resolution**, so there's little edge in buying near-certain outcomes right before expiry.
- **The edge flips with market regime.** Makers lost before 2024. So the bot must measure its own edge live (Section 8) and stop itself if the edge disappears.
- Taker fee = `ceil_to_cent(0.07 × contracts × P × (1−P))`, about 1.75¢/contract at 50¢. Most series charge makers **0**, but some don't. Verify per series.

**Therefore: the bot only ever posts resting, `post_only` limit orders.** It never crosses the spread. The edge comes from (a) collecting the spread, (b) leaning against the longshot bias, and (c) better fair values in weather and crypto.

---

## 3. Two switchable strategy modes (per-market, set in config)

### Mode A — `tilt` (favorite / NO tilt, default)
- Rest **bids only** on the side the bias favors: the favorite (YES when fair ≥ ~0.70) or NO on longshots (YES fair ≤ ~0.30).
- Bid price = `min(fair_value − edge_buffer, best_bid + 1 tick)`, rounded **down** to the market's tick. Never bid at or above best ask (post_only would reject it anyway; check before sending).
- `edge_buffer` is configurable per category. Default 2¢; widen for thin books.
- Never buy **YES** under `min_yes_price` (default 0.10). Never buy **NO** when NO is under 0.10 either (that's a longshot too).
- Optional take-profit: rest a sell (exit) order at `entry + target` when inventory exists.

### Mode B — `mm` (two-sided Avellaneda–Stoikov market making)
- Reservation price: `r = fair − q·γ·σ²·τ`
  - `q` = signed inventory in contracts (YES positive)
  - `γ` = risk aversion
  - `σ²` = variance of fair value per unit time, estimated from rolling mid changes in probability space
  - `τ` = time to close as a fraction of the horizon
- Half-spread: `δ = (γ·σ²·τ)/2 + (1/γ)·ln(1 + γ/κ)`, where `κ` = order-arrival intensity, estimated from the fill rate.
- Floor the spread at `min_spread_ticks` (default 2 ticks) and cap it at `max_spread`.
- **Inventory skew sign must be unit-tested.** Long YES inventory must lower **both** quotes. (A public blowup came from a flipped sign.) Write a test that fails if it's wrong.
- Clamp quotes inside [tick, 1 − tick] and never cross the book.
- When `|q| ≥ max_inventory`, quote only the side that reduces inventory.
- Bounds-check every parameter at load. Reject a σ that is 100× off. (Another public blowup came from volatility set 100× too low.)

Both modes share: fair-value engine, risk manager, order manager, ledger.

---

## 4. Fair-value engines (by category)

Each engine returns `FairValue(p: Decimal, confidence: 0–1, source: str, as_of: datetime)`. If confidence is below threshold or data is stale, **do not quote that market**. Quote only off engines you trust. Never fall back to "just use mid" for placing tilt orders unless the engine is explicitly `market_implied`.

### 4.1 Weather (highest priority — build first)
- Markets: daily high/low temperature series (e.g. `KXHIGH*` cities). Discover via the series/events endpoints; don't hard-code tickers.
- **Settlement source = NWS Daily Climate Report (CLI) for the specific station** named in the market rules. Parse each market's rules text to get the station and the strike/bracket. Unit-test the parser on several real markets.
- Data:
  - NWS API (api.weather.gov) forecast and observations for that station's grid point
  - Open-Meteo ensemble API (GFS 31 members + ECMWF ensemble)
  - Hourly observations (METAR) to track the running max/min so far today
- Model: empirical distribution from ensemble members, bias-corrected per station using a rolling forecast-error history (store it). Blend with the NWS point forecast. Once the day is underway, **condition on the observed running max/min**. (If it's already 78°F at 2pm, P(high ≥ 75) = 1.)
- **Gotchas the code must handle (each one unit-tested):**
  - During daylight saving time the climate "day" runs **1:00 AM to 12:59 AM local standard time**, not midnight to midnight.
  - CLI reports can be delayed or revised. Settlement can lag.
  - °C→°F conversion and rounding can shift the reading by 1°F. Model the reported integer, not raw sensor values.
  - Phone weather apps are not the settlement source.
- Stop quoting a weather market when the observed value makes the outcome effectively certain (inventory risk with no edge) or when the forecast spread is collapsing fast near close.

### 4.2 Crypto ranges (BTC/ETH)
- Fair value = lognormal/risk-neutral probability of finishing in the range, using spot plus implied vol (Deribit DVOL or near-expiry option IV via public API), with a realized-vol fallback.
- **Settlement index** is defined in market rules (e.g. a CF Benchmarks index). Use the same reference and averaging window the rules specify. Kalshi's WS exposes `cfbenchmarks_value` / `pyth_value` channels; check the docs and use them if available.
- Pull quotes on spot moves greater than X% in Y seconds (configurable). You will lose latency races, so don't try.
- Default **disabled** until weather is proven in demo.

### 4.3 Econ / finance (CPI, Fed, jobs, index ranges)
- The most efficient category. Default mode = `tilt` only, with a wide buffer.
- Fair value: Cleveland Fed inflation nowcast for CPI, market-implied for Fed decisions, and a longshot-bias-adjusted market-implied model otherwise.
- **Hard rule: cancel all quotes in a series N minutes before a scheduled release** (default 30 min; CPI and jobs come out 8:30 AM ET). Keep a release calendar in config, and refresh it from a source if you can.
- Default **disabled** until weather is proven.

### 4.4 "Any open market" scanner + `market_implied` engine
- Scans all open markets in **allowed** categories and ranks them by: spread width, top-of-book depth, 24h volume, time to close (prefer 6h–14d), and price away from 50¢. Exclude everything in Section 6.
- `market_implied` fair = mid, adjusted toward the empirical favorite–longshot calibration curve (make it a configurable lookup table; seed it from the research numbers above and refine it from the bot's own fills).
- Tilt mode only. Small size. Default **disabled**.

---

## 5. Exchange integration (verify everything against docs)

- **Hosts** (confirm against docs):
  - Prod REST: `https://external-api.kalshi.com/trade-api/v2`
  - Demo REST: `https://external-api.demo.kalshi.co/trade-api/v2`
  - Prod WS: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
  - Demo WS: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`
  - Legacy hosts (`api.elections.kalshi.com`, `demo-api.kalshi.co`) still work. Make the hosts configurable.
  - Demo and prod use **separate keys**.
- **Auth:** headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`. Sign `timestamp + METHOD + path`.
  - The path includes `/trade-api/v2`, with **no host and no query string**.
  - For WS, sign the path `/trade-api/ws/v2`.
  - Support **Ed25519** (preferred) and RSA-PSS SHA-256. Test signing against a known vector if the docs give one.
- **Prices and quantities are strings in `_dollars` / `_fp` fields** (e.g. `"0.9600"`, `"278.00"`). Parse with `Decimal` only. **Never use float for money, price or size.** Sub-cent ticks (0.5¢) exist in some markets. Read each market's tick/price structure from the market object and round to it. Don't assume 1¢.
- **WebSocket order book:**
  - Channel `orderbook_delta` delivers `orderbook_snapshot` then `orderbook_delta` messages. Snapshot has `yes_dollars_fp` / `no_dollars_fp` as `[price, size]` string pairs. Delta has `price_dollars`, `delta_fp` (signed) and `side`.
  - Kalshi books are **bids only on each side**: YES ask = 1 − best NO bid. Implement and test this conversion.
  - Track `seq` **per subscription (`sid`)**. On any gap, resync (the `get_snapshot` command, or unsubscribe then resubscribe) and **replace** the local book. Don't apply deltas on top of a stale book.
  - Ignore unknown message types and fields (forward compatibility).
  - Also subscribe to `fill` and `user_orders` channels for my own orders.
- **Orders:**
  - `POST /portfolio/orders` with `ticker`, `side`, `action`, `count`, `type: limit`, price (use whichever price field the current docs specify), `client_order_id`, **`post_only: true` always**, `time_in_force` / `expiration_ts`, and `self_trade_prevention_type`.
  - Use **batch create/cancel and amend** endpoints if they exist, to save rate-limit tokens.
  - Use `order_group_id` if order groups let you cap exposure exchange-side. Check the docs.
- **Idempotency:** every order gets a deterministic `client_order_id` (uuid4, stored before sending). On timeout, **query by client_order_id before retrying**. Never blind-resend.
- **Rate limits:** start at Basic (200 read / 100 write tokens per second). Implement a token-bucket limiter using the per-endpoint costs from the docs. On 429, exponential backoff with jitter. Provide a CLI command to call the "upgrade to Advanced" endpoint, but don't run it automatically.
- **Startup reconciliation:** fetch balance, positions, resting orders and recent fills. Rebuild state from the exchange, which is the source of truth. Cancel orphan orders the bot doesn't recognize **only if** they carry the bot's `client_order_id` prefix. Never touch my manual orders.

---

## 6. Hard exclusions (never trade these)

- Categories: **sports, politics, elections, entertainment, media/mentions** (Nevada geofence plus insider-risk markets).
- **Multivariate / combo / parlay tickers** (e.g. `KXMV*`, anything flagged multivariate). Filter on the API flag **and** a ticker regex. You can't exit these. (Real blowup cause.)
- Markets closing in under `min_time_to_close` (default 60 min), or with no known close/settlement time.
- Markets with top-of-book depth or 24h volume below threshold.
- Any series where the maker fee multiplier is above 0, unless I explicitly allow it in config.
- Perps / margin products.
- Anything a fair-value engine can't price with confidence.

---

## 7. Risk management (non-negotiable; all enforced in one `RiskManager` that every order must pass)

Defaults for a $500 bankroll (all configurable; validated at load; refuse to start if absurd):

| Limit | Default |
|---|---|
| Max capital deployed (resting + positions) | $300 (60%) |
| Max cost basis per market | $50 |
| Max per event (correlated strikes/brackets in one event) | $75 |
| Max per category | $150 |
| Max per single order | $15 |
| Max resting orders total | 40 |
| Max markets quoted at once | 10 (start with 3 in demo) |
| **Daily realized + unrealized loss stop** | **−$25 → cancel all, stop for the day, alert** |
| Weekly loss stop | −$60 → stop until I resume manually |
| Max drawdown from peak equity | −$100 → full kill, manual restart only |
| Max orders per minute | 30 |
| Max consecutive API errors | 5 → pause 5 min, alert |
| Stale data (no book update / fair value older than X) | cancel that market's quotes |

Additional rules:
- **Kill switch:** Telegram `/kill`, a local `KILL` file, or SIGTERM, any of which cancels all bot orders and halts. Test it.
- **Dead-man switch:** if the main loop stalls more than 60s, a watchdog cancels all orders. Use exchange-side order expirations too, so quotes expire automatically if the bot dies.
- Mark positions to market with conservative prices: the best bid for longs, not mid.
- **Never** place a market order or cross the spread. No "emergency flatten" by taking liquidity unless `allow_taker_flatten: true` (default false), and even then only with my Telegram confirmation.
- Correlation: strikes and brackets in the same event are one risk bucket.
- The bot stops quoting a market the moment its position hits the cap, and quotes only the reducing side.

---

## 8. Self-measurement (edge monitoring)

- For every fill, log the fair value at fill time and later the settlement outcome. Compute per category and per mode:
  - realized P&L
  - fill rate
  - **adverse selection** (mid move 1m / 5m / 30m after fill against you)
  - average edge captured vs fair
  - Brier score / calibration of the fair-value engine
- **Auto-disable rule:** if, after ≥ 50 settled fills in a category, P&L or fair-value calibration is significantly negative, disable that category and alert me.
- Daily summary to Telegram: P&L, fees, exposure, fills, adverse-selection stats, any auto-disables.

---

## 9. Ledger & tax records (I'm an accounting student; make this clean)

- SQLite database (WAL mode) with tables for: orders, fills (including fee_cost), positions snapshots, settlements, balance snapshots, fair-value snapshots, config versions, and errors.
- **Append-only** for fills and settlements. Store raw exchange JSON alongside parsed fields.
- Nightly reconciliation against the exchange's fills/settlements endpoints. Any mismatch gets alerted.
- `export-tax` CLI: CSV per tax year with date acquired, date closed/settled, ticker, side, quantity, cost basis including fees, proceeds, gain/loss, and holding period. Use FIFO lot matching, with the lot method configurable. (Kalshi may not issue a full 1099-B, so this is my record. IRS treatment is unsettled. Don't encode a tax opinion; just produce clean data.)
- Daily equity curve CSV.

---

## 10. Telegram bot (alerts + control)

- Use the Bot API with a whitelisted `chat_id` only. Ignore every other sender.
- **Alerts:** startup/shutdown, fills (batched every N min), risk-limit hits, kill events, API errors, WS resyncs, daily summary, auto-disables.
- **Commands:**
  - `/status` (equity, P&L today, exposure by category, markets quoted)
  - `/positions`, `/orders`
  - `/pause`, `/resume`
  - `/kill`
  - `/mode <market|category> <tilt|mm|off>`
  - `/limits`
- Any command that **increases** risk (resume after a weekly stop, raise limits, enable taker flatten) requires a confirm step, and the dangerous ones are not exposed at all. Limit increases happen only in the config file.
- Telegram failure must **never** crash or block trading. Use a queue with drop-oldest.

---

## 11. Engineering requirements

- **Python 3.12**, asyncio. Dependencies (pin exact versions, minimal, well-known only): `httpx`, `websockets`, `cryptography`, `pydantic` v2 (settings and models), `pyyaml`, `structlog` or stdlib logging (JSON), `aiosqlite`, `numpy`, `scipy` (vol/lognormal), `python-telegram-bot` or raw httpx, `pytest`, `pytest-asyncio`, `hypothesis`, `ruff`, `mypy`. **No unvetted packages.** A fake Polymarket bot repo stole keys in 2026 via typosquatted packages. Check every package name.
- Suggested layout:

```
kalshi_maker/
  config/ (config.example.yaml, release_calendar.yaml, calibration_curve.yaml)
  bot/
    main.py           # orchestrator, graceful shutdown
    settings.py       # pydantic config + validation
    auth.py           # Ed25519 / RSA-PSS signing
    rest.py           # REST client, rate limiter, retries, idempotency
    ws.py             # WS client, seq tracking, resync, reconnect w/ backoff
    book.py           # local order book (Decimal), yes/no conversion
    markets.py        # discovery, rule parsing, exclusions, scanner
    fees.py           # fee math (ceil to cent, per-series multipliers)
    fair/ weather.py crypto.py econ.py implied.py base.py
    strategy/ tilt.py mm_avellaneda.py base.py
    orders.py         # order manager: diff desired vs live quotes → place/amend/cancel
    risk.py           # RiskManager (single gate)
    ledger.py         # SQLite, reconciliation, tax export
    metrics.py        # edge monitoring, auto-disable
    telegram.py
    watchdog.py
  backtest/ (replay engine using historical trades/candles, conservative fill model)
  tests/
  deploy/ (Dockerfile, docker-compose.yml, systemd unit, setup_vps.sh)
  DECISIONS.md, README.md, RUNBOOK.md
```

- **Order manager pattern:** each tick, the strategy outputs *desired quotes*. The order manager diffs them against live orders and sends the minimal set of amend/cancel/place calls. Don't cancel and replace everything every tick (it wastes rate limit and loses queue priority). Add hysteresis so tiny fair-value changes below one tick don't move quotes.
- Money is `Decimal` everywhere, with an explicit rounding mode: bids round **down** to tick, asks round **up**.
- Clock: all internal times in UTC, aware datetimes. Weather logic converts to station local *standard* time explicitly.
- Config: `config.yaml` plus `.env` for secrets (`KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`). `.env` and key files go in `.gitignore`. The private key file needs `chmod 600`, and the bot refuses to start if permissions are looser.
- `ENV=demo` is the default. Prod requires **all** of: `ENV=prod`, `LOCATION_OK=true`, `I_ACCEPT_REAL_MONEY_RISK=true`, and a typed confirmation on first prod start.
- Structured JSON logs with rotation. No secrets in logs; redact the signature headers.
- Graceful shutdown cancels all bot orders.

### Testing (required, must pass before each phase ends)
- Unit tests:
  - fee math, including rounding edge cases
  - tick rounding with 0.5¢ ticks
  - YES/NO book conversion
  - seq gap → resync
  - **A-S inventory skew sign**
  - parameter bounds
  - risk limits (property-based with hypothesis: no sequence of fills or quotes can breach a cap)
  - exclusions (combo tickers, banned categories)
  - weather rule parser and DST day boundary
  - lognormal range probability
  - FIFO tax lots
  - kill switch and watchdog
- Integration tests against the **demo** API: auth works, place a post_only order far from the market, amend it, cancel it, reconcile.
- **Backtest/replay:** conservative fill model. Assume you're last in queue, fill only when price trades *through* your level, and include adverse selection. There's no public historical order-book depth, so state clearly that backtests overstate maker fills.

---

## 12. Build phases (stop after each one and show me results)

1. **Skeleton + auth + REST read-only:** balance, markets, orderbook on demo. Settings validation. Tests.
2. **WS book + reconciliation + ledger:** live local books with seq handling. Ledger writes.
3. **Weather fair-value engine:** run it in "shadow mode" (log fair vs market for a few days; no orders). Show me a calibration report.
4. **Risk manager + order manager + tilt strategy on demo:** 3 weather markets, tiny size.
5. **Telegram + watchdog + kill switch.** Prove the kill switch works on demo.
6. **A-S market-making mode** on demo, with skew-sign tests.
7. **Metrics / auto-disable / daily reports / tax export.**
8. **Crypto, econ and scanner engines:** each one shadow mode, then demo, separately.
9. **Deploy:** Dockerfile and systemd, a `setup_vps.sh` for Ubuntu 24.04 (non-root user, ufw, unattended-upgrades, time sync via chrony), and a RUNBOOK.md (start, stop, kill, rotate keys, read logs, restore from DB).
10. **Prod readiness checklist** (Section 13). Do not enable prod yourself. I do that.

---

## 13. Go-live checklist (bot prints it; I tick it manually)

- [ ] ≥ 2 weeks demo with no unexplained errors, and the kill switch tested
- [ ] Weather fair-value calibration acceptable in shadow mode
- [ ] API key created **without withdraw permission**
- [ ] I am physically in the US (`LOCATION_OK=true`)
- [ ] Start prod at **half** the default limits for the first week
- [ ] Ledger reconciles exactly with the exchange for 7 straight days

---

## 14. Questions you must ask me before/while building (don't assume)

1. Which weather cities/series should the bot start with? (Suggest the 3 most liquid, based on what you find via the API.)
2. Should I confirm the risk-limit table above or change it?
3. Telegram: I'll create the bot with @BotFather and give you the chat_id. Do you want batched or instant fill alerts?
4. Do you want a lightweight local web dashboard (FastAPI + simple HTML) in addition to Telegram?
5. Which VPS provider/region? (Latency doesn't matter much for this strategy; a US region is fine.)
6. Lot method for tax export (FIFO default)?
7. Anything in Section 6's exclusions you want to change?

---

## 15. Things you must NOT do (summary)

- Do not place taker or market orders, or cross the spread.
- Do not trade sports, politics, entertainment, media or combo/multivariate markets.
- Do not use floats for money. Do not assume 1¢ ticks or integer-cent price fields.
- Do not copy field names from old blogs or repos. Verify against live docs.
- Do not blind-retry order submissions. Query by client_order_id first.
- Do not apply WS deltas after a seq gap without a fresh snapshot.
- Do not run prod by default. Do not touch orders the bot didn't create.
- Do not add a VPN or geolocation workaround of any kind.
- Do not add dependencies you haven't verified on PyPI by exact name.
- Do not let Telegram, logging or metrics failures stop risk controls or order cancellation.
- Do not claim the bot is profitable. Report measured numbers only.
- Do not skip tests to "move faster."

---

## 16. Honest expectations (put this in the README)

- Research shows **no audited profits for any retail Kalshi bot**. The maker edge is real but thin (~1–2.7pp by category), and it has flipped before.
- Most retail blowups came from **bugs and missing risk controls**, not bad ideas. This spec exists to prevent those.
- Treat the first months as paid tuition in market microstructure. Success = no blowups, clean books and a measured edge.
