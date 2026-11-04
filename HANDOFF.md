# HANDOFF

Current state of the Kalshi maker bot, for whoever picks this up next (human or Claude).
Spec: `KALSHI_MAKER_BOT_PROMPT.md`. Every decision and doc conflict: `DECISIONS.md`.

_Last updated: 2026-11-04 (Phase 3 paused on settlement wording)._

## Where things stand

| Phase | State |
|---|---|
| 1 Skeleton, auth, read-only REST | done, verified live on demo |
| 1b Windows port, V2 orders, laptop safety | done; **demo order round-trip passes** (place → amend expiry → cancel) |
| 2 WS books + reconciliation + ledger | done, **approved by owner 2026-11-04** |
| 3 Weather fair value (shadow mode) | **paused**: settlement wording ambiguous; owner decision needed (below) |
| 4+ | not started |

Environment: owner's Windows 11 laptop in Prague (Kalshi reachable directly; never add
VPN/proxy/geolocation workarounds). `ENV=demo`, `LOCATION_OK=false`. Demo account
funded with $500. API tier Basic.

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

## Blocking decision for Phase 3 (asked 2026-11-04)

Settlement rules were pulled from the API and quoted verbatim in DECISIONS.md ("Weather
settlement rules: exact wording"). Source = The Weather Company for all three series;
stations = NWS CLI Central Park / Miami / Chicago-Midway. Three things are not stated:
(1) the precision of the final value (the brackets only work for integers), (2) the day
window (the text says 11:59 PM local, but close_time is 1:00 AM local standard), and (3)
whether TWC republishes the NWS CLI value. Proposed handling, awaiting the owner's answer:

1. Model the settlement value as an integer °F (the strikes, subtitles and CLI reports all
   imply it). Log any settlement that contradicts this.
2. Use the NWS CLI day (local standard time). It equals the calendar day until DST resumes on
   2027-03-14; revisit before then.
3. Use the NWS CLI report + NWS observations as the proxy for TWC's number. After each
   settlement, compare Kalshi's result with the CLI value and auto-flag any disagreement.
   Don't scrape weather.com's undocumented backend unless the owner asks.

## Open items for the owner

- Windows Time service was not running on 2026-11-04 (clock about 0.7 s behind). Fix in an
  admin PowerShell: `Set-Service W32Time -StartupType Automatic; Start-Service W32Time;
  w32tm /resync`
- Duplicate `client_order_id` → 409 is still assumed, not observed.

## Gotchas learned the hard way

- REST reads lag writes by seconds (404 right after create; `resting` right after cancel).
  Trust write responses and the WS; poll when reading back.
- The unfiltered `/markets` list is mostly KXMV combos, which 404 on single-market GET.
- weather.gov returns 403 without a User-Agent.
- Demo weather markets use sub-cent edge ticks (0.001 below $0.10 and above $0.90).
- PowerShell here-strings can't be piped to `git commit -F -`; write the message to a file.
- Git identity is repo-local `Claude <noreply@anthropic.com>`.
