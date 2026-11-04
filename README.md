# kalshi-maker

A maker-only (post-only limit orders, never crosses the spread) trading bot for Kalshi.
**Demo by default.** It never places real-money orders unless you deliberately enable prod
(see "Prod gate").

## Honest expectations

- There are no audited profits for any retail Kalshi bot. The maker edge in the research is
  real but thin (about 1–2.7 percentage points depending on category), and it has flipped
  before (makers lost before 2024).
- Most retail blowups came from bugs and missing risk controls, not bad ideas. This project
  is built around preventing those.
- Treat the first months as paid tuition in market microstructure. Success means no blowups,
  clean books and a *measured* edge. This bot reports measured numbers only and never
  claims to be profitable.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Skeleton, settings validation, auth, read-only REST, tests | done; docs re-verified live 2026-11-04. Demo smoke test waits on the Key ID |
| 1b | Windows port, V2 order endpoints, laptop-safety layer | done (offline tests); live demo order test waits on the Key ID |
| 2 | WS book + reconciliation + ledger | not started |
| 3 | Weather fair value (shadow mode) | not started |
| 4–10 | see the spec | not started |

## Setup (Windows, PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

This creates `.venv`, installs the pinned dependencies, copies `config\config.yaml` and
`.env` (it never overwrites them), and locks `%USERPROFILE%\.kalshi\demo_key.pem` to your
user only:

```powershell
icacls "$HOME\.kalshi\demo_key.pem" /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

The bot refuses to start if any other account (including SYSTEM or Administrators) has an
entry on the key file, or if the file inherits permissions from its folder. Then put your
demo **Key ID** in `.env` yourself.

On Linux/macOS the equivalent is `chmod 600` on the key; the same check enforces it.

## Commands

```powershell
.\.venv\Scripts\python.exe -m kalshi_maker check-config     # validate config\config.yaml
.\.venv\Scripts\python.exe -m kalshi_maker status           # exchange status (public)
.\.venv\Scripts\python.exe -m kalshi_maker clock            # clock drift vs exchange (warns > 1 s)
.\.venv\Scripts\python.exe -m kalshi_maker markets --series KXHIGHNY --max 10
.\.venv\Scripts\python.exe -m kalshi_maker orderbook <TICKER>
.\.venv\Scripts\python.exe -m kalshi_maker balance          # needs the demo key
.\.venv\Scripts\python.exe -m kalshi_maker smoke            # all of the above + tier/costs
.\.venv\Scripts\python.exe -m kalshi_maker cancel-bot-orders  # cancels ONLY bot orders
.\.venv\Scripts\python.exe -m kalshi_maker upgrade-tier     # asks first; never automatic
```

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1               # ruff + mypy + unit tests
powershell -ExecutionPolicy Bypass -File .\scripts\check.ps1 -Integration  # + live DEMO tests
```

The integration tests place one post-only, 1-contract YES bid at the lowest tick of a demo
weather market, amend its expiry, cancel it and confirm it's gone.

## Running on a laptop

- While the bot runs it holds `SetThreadExecutionState`, so Windows won't idle-sleep. Closing
  the lid or choosing Sleep still suspends the machine.
- Every resting order carries an exchange-side expiry (10 min by default). The bot pushes it
  forward while it's alive, so if the laptop dies or sleeps, its quotes die too.
- After a sleep/wake, a Wi-Fi drop, or a clock jump over 30 s, the bot stops quoting. Once it
  can reach Kalshi again, it cancels every bot order, re-snapshots books and reconciles before
  quoting again. Your manual orders are never touched.
- Keep Windows Time running (`Get-Service W32Time`). The bot warns if your clock is more than
  1 s off the exchange's.

## Prod gate

Real orders require **all** of the following: `ENV=prod`, `LOCATION_OK=true` (only while
you are physically in the US; VPNs violate Kalshi's terms), `I_ACCEPT_REAL_MONEY_RISK=true`,
and typing a confirmation phrase on the first prod start. The bot never enables prod by itself.

See `DECISIONS.md` for every doc conflict and unverified API detail.
