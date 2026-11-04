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

### Weather settlement rules: exact wording (pulled from the API 2026-11-04)

Pulled with `GET /series/{t}` and `GET /markets?series_ticker={t}&status=open` on **prod and
demo** for KXHIGHNY, KXHIGHMIA and KXHIGHCHI. All 72 open markets (3 series x 2 envs) share
one template, with one `rules_secondary` text and identical series metadata. Demo matches
prod exactly.

**Series metadata (all three):**
`settlement_sources = [{"name": "The Weather Company", "url": "https://weather.com/kalshi"}]`,
`contract_terms_url = https://assets.kalshi.com/contract_terms/GLOBALTEMPERATURE.pdf`,
`fee_type = "quadratic"`, `fee_multiplier = 1`, `frequency = "daily"`.

**`rules_primary`, verbatim (KXHIGHNY, 2026-11-04 markets):**

> [greater, floor 77] "If the maximum temperature recorded at New York City (CLINYC) for Nov 4,
> 2026, is greater than 77° fahrenheit according to The Weather Company, then the market
> resolves to Yes."  (`yes_sub_title`: "78° or above")
>
> [less, cap 70] "If the maximum temperature recorded at New York City (CLINYC) for Nov 4,
> 2026, is less than 70° fahrenheit according to The Weather Company, then the market
> resolves to Yes."  (`yes_sub_title`: "69° or below")
>
> [between, 70-71] "If the maximum temperature recorded at New York City (CLINYC) for Nov 4,
> 2026, is between 70-71° fahrenheit according to The Weather Company, then the market
> resolves to Yes."  (`yes_sub_title`: "70° to 71°")

Miami and Chicago are identical except for the station: "Miami (CLIMIA)", "Chicago (CLIMDW)".

**`rules_secondary`, verbatim (all markets):**

> "Not all weather data is the same. While checking a source like AccuWeather or Google
> Weather may help guide your decision, the official and final value used to determine this
> market is the maximum/minimum temperature as reported by the Weather Company.
>
> Reading weather data
> Preliminary Weather Company data may be subject to rounding and conversion differences from
> the final reported value.
> Use caution when interpreting preliminary Weather Company readings.
>
> If it is determined in the sole discretion of the Exchange that there is a material error in
> the initial non-preliminary publication, Expiration may be held until the publication of a
> non-materially-erroneous data revision or until the Expiration Date, whichever comes first.
> If no data is available at the end of this period, all markets will resolve to the last fair
> price determined by Kalshi. Please note, a status of "Final" on weather.com/kalshi is
> provided for convenience only, and does not guarantee data to be error-free and thus exempt
> from the above."

**`early_close_condition` (sample KXHIGHNY-26NOV05-T77):**

> "The Last Trading Time will be 11:59 PM local time on November 05, 2026 regardless of any
> data releases or events occurring. Expiration will occur on the sooner of the first 7:00 or
> 8:00 AM ET following the release of the data for November 05, 2026, or one week after
> November 05, 2026."

API timing fields for that market: `close_time 2026-11-06T06:00:00Z` (NY, MIA) /
`07:00:00Z` (CHI), `expected_expiration_time 2026-11-06T20:00:00Z`,
`latest_expiration_time 2026-11-12T15:00:00Z`.

**Contract terms PDF (GLOBALTEMPERATURE, linked from the series), relevant clauses verbatim:**

> "Source Agency: The Source Agencies are, in hierarchical order, The Weather Company (TWC),
> National Weather Service, the national weather service for <area> ..."
>
> "'Above' means greater than (>), 'below' means less than (<), ... and 'between' means within
> an inclusive range (≥ lower bound and ≤ upper bound)."
>
> "<measurement units>: ... Where relevant, such units may be defined or rounded to one decimal
> place."
>
> "Only the first official non-preliminary report published by the Source Agencies that
> includes the relevant data will be used for resolution ..."
>
> "Contract resolution is based on the full precision reported by the Source Agency. Rounding
> by media outlets, secondary reporting, or third-party summaries does not affect resolution."
>
> "Last Trading Date: ... the final day of <time period>. The Last Trading Time will be one
> minute prior to the final time in <time period>."

