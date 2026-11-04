# DECISIONS

Log of non-obvious choices and of every place the spec and Kalshi's docs disagree, or
where a detail could not be verified. Newest entries at the bottom of each section.

## How docs were verified

- **Phase 1 (cloud sandbox):** docs.kalshi.com and the API hosts were blocked, so details
  came from web-search summaries. The uncertain ones were marked UNVERIFIED.
- **2026-11-04 (owner's Windows laptop):** every UNVERIFIED item was re-checked by reading
  docs.kalshi.com directly: the `.md` pages, `openapi.yaml` (v3.36.0), `asyncapi.yaml` and
  `llms.txt`. Public endpoints were also called live (demo and prod `/exchange/status`,
  `/markets`, `/series`, `/account/endpoint_costs`). Authenticated calls are still pending
  the owner's Key ID.

## Verified

| Item | Value | Source |
|---|---|---|
| REST hosts | demo `https://external-api.demo.kalshi.co/trade-api/v2`, prod `https://external-api.kalshi.com/trade-api/v2`. Legacy `demo-api.kalshi.co` and `api.elections.kalshi.com` still supported | getting_started/api_environments |
| WS hosts | demo `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`, prod `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | same |
| warp-ws hosts | `warp-ws[.demo].kalshi.co[m]` serve **only** `fill` + `user_orders` (lower latency); any other channel gives error 8. Same auth. Optional; not used yet | same |
| Separate keys | demo keys only work on demo, prod keys only on prod | same |
| Auth headers | `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE` | getting_started/api_keys |
| Signed message | `timestamp + METHOD + path`; path from `/trade-api/...`, **query string stripped** (resolves old UNVERIFIED #1) | api_keys, api_environments |
| **Key types** | **Ed25519 is recommended and is the UI default**; RSA-2048 with RSA-PSS/SHA-256/MGF1/salt=digest is also supported. Kalshi chooses by registered public key. The PEM header doesn't reveal the type, so detect it from the parsed key (resolves #2; spec was right) | api_keys |
| Orderbook REST | `orderbook_fp.yes_dollars` / `no_dollars`, `[price, count_fp]` string pairs, bids only, NO side in NO-leg prices | api-reference/market/get-market-orderbook |
| Orderbook WS | snapshot `msg.yes_dollars_fp` / `no_dollars_fp` (`[price, count_fp]`, keys absent when empty), plus `market_ticker`, `market_id`, `clock`. Delta `msg.price_dollars`, `delta_fp` (signed), `side` (yes/no), `clock`, optional `client_order_id`, `ts_ms`. `sid` + `seq` at top level (resolves #7; spec names were right) | asyncapi.yaml |
| WS seq | `seq` exists only on orderbook messages; `user_order` and `fill` messages have none. On a gap: `update_subscription` with `action: get_snapshot` (+ `market_tickers`), or unsubscribe/resubscribe. A repeated `subscribe` does NOT resync | asyncapi.yaml |
| WS keepalive | Kalshi pings every 10 s (body `heartbeat`); client must pong | websockets/connection-keep-alive |
| Price grid | `price_ranges[{start,end,step}]` is "the source of truth"; `step` is the tick. Off-grid prices are rejected. Don't key logic off `price_level_structure` (resolves #3) | getting_started/fixed_point_migration |
| Balance | `balance` (int cents), `balance_dollars` (string), `portfolio_value` (**int cents only; no `portfolio_value_dollars`**, resolves #5 and code fixed), `updated_ts` | api-reference/portfolio/get-balance |
| Positions | `GET /portfolio/positions` returns `market_positions` + `event_positions`, `cursor`; default `settlement_status=unsettled` (resolves #6; per-position field names get checked live in Phase 2) | get-positions |
| `/account/limits` | exists, auth: `{usage_tier, read{refill_rate,bucket_capacity}, write{...}, grants[]}` (resolves #4 path) | get-account-api-limits |
| `/account/endpoint_costs` | exists and is **public**: `{default_cost: 10, endpoint_costs[{method,path,cost}]}` | live call, both envs |
| Upgrade | `POST /account/api_usage_level/upgrade` (cost 30) grants permanent Advanced; criterion: ≥1 of last 100 orders via API | upgrade-account-api-usage-level |

### Rate limits (resolves #4)

- Token model. Default cost **10**. Two independent buckets: **Read** (GETs and anything not
  routed to Write) and **Write** (order create/amend/cancel/decrease, order groups, RFQ).
- **Basic: read 200 tokens/s, capacity 3 s (600); write 100 tokens/s, capacity 1 s (100;
  Basic write buckets don't bank idle time).** Advanced 300/300; Advanced+ write holds 3 s.
- `GET /portfolio/balance` has its **own** bucket: 1 call/s on Basic/Advanced, no burst.
- Non-default costs (live, 2026-11-04): V2 cancel / batch cancel / cancel-all = **2 per
  order**, `GET /portfolio/orders/:id` = 2, upgrade = 30, cfbenchmarks = 50.
- **Batches bill per item and don't save tokens.** The whole batch must fit in the bucket at
  once or the batch is rejected. So on Basic a create batch is at most 10 orders and a
  cancel batch at most 50. The client splits cancel batches automatically and refuses
  oversized create batches.
- 429 body `{"error": "too many requests"}` with **no Retry-After**; exponential backoff.
- Implementation: costs are fetched from `/account/endpoint_costs` at startup (config holds
  an offline copy); bucket sizes come from `/account/limits` once authenticated.

### Orders: V2 endpoints (spec conflict, docs win)

The spec describes `POST /portfolio/orders` with `side yes|no`, `action buy|sell`, `type`.
**That legacy endpoint has been deprecatable since May 6, 2026.** The bot uses only V2:

| Op | Endpoint | Notes |
|---|---|---|
| Create | `POST /portfolio/events/orders` | required: `ticker, side, count, price, time_in_force, self_trade_prevention_type` |
| Batch create | `POST /portfolio/events/orders/batched` | `{"orders": [CreateOrderV2Request...]}`; per-item `error` in response |
| Amend | `POST /portfolio/events/orders/{id}/amend` | `ticker, side, price, count` (count = **new total** = filled + desired resting), optional `expiration_time` (0 removes), `client_order_id`, `updated_client_order_id`. **Expiry-only amend (same price + count) keeps queue position** |
| Decrease | `POST /portfolio/events/orders/{id}/decrease` | `reduce_by` xor `reduce_to` (not used yet) |
| Cancel | `DELETE /portfolio/events/orders/{id}?market_ticker=` | returns `{order_id, client_order_id, reduced_by, ts_ms}` |
| Batch cancel | `DELETE /portfolio/events/orders/batched` | `{"orders": [{order_id, market_ticker}]}` |
| Cancel ALL | `DELETE /portfolio/events/orders` | **never used**: it would cancel the owner's manual orders too |
| Read | `GET /portfolio/orders`, `GET /portfolio/orders/{id}` | still the documented read paths |

Create fields: `ticker`, `client_order_id`, `side` = **`bid` (buy YES) | `ask` (sell YES =
buy NO at 1−p)**, `price` (YES price, fixed-point dollar string, 2–4 dp), `count`
(fixed-point string, ≤2 dp, fractional allowed where the market permits),
`time_in_force` ∈ {`good_till_canceled`, `immediate_or_cancel`, `fill_or_kill`},
`expiration_time` (unix **seconds**, used with `good_till_canceled`), `post_only`,
`self_trade_prevention_type` ∈ {`taker_at_cross`, `maker`} (required), `cancel_order_on_pause`,
`reduce_only` (only valid with IOC), `subaccount`, `order_group_id`, `exchange_index`
(omit to auto-route by ticker). The spec's `expiration_ts` is actually named
`expiration_time`.

Direction on responses uses `outcome_side` (yes/no) and `book_side` (bid/ask). The legacy
`action`/`side`/`is_yes`/`purchased_side` fields are deprecated.

### Fees (spec conflict, docs win)

- Spec: `ceil_to_cent(0.07 × C × P × (1−P))`. **Docs (fee_rounding):** trade fee = model fee
  **ceil to $0.000001**, then a rounding fee re-aligns the balance to $0.0001 for direct
  members, and a per-order accumulator rebates the over-payment. `fees.py` follows the docs.
  The ledger always records the exchange's actual per-fill `fee_cost`.
- Series `fee_type`: `quadratic` → **no maker fee**; `quadratic_with_maker_fees` → maker
  0.25×; `quadratic_with_combo_maker_fees` → 0.5×; `flat` → separate table. Times
  `fee_multiplier`. Unknown types are refused. All active daily weather series are
  `quadratic` ×1, so makers pay 0.

### Weather settlement source (spec conflict, matters for Phase 3)

The spec says weather settles on the NWS Daily Climate Report (CLI). **The live market rules
say otherwise.** Example (KXLOWTSATX, 2026-11-05): *"…the minimum temperature recorded at
San Antonio (CLISAT)… according to **The Weather Company**"*, and the series'
`settlement_sources` = The Weather Company (weather.com/kalshi). Rules warn that preliminary
values may differ by rounding/conversion, and an erroneous publication can delay expiry.
The NWS-sourced series (KXLOWNY, KXLOWCHI, KXHIGHHOU…) have **no open markets**. Phase 3
must model The Weather Company's reported integer for the named station, and parse the
source from each market's rules rather than assuming it. NWS/Open-Meteo remain *inputs*
to the forecast. The CLI "climate day" DST rule needs re-checking against TWC's definition
before it's coded. **I'll raise this at the Phase 3 start.**

### Live demo results (2026-11-04, authenticated)

- Ed25519/RSA signing accepted; `GET /account/limits` → tier `basic`, read 200/s cap 600,
  write 100/s cap 100, exactly as documented.
- `/account/endpoint_costs` → 19 non-default costs loaded.
- Demo has 4 exchange shards (index 0–3); orders auto-route by ticker.
- Demo KXHIGHNY uses the tapered grid (0.001 ticks below $0.10 and above $0.90, 0.01
  between). Sub-cent ticks are real on weather markets.
- The V2 create body passed validation and failed only on `insufficient_balance` (demo
  balance $0). The order round-trip test waits for demo funds.
- The unfiltered `/markets` list is dominated by KXMV combos, which 404 on
  `GET /markets/{ticker}`. `smoke` now defaults to `--series KXHIGHNY`.

### Still unverified (needs the authenticated demo run)

1. Duplicate `client_order_id` on create → assumed **409 Conflict** (the spec lists 409 for
   create). `create_order_idempotent` treats 409 as "already exists → look it up".
2. Max `client_order_id` length. The bot sends 36 chars (`kmb-` + 32 hex).
3. `GET /portfolio/orders` has no `client_order_id` filter for direct members (only the FCM
   endpoint has `client_order_ids`), so lookup scans the ticker's orders.
4. Live confirmation that `use_yes_price` behaves as documented (Phase 2 WS test).

## Section 14 answers (owner, 2026-11-04)

1. **Weather cities:** proposed **KXHIGHNY, KXHIGHMIA, KXHIGHCHI** (top 3 daily-high
   series by prod 24h volume; LAX is the 4th). Awaiting confirmation.
2. **Risk limits:** confirmed as tabled for $500; start with **3 markets**.
3. **Fill alerts:** batched every **15 min**; risk/kill/error alerts instant (`alerts` config).
4. **Dashboard:** yes, **local-only and read-only**, after Telegram, low priority.
5. **VPS:** later, **Phase 9 only**. Runs on the owner's Windows laptop for now.
6. **Tax lots:** **FIFO**.
7. **Exclusions:** no changes.

## Windows / laptop decisions (2026-11-04)

- **Key permissions.** On Windows, `chmod 600` means nothing. The check reads the key file's
  DACL through the Win32 API (ctypes, no new dependency) and requires: a protected DACL (no
  inheritance), and allow-entries **only** for the current user's SID. SYSTEM and
  Administrators are refused too, because "owner-only" is meant literally. Deny entries are
  allowed. The error message prints the exact `icacls` fix. POSIX keeps the mode-600 + owner
  check for the future VPS.
- **Sleep prevention.** `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)` is held
  by the main thread for the bot's lifetime (replacing `caffeinate`). It blocks idle sleep
  only. Lid-close or manual sleep still suspends, which is what the continuity monitor and
  exchange-side expiry cover.
- **Continuity monitor.** About 1 Hz tick. A monotonic gap > 15 s = frozen. On Windows,
  `time.monotonic` counts through sleep, and comparing with `QueryUnbiasedInterruptTime`
  (which doesn't) labels it suspend vs stall. |Δwall − Δmono| > 30 s = clock jump. The
  network layers report drops.
- **Safety gate.** Starts closed. Any trip stops quoting immediately. Recovery order is
  fixed: exchange reachable → **cancel all bot orders** (by client_order_id prefix, via
  batch cancel) → re-snapshot books → reconcile → resume. A trip during recovery restarts
  it. A fresh start runs the same path.
- **Order expiry.** Every order is `good_till_canceled` + `expiration_time = now + 600 s`;
  the bot never sends an order without one. While the gate is open, expiries are pushed
  forward with expiry-only amends once less than 360 s remain (refresh every 240 s). Config
  validation requires refresh ≤ expiry/2. Cost on Basic: ≤40 orders × 10 tokens per 4 min,
  well under 1% of the write budget.
- **`cancel_order_on_pause: true`** on every order, so the exchange pulls quotes on a pause.
- **Self-trade prevention `taker_at_cross`.** It cancels the incoming order and leaves
  resting quotes and their queue position alone. post_only would reject a cross anyway.
- **Clock drift.** The `Date` header has 1 s resolution, so six samples at different
  sub-second phases are intersected to bound the offset. The bot warns only when the bound
  *certainly* exceeds 1 s. Phase 2 adds a continuous check from WS `sending_ts_ms` (ms
  resolution). On 2026-11-04 the laptop was 0.5–0.8 s behind, and the **Windows Time
  service was not running** (owner to enable).
- **weather.gov** returns 403 without a `User-Agent`. The weather client must send one.
- **Scripts** are PowerShell (`scripts/setup.ps1`, `scripts/check.ps1`); README commands
  are PowerShell.

## Design choices (Phase 1)

- **Layout.** The repo root is the spec's `kalshi_maker/` directory, and the spec's `bot/`
  package is `kalshi_maker/` (importable as `kalshi_maker.*`).
- **Python 3.12** (`py -3.12` on Windows).
- **pydantic-settings** (official pydantic org package) loads `.env`.
- **No respx.** Tests use httpx's built-in `MockTransport`.
- **YAML money as strings.** Risk limits are quoted strings so they never pass through float.
- **Config is strict.** Unknown keys are rejected.
- **Limit coherence checks.** order ≤ market ≤ event ≤ category ≤ capital; daily ≤ weekly ≤
  drawdown ≤ 50% of bankroll.
- **Required exclusions.** Config refuses to load if Section 6 bans are removed.
- **Retries.** GETs only. Writes are never auto-retried. An ambiguous create (timeout/5xx/409)
  is looked up by client_order_id and **never re-sent**. If it's not found, it's reported as
  outcome-unknown, and the next reconcile or its exchange expiry resolves it.
- **Bot order identity.** `client_order_id = "kmb-" + uuid4().hex`. Only orders matching
  that exact pattern are ever cancelled by the bot.
- **Prod gate.** ENV=prod + LOCATION_OK + I_ACCEPT_REAL_MONEY_RISK + a typed phrase on first
  start.
