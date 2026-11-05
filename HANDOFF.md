# HANDOFF

Current state of the Kalshi maker bot, for whoever picks this up next (human or Claude).
Spec: `KALSHI_MAKER_BOT_PROMPT.md`. Every decision and doc conflict: `DECISIONS.md`.

_Last updated: 2026-11-05 (cloud session): model v2, market blend, symmetric quoting on
branch `cloud/weather-model-fixes` — see "Merging the model fixes" below. Trading on the
laptop was HALTED by the risk stops at 2026-11-05 06:01Z (phantom loss, see DECISIONS.md
"Model v2") and the collector was stopped by Ctrl+C at 13:06Z._

## Where things stand

| Phase | State |
|---|---|
| 1 Skeleton, auth, read-only REST | done, verified live on demo |
| 1b Windows port, V2 orders, laptop safety | done; **demo order round-trip passes** (place → amend expiry → cancel) |
| 2 WS books + reconciliation + ledger | done, **approved by owner 2026-11-04** |
| 3 Weather fair value (shadow mode) | done, approved 2026-11-04. **Shadow collector running** since 2026-11-04 20:46Z |
| 4 Risk + order manager + tilt | **LIVE ON DEMO since 2026-11-04 21:35Z** (owner's decision; local config.yaml `trading_enabled: true`; example config stays false) |
| 5 Telegram + watchdog + kill switch | **live**: @nolankalshi_bot, chat id in .env; /status and /kill verified by the owner; KILL file verified |
| 6+ | not started (A-S market making next per spec) |

Environment: owner's Windows 11 laptop in Prague (Kalshi reachable directly; never add
VPN/proxy/geolocation workarounds). `ENV=demo`, `LOCATION_OK=false`. Demo account
funded with $500. API tier Basic.

## Merging the model fixes (laptop, 2026-11-05)

1. Commit or stash local edits, then `git fetch origin` and
   `git merge origin/cloud/weather-model-fixes` (docs may conflict on line endings only).
2. `powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1` — 362 tests, ruff, mypy.
3. Update the LOCAL `config/config.yaml` from `config/config.example.yaml`: new `weather`
   keys (`obs_offset_probs`, `ramp_*`, `stations`, and the new prior values sigma 1.5 /
   nws_weight 0.5 / bias_f 1.0), new `blend:` and `symmetric:` sections,
   `trading.tilt_enabled`. All new keys have defaults, so the old file still loads, but the
   old sigma/weight/bias values would keep the old behaviour.
4. `.\.venv\Scripts\python.exe scripts\calibrate_weather.py --days 365 --out config\weather_calibration.yaml`
   and paste its `stations:` block (and `obs_offset_probs` once it has data) under
   `weather:` in config.yaml. Check the NCEI station names it prints.
5. `python -m kalshi_maker fair` and compare the fair values with the prod bid/ask: the
   leading bracket should now be near the market, and "not quoted" reasons should be rare.
6. `python -m kalshi_maker risk-reset --confirm`, delete `KILL` if present, restart the
   collector (`scripts\start-collector.ps1`), confirm the Telegram "bot started" alert and
   that /status shows realized/unrealized separately and a "not quoted:" line.

