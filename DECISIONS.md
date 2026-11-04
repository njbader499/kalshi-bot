# DECISIONS

Log of non-obvious choices and of every place the spec and Kalshi's docs disagree, or
where a detail could not be verified. Newest entries at the bottom of each section.

## How docs were verified (Phase 1, 2026-11-04)

The build sandbox's network policy **blocked docs.kalshi.com and all Kalshi API hosts**
(HTTP 403 at the proxy). Verification in Phase 1 came from web-search summaries of
docs.kalshi.com pages, not from reading the pages or calling the API directly. Anything
that could not be confirmed that way is marked `UNVERIFIED` in code and listed below.
**Before Phase 2, run `python -m kalshi_maker smoke` against demo from your own machine**
(or allow `docs.kalshi.com`, `*.kalshi.co`, `*.kalshi.com` in the sandbox) so the live
responses can confirm the field names below.

## Verified (via docs.kalshi.com search summaries)

| Item | Value | Source page |
|---|---|---|
| Demo REST | `https://external-api.demo.kalshi.co/trade-api/v2` | getting_started/api_environments |
| Demo WS | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | same |
| Prod REST / WS | `https://external-api.kalshi.com/trade-api/v2`, `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | same |
| Legacy hosts | `demo-api.kalshi.co`, `api.elections.kalshi.com` still work | same |
| Separate demo / prod keys | yes | same |
| Auth headers | `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE` | getting_started/api_keys |
| Signed message | `timestamp + METHOD + path`, path starts `/trade-api/v2` | quick_start_authenticated_requests |
| Signature | RSA-PSS, MGF1-SHA256, salt = digest length, base64 | same |
| WS signing | `GET` + `/trade-api/ws/v2` | WS quickstart (via guide) |
| Orderbook REST | `orderbook_fp.yes_dollars` / `no_dollars`, `[price, size]` string pairs, **bids only**, ascending (best last) | getting_started/orderbook_responses |
| Market fields | `yes_bid_dollars`, `yes_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `price_level_structure`, `price_ranges[{start,end,step}]` | api-reference/market/get-markets |
| Balance | `balance` (int cents, truncates sub-cent), `balance_dollars` (fixed-point string, added ~May 2026) | api-reference/portfolio/get-balance, changelog |

## UNVERIFIED (must be confirmed against live docs/API)

1. **Query string excluded from signature.** Community sources say so; the official
   example path has no query. We strip it (`auth.signing_path`). If authenticated GETs
   with params return 401 on demo, this is the first suspect.
2. **Ed25519 keys.** Official docs describe RSA (`RSA_PRIVATE_KEY` format). A third-party
   guide claims Ed25519 is now the default. We load either key type; only RSA-PSS is
   confirmed. *Conflict with the spec, which says Ed25519 is preferred.* Use whatever the
   Kalshi key page gives you.
3. **`price_ranges[].step` is the tick size.** Strong inference, not stated outright.
   The bot refuses to quote a market without `price_ranges` instead of assuming 1¢.
4. **Rate limits.** Search results disagree: the older official page lists *requests* (Basic
   20 read / 10 write per sec); newer guides list *tokens* (Basic 200 / 100, most calls cost
   10, `GET /account/endpoint_costs` is authoritative). The two models give the same
   effective rate, so the token model is configured. `/account/limits` and
   `/account/endpoint_costs` paths are unverified. Balance may have its own 1/sec bucket.
5. **`portfolio_value_dollars`.** Assumed by analogy with `balance_dollars`; falls back to
   integer-cent `portfolio_value`.
6. **Positions response key `market_positions`.** Remembered from earlier API versions. To
   be confirmed in Phase 2 (reconciliation).
7. **Spec vs docs on the WS book (Phase 2).** Spec says the snapshot has
   `yes_dollars_fp` / `no_dollars_fp`; REST uses `orderbook_fp.yes_dollars`. Will check
   the AsyncAPI spec before writing ws.py.
8. **A second demo WS host** (`warp-ws.demo.kalshi.co`) is mentioned for fill/order
   updates. Need to check whether the `fill` / `user_orders` channels need it.

## Design choices

- **Layout.** The repo root is the spec's `kalshi_maker/` directory, and the spec's `bot/`
  package is `kalshi_maker/` (importable as `kalshi_maker.*`). It's the same structure with
  one less nesting level.
- **Python.** Targets 3.12 (the venv uses `/usr/bin/python3.12`).
- **pydantic-settings** (official pydantic org package) loads `.env`. That's one extra
  dependency, but it avoids a hand-rolled .env parser.
- **No respx.** Tests use httpx's built-in `MockTransport`, so there's no extra test dependency.
- **YAML money as strings.** Risk limits are quoted strings so they never pass through
  float. Unquoted YAML floats still convert exactly (pydantic goes via `str`).
- **Config is strict.** Unknown keys are rejected, because a typo like `max_capitl_deployed`
  must not silently fall back to a default.
- **Limit coherence checks.** Load fails if order cap > market cap > event cap > category
  cap > capital cap, if daily > weekly > drawdown is broken, or if drawdown > 50% of bankroll.
- **Required exclusions.** Config validation refuses to load if sports, politics,
  elections, entertainment or mentions are removed from the banned list, or if KXMV
  isn't banned. Changing Section 6 means changing code, not just config.
- **`max_markets_quoted: 3`** in the example config (the demo start value); the spec's 10 is
  meant for later.
- **Retries.** Only GETs are retried (429/5xx/transport errors, full-jitter exponential
  backoff, honours `Retry-After`). Non-GET requests are never auto-retried. Order
  idempotency (query by `client_order_id`) comes in Phase 4.
- **Token bucket burst = 1 s** of budget. That's conservative, because the docs' burst
  figure is unclear (2 s or 3 s depending on the source).
- **Fee rounding** applies to the whole order fee, `ceil` to the cent, at taker rate 0.07.
  Maker rate comes from config (default 0), and per-series fee fields will be read from the
  series object once verified.
- **Prod gate.** Requires ENV=prod, LOCATION_OK, I_ACCEPT_REAL_MONEY_RISK, and a typed
  phrase on first start, remembered in `data/.prod_confirmed`. LOCATION_OK lives in `.env`,
  not YAML, so all environment gates are in one place.
