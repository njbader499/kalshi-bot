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
| 1 | Skeleton, settings validation, auth, read-only REST, tests | **done, offline**. Live demo smoke test still pending |
| 2 | WS book + reconciliation + ledger | not started |
| 3 | Weather fair value (shadow mode) | not started |
| 4–10 | see the spec | not started |

## Setup (local)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp config/config.example.yaml config/config.yaml
cp .env.example .env            # fill in your DEMO key id + key path yourself
chmod 600 keys/kalshi_demo.pem  # the bot refuses looser permissions
```

## Phase 1 commands

```bash
python -m kalshi_maker check-config        # validate config/config.yaml and print limits
python -m kalshi_maker status              # exchange status (public)
python -m kalshi_maker markets --series KXHIGHNY --max 10
python -m kalshi_maker orderbook <TICKER>
python -m kalshi_maker balance             # needs demo key
python -m kalshi_maker smoke               # all of the above, for Phase 1 sign-off
```

## Tests

```bash
pytest -q                                   # offline unit tests
KALSHI_INTEGRATION=1 pytest -m integration  # live demo, read-only
ruff check . && mypy kalshi_maker
```

## Prod gate

Real orders require **all** of the following: `ENV=prod`, `LOCATION_OK=true` (only while
you are physically in the US; VPNs violate Kalshi's terms), `I_ACCEPT_REAL_MONEY_RISK=true`,
and typing a confirmation phrase on the first prod start. The bot never enables prod by itself.

See `DECISIONS.md` for every doc conflict and unverified API detail.