## How to run

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1          # once
powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1          # lint + types + unit
powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1 -Integration  # + live demo
.\.venv\Scripts\python.exe -m kalshi_maker run --max-seconds 120     # books + ledger, no orders
```

Secrets: `.env` (git-ignored) holds the demo Key ID and the key path
`C:/Users/njbad/.kalshi/demo_key.pem`. The key is ACL-locked to the owner; the bot refuses
to start otherwise. Never print or log key contents.

## Owner's standing decisions

- Cities: **KXHIGHNY, KXHIGHMIA, KXHIGHCHI**. Risk limits as in the spec for $500; start
  with 3 quoted markets.
- Alerts: fills batched every 15 min; risk/kill/error alerts instant. Telegram is Phase 5.
- Dashboard: local-only and read-only, after Telegram, low priority.
- VPS: Phase 9 only. Tax lots: FIFO. Exclusions: unchanged.
- Laptop safety: on sleep/wake, a network drop or a clock jump over 30 s → cancel bot
  orders, resnapshot, reconcile, then resume. 10-min exchange-side expiry refreshed while
  alive. Prevent sleep. Warn on clock drift over 1 s.
- Settlement source must come from the API rules text, never memory. Stop and ask if it's
  ambiguous.
- Commit and push after each milestone; stop for review at the end of each phase.

## Current live state (2026-11-04 21:50Z)

- The bot runs **live on DEMO** under the supervisor (pinned commit: see the supervisor log).
  Since ~22:00Z: **8 cities** (NYC, MIA, CHI, LAX, ATL, BOS, DFW, DEN), **max 10 markets**,
  edge-scaled size up to the $15 per-order cap; all risk limits unchanged. Telegram: 15-min
  fill digest + daily summary 08:00 Prague.
  `ENV=demo`, `LOCATION_OK=false`, `I_ACCEPT_REAL_MONEY_RISK=false`; the prod gate is
  untouched. Tilt only; risk limits as configured (the category cap $150 binds first).
- First demo orders 21:36Z; first fill: KXHIGHMIA-26NOV05-B88.5, 1 NO @ 0.89 (maker, no
  fee). Balance $499.11.
- Kill switch verified live: the KILL file (21:36Z) and Telegram /kill (21:44Z) both left 0
  bot orders on the exchange. After /kill: `risk-reset --confirm` (local), delete KILL,
  restart.
- The model is still UNTUNED (calibration pending). Demo P&L is a plumbing test, not
  evidence of edge.

## Lessons from going live (2026-11-04)

- WS `user_order` updates must drive the order manager (fills/expiry free a market),
  and expiries must be refreshed: both were missing until `faed911`.
- Telegram re-delivers an update until a later getUpdates acknowledges it. A restart after
  /kill re-killed itself until `a8a327e` (commands older than process start are ignored).
- Don't `tail -f` the supervisor log on Windows: it locks the file. Writes now retry and
  fall back to `logs\collector-supervisor-fallback.log` (`5efb726`). Kill orphaned
  tail.exe after stopping monitors.
- Restarting: KILL → wait for "not restarting (rc=10)" → (after /kill: risk-reset
  --confirm) → delete KILL → `start-collector.ps1 -Commit <sha>`.

## Shadow collector (running in the background)

Started 2026-11-04 20:46Z. It's `python -m kalshi_maker run` (no orders) under a
supervisor that restarts it on crashes (backoff 10 s → 5 min). It logs fair value vs prod
prices every 10 min and checks settlements hourly.

- Code runs from a **git worktree pinned to a commit**: `C:\Users\njbad\kalshi-bot-collector`
  (currently `57eb7cd`, Phase 3 code: shadow logging only). Editing this repo never affects
  it until it's re-pinned. Re-pinning to a later commit would also log dry-run order
  rehearsals; that's optional and the owner's call (stop it with KILL, delete KILL, then
  run start-collector.ps1 -Commit <sha>).
- Config: `config\collector.yaml` (git-ignored, generated from `config\config.yaml`, with
  absolute paths). Ledger `data\ledger.db`, logs `logs\collector-YYYYMMDD.log` and
  `logs\collector-supervisor.log`, KILL file `KILL` in this repo root.
- Check it: `Get-Content logs\collector-supervisor.log -Tail 5` and the newest
  `logs\collector-*.log` (a `shadow: logged 36 fair values` line every 10 min).
- Stop it: `New-Item KILL`. The bot exits with code 10 and the supervisor doesn't restart
  it. Delete `KILL` before starting again.
- Start / upgrade: `powershell -ExecutionPolicy Bypass -File .\scripts\start-collector.ps1
  -Commit <sha>` (stop it first; a second instance refuses to start via `data\run.lock`).
- It doesn't survive a reboot or logoff (no scheduled task yet). After a reboot, run the
  start script again. Lid-close sleep pauses it; the continuity monitor resyncs on wake.

## Calibration report

`.\.venv\Scripts\python.exe -m kalshi_maker calibrate [--csv out.csv]`. Run it after **3+
settled days**. It prints the Brier score of model vs market mid (paired; skill > 0 means
the model beat the market), reliability by city, and per-station bias with a suggested
`bias_f`. It's printed twice: all rows, then only rows with confidence ≥ 0.6 (what Phase
4 would quote). It reads the ledger read-only, so it's safe while the collector runs.

## Phase 3 state (shadow mode, no orders)

- Settlement model approved by the owner 2026-11-04: integer °F; NWS climate day in LST;
  NWS CLI + observations as the proxy for The Weather Company; "greater than X" = X+1 or
  above, "less than X" = X-1 or below, "between" inclusive. Quotes are in DECISIONS.md.
- **Proxy check:** 126/126 prod settlements in the last 7 days match the NWS CLI exactly.
  Runs automatically (`fair`, and hourly in `run`); mismatches go to ledger `errors`.
- **DST reminder:** before **2027-03-14**, re-verify the climate-day rule, then set
  `weather.dst_rule_rechecked: true`. Until then DST dates are refused (logged reminder
  from 2027-02-28).
- Model parameters are **untuned priors**. Tomorrow's fair values differ a lot from prod
  prices in places, so calibrate before trusting them. Same-day prices without an official
  intraday CLI are low-confidence (0.5) by design: hourly METARs miss peaks.
- Next: let `run` collect a few days of shadow data, then build the calibration report
  (Brier score / reliability by city, per-station bias), as the spec's Phase 3 asks.
- Local ledger note: `errors` holds 126 false-positive `settlement_mismatch` rows from the
  first demo-based check, plus one `retraction` row explaining them (the table is
  append-only).

## Turning trading on (owner only, after calibration)

1. Run `calibrate` after 3+ settled days. Look at skill vs market (> 0?), reliability and
   bias. Consider setting `weather.bias_f`.
2. Review the dry log: `SELECT * FROM order_actions ORDER BY id DESC LIMIT 50` in
   `data\ledger.db`.
3. Set `trading.trading_enabled: true` in `config\config.yaml` (demo only; prod has its own
   gates). Restart `run`. On startup it prints "LIVE ORDER PLACEMENT ENABLED".
4. `risk-status` shows halts; `risk-reset --confirm` clears a KILL (local only, never via
   Telegram).

## Telegram setup (owner)

1. Create a bot with @BotFather and copy the token. Send your bot any message, then read
   your chat id from `https://api.telegram.org/bot<token>/getUpdates` in a browser.
