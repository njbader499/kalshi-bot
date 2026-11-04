"""Opt-in live checks against the Kalshi DEMO API (read-only in Phase 1).

Run:  KALSHI_INTEGRATION=1 pytest -m integration
Needs config/config.yaml and .env with demo credentials. Never runs against prod.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kalshi_maker.cli import _signer
from kalshi_maker.rest import KalshiREST
from kalshi_maker.settings import Env, load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KALSHI_INTEGRATION") != "1", reason="opt-in"),
]


@pytest.fixture
def settings():
    s = load_settings(Path("config/config.yaml"), Path(".env"))
    assert s.env is Env.DEMO, "integration tests only run against demo"
    return s


async def test_public_read(settings) -> None:
    async with KalshiREST(settings.hosts.rest, settings.config.rate_limits) as api:
        assert "trading_active" in await api.exchange_status()
        n = 0
        async for m in api.iter_markets(status="open", limit=5):
            ob = await api.get_orderbook(m["ticker"])
            assert ob is not None
            n += 1
            if n >= 2:
                break


async def test_auth_balance(settings) -> None:
    async with KalshiREST(
        settings.hosts.rest, settings.config.rate_limits, signer=_signer(settings)
    ) as api:
        bal = await api.get_balance()
        assert bal.balance >= 0