**Station codes, confirmed via api.weather.gov (not memory):** `CLINYC`, `CLIMIA`, `CLIMDW`
are the NWS Daily Climate Report product IDs for **Central Park NY** (office OKX), **Miami**
(MFL) and **Chicago-Midway** (LOT; *not* O'Hare). Those reports give the daily max as an
**integer °F** with times labelled **(LST)**. `weather.com/kalshi` is a JavaScript app titled
"Official Climate Reports: verified climate observations from official government climate
stations" with a "Daily Climate" tab. It has no documented API.

### Settlement-wording ambiguities: RESOLVED by the owner 2026-11-04 (see Phase 3)

What is clear: the source is The Weather Company (NWS second in the hierarchy). The
stations are the NWS CLI stations above. "greater than X" is strictly > X, "less than X" is
strictly < X, and "between A-B" is inclusive. Demo and prod agree.

What is NOT stated anywhere in the API text or the contract terms:

1. **Precision of the reported value.** "Full precision reported by the Source Agency" plus
   "may be ... rounded to one decimal place", but the strikes only partition **integers**.
   Checked on every 2026-11-05 event: each integer hits exactly one bracket, while 71.5,
   73.5 and 75.5 (NY), 83.5/85.5/87.5 (MIA) and 56.5/58.5/60.5 (CHI) hit **no** bracket. The
   `yes_sub_title`s ("78° or above" for "greater than 77°") also assume integers. The NWS CLI
   reports are integers. TWC's final precision is not stated.
2. **The day window behind "for Nov 4, 2026".** Not defined. The NWS CLI "day" is local
   standard time (during DST, 1:00 AM to 12:59 AM local daylight time). The text says trading
   ends "11:59 PM local time", but the API `close_time` is **1:00 AM local standard time**
   (06:00Z for EST, 07:00Z for CST). Today (standard time until 2027-03-14) the LST day and
   the local calendar day coincide, so this only matters from March.
3. **Which number TWC publishes.** Is TWC's "Daily Climate" value the NWS CLI value
   republished, or TWC's own observation for the same station? The page says "official
   government climate stations", which suggests the former, but it isn't stated, and the
   data feed is undocumented.

**Owner's decision (2026-11-04):** all three proposals approved; see "Phase 3 decisions" below.

### Live demo results (2026-11-04, authenticated)

- Ed25519/RSA signing accepted; `GET /account/limits` → tier `basic`, read 200/s cap 600,
  write 100/s cap 100, exactly as documented.
- `/account/endpoint_costs` → 19 non-default costs loaded.
- Demo has 4 exchange shards (index 0–3); orders auto-route by ticker.
- Demo KXHIGHNY uses the tapered grid (0.001 ticks below $0.10 and above $0.90, 0.01
  between). Sub-cent ticks are real on weather markets.
- The V2 create body passed validation and failed only on `insufficient_balance` (demo
  balance $0). After funding ($500 demo), **the order round-trip passed 3/3 times**:
  post-only 1-contract YES bid at the lowest tick (create returned `fill_count 0.00`,
  `remaining_count 1.00`), expiry-only amend (the new `expiration_time` shows on the
  order), cancel (`reduced_by 1.00`), and the order then reads `canceled` and is absent
  from resting orders.
- **REST reads lag the matching engine.** Right after a 201 create,
  `GET /portfolio/orders/{id}` returned **404**. Right after a successful cancel it still
  said `resting`. Both resolved within seconds. (The docs say so too: "There is typically a
  short delay before exchange events are reflected in the API endpoints", see
  Get User Data Timestamp.) Consequences:
  * Write responses (create/amend/cancel bodies) and the WS `user_orders`/`fill` channels
    are the authoritative real-time view. REST reads are for reconciliation.
  * `create_order_idempotent` polls the client_order_id lookup 4× over about 6 s before
    declaring the outcome unknown.
  * Reconcile raising "bot order still resting" right after a cancel can be lag. The gate
    simply retries the recovery 5 s later, which self-heals.
- `client_order_id` of 36 chars (`kmb-` + 32 hex) is accepted (resolves the length
  question).
- The unfiltered `/markets` list is dominated by KXMV combos, which 404 on
  `GET /markets/{ticker}`. `smoke` now defaults to `--series KXHIGHNY`.

### Still unverified (needs the authenticated demo run)

1. Duplicate `client_order_id` on create → assumed **409 Conflict** (the spec lists 409 for
   create). `create_order_idempotent` treats 409 as "already exists → look it up".
2. ~~Max `client_order_id` length~~: 36 chars verified live.
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

## Phase 2 decisions (2026-11-04)

- **New dependencies**, checked on PyPI by exact name and project URL:
  `websockets==17.2` (python-websockets/websockets; 17.3 is only 16 days old, so I took the
  previous release for margin) and `aiosqlite==0.22.1` (omnilib). Neither has runtime deps.
- **No proxy, ever.** `websockets` picks up a system proxy by default, so `proxy=None` is
  passed explicitly.
- **`use_yes_price: true`** on the order-book subscription. Kalshi will make it the default
  and then remove the flag, so opting in now avoids a silent breaking change later.
  `book.py` converts NO levels back to NO-leg pricing, so WS and REST books are identical.
  **Verified live:** WS books equal REST books for 6 KXHIGHNY markets.
- **Seq gaps.** Any gap or duplicate on the order-book `sid` invalidates every book on it
  and sends one `get_snapshot` for all markets. Deltas for an invalid book are dropped, never
  applied. Each snapshot replaces its book. A delta that would make a level negative
  resyncs just that market. A snapshot overdue for more than 10 s forces a reconnect.
- **No clock-based delta filtering.** The exchange clock can go backwards during
  maintenance, so filtering on it could freeze a book. `seq` is the integrity check.
- **Fills and orders on WS have no seq.** After every (re)connect the safety gate runs a
  REST reconcile, which backfills fills and settlements into the ledger, deduplicated by
  `trade_id`.
- **Staleness.** A quiet market on a healthy connection isn't stale. Connection health
  comes from websockets pings (20 s interval and timeout) plus seq continuity. A book is
  usable iff `valid` and the socket is up. Phase 4's risk manager will gate on that plus
  `stale_data_seconds` for fair values.
- **Ledger.** SQLite WAL with `synchronous=FULL` (laptop power loss). Money is stored as
  TEXT and floats are refused. Fills, settlements, order events, snapshots, config versions
  and errors are append-only, enforced by triggers. A WS/REST copy of the same fill that
  disagrees on count, price, fee or order is logged to `errors`. Any taker fill is logged
  as an error, since the bot is maker-only. Order intents are written *before* sending
  (used from Phase 4).
- **Reconcile.** The first run looks back 30 days for fills and settlements; after that it
  resumes from the last recorded fill with a 1 h overlap. A bot order still resting after
  the cancel step fails reconciliation, so the gate retries the whole recovery. Manual
  orders are counted and never touched.
- **Watch set.** All open markets in KXHIGHNY, KXHIGHMIA and KXHIGHCHI (36 on demo
  today). The 3-market quoting limit applies in Phase 4. Markets are re-discovered every
  30 min; a changed set restarts the WebSocket, which trips the gate into full recovery.
- **KILL file.** Detected within 1 s: cancels bot orders, exits with code 10, and blocks
  startup until it's deleted.
- **Git identity.** This laptop had none, so I set a repo-local `Claude
  <noreply@anthropic.com>` to match the Phase 1 commit. Your global git config is
  untouched.

## Phase 3 decisions (2026-11-04): weather fair value, shadow mode

**Owner-approved settlement model:**
1. The settlement value is an **integer °F**.
2. The day is the **NWS climate day in local standard time**: [00:00, 24:00) at the
   station's fixed standard offset (in DST that is 01:00 to 00:59 local daylight time).
   **Dated reminder:** recheck before US DST resumes on **2027-03-14**. The engine refuses
   any DST-season date until `weather.dst_rule_rechecked: true`, and logs a reminder from
   2027-02-28.
3. **NWS CLI + observations are the proxy** for The Weather Company's number, and every
   finalized market is cross-checked automatically. Mismatches go to ledger `errors`
   (`settlement_mismatch`) and the runner prints them.
4. **Strike semantics** (owner): "greater than X" = X+1 or above; "less than X" = X-1 or
   below; "between A-B" = A..B inclusive. Unit-tested on the exact quoted rules strings,
   including a check that every integer falls in exactly one bracket of a live event.

**The parser accepts only the verified template.** Rules text, station code, series→station
mapping, floor/cap strikes and `yes_sub_title` must all agree, or the market isn't priced.
A source other than "The Weather Company", a decimal strike, or extra wording means refusal.

**Proxy validated live:** on 2026-11-04, 126 of 126 prod markets settled in the prior 7
days (NYC, Miami, Chicago) had `expiration_value` equal to the final NWS CLI max (whole
numbers, e.g. "68.00") and a `result` matching our strike semantics. 18 more were too old
for the NWS CLI listing (it keeps about 7 days).

**Data sources (all verified live, field names in `weather/sources.py`):**
- Open-Meteo ensemble: GEFS (31 series) + ECMWF IFS 0.25° (51 series) = 82, hourly °F,
  UTC. Each member's max over the LST window (needs ≥ 20 of 24 hours).
- NWS hourly point forecast (`/points` → `forecastHourly`) as a mixture component (weight
  0.3). Only hours not yet past are used.
- NWS observations (KNYC, KMIA, KMDW; QC flags X/Q dropped) give the extreme so far.
- NWS CLI products (`CLINYC`, `CLIMIA`, `CLIMDW`): final ones pin the outcome; intraday
  "VALID … AS OF" ones are official hard bounds.
- Prices: **prod public order books** (read-only, unauthenticated). Demo books are thin and
  not real prices, so they're shown for reference only. Settlements are also read from
  prod, because demo's finalized markets have an empty `expiration_value`.

**Model (`weather/model.py`)**: latent T ~ mixture of N(member + bias, σ) and
N(NWS + bias, σ_nws); reported X = round-half-up(T); P(X = k) integrates each component
over [k−0.5, k+0.5). Statistics are float; the fair price becomes a Decimal (4 dp) only at
the boundary. **All parameters are untuned priors** (σ 2.0 °F, NWS weight 0.3 / σ 2.5 °F,
bias 0, observation slack 5%), bounds-checked at load. Calibrating them is the point of the
shadow log.

**Observation conditioning.** A METAR-derived extreme r is a *soft* bound: X ≥ r with
probability 0.95, X = r−1 with probability 0.05 (°C→°F conversion can be a degree off).
The first version used a hard "r−1" floor. With an observed 78.08 °F it put most of the
mass on exactly 77, giving P(≥78) = 0.25 for a near-certain outcome. A unit test caught
it before it shipped.

**Same-day confidence is halved without an official CLI value.** Live on 2026-11-04,
hourly readings gave 71 / 84 / 70 °F so far while prod was already bidding 0.99 on
NYC 72-73, Miami 85-86 and Chicago 69-70: hourly readings miss peaks between reports.
The METAR 6-hour max groups I checked through the NWS API were incomplete and
inconsistent, so I'm not relying on them. Halved confidence (0.5) is below
`min_confidence` 0.6, so Phase 4 won't quote same-day markets on hourly evidence alone.
An intraday CLI value restores it.

**Live shadow comparison 2026-11-04 (tomorrow's brackets, prod):** the untuned model
disagrees with the market by up to 0.65 on some brackets (e.g. Miami 86-87: fair 0.11 vs
bid 0.76; Chicago ≤54: 0.45 vs 0.79). Treat that as model error until calibration says
otherwise. **No edge is claimed.** The raw ensemble median (Miami 82.5 °F) sits well below
the NWS forecast (84) and the market's implied peak (~86-87), which is the kind of
station bias the planned per-station bias correction targets.

**Robustness:**
- NWS lists some CLI products before they can be fetched (404 seen live). A failed
  product is skipped for that round and retried, never cached as bad, and never sinks a
  whole city's pricing.
- **False-positive incident:** the first live settlement check ran against demo, read empty
  `expiration_value`s as mismatches, and wrote 126 bogus `settlement_mismatch` rows to the
  local `data\ledger.db`. That table is append-only, so the rows stay, and one
  `retraction` row now explains them. Fixed: empty values are skipped, and the check
  always reads prod.
- `tzdata==2026.5` added (python/tzdata). Windows has no time-zone database, so `zoneinfo`
  couldn't resolve America/New_York without it. 2026.6 is only a week old.
- Weather fetches run as background tasks in the runner. They can never block the safety
  loop, and failures are logged to the ledger.

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