2. Put both in `.env`: `TELEGRAM_TOKEN=...`, `TELEGRAM_CHAT_ID=...` (never in chat).
3. Restart `run`. Startup prints "telegram: alerts + commands on". Try `/status`.
   Commands: `/status /positions /orders /limits /pause /resume /kill /mode`; risk-
   increasing ones ask for `/confirm <code>`.

## Kill switch / watchdog (Phase 5)

- Kill: `/kill` in Telegram, `New-Item KILL` in the repo root, or SIGTERM. All bot orders
  are cancelled, the process exits with code 10 and isn't restarted. In live mode it also
  needs `risk-reset --confirm`.
- Watchdog: if the main loop stalls > 60 s, a separate thread cancels bot orders (live
  mode) and alerts.

## Open items for the owner

- Windows Time service was not running on 2026-11-04 (clock about 0.7 s behind). Fix in an
  admin PowerShell: `Set-Service W32Time -StartupType Automatic; Start-Service W32Time;
  w32tm /resync`
- Duplicate `client_order_id` → 409 is still assumed, not observed.
- Old checkouts at `0f27b14` have a chmod-based `test_key_file_permissions` that fails on
  Windows; it was replaced in `592adc3` by an ACL check (passes on the owner's locked key).

## Gotchas learned the hard way

- REST reads lag writes by seconds (404 right after create; `resting` right after cancel).
  Trust write responses and the WS; poll when reading back.
- The unfiltered `/markets` list is mostly KXMV combos, which 404 on single-market GET.
- weather.gov returns 403 without a User-Agent.
- Demo weather markets use sub-cent edge ticks (0.001 below $0.10 and above $0.90).
- PowerShell here-strings can't be piped to `git commit -F -`; write the message to a file.
- Git identity is repo-local `Claude <noreply@anthropic.com>`.
- NWS lists CLI products before they're fetchable (404). Demo's settled markets have no
  `expiration_value`; use prod public data for settlements and real prices.
- Windows Python needs `tzdata` for `zoneinfo`.
- Bash heredocs with mixed quotes fail in this shell; put scripts in files.
