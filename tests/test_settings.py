from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kalshi_maker.settings import (
    PROD_CONFIRM_PHRASE,
    AppConfig,
    ConfigError,
    Env,
    Secrets,
    Settings,
    check_key_file,
    check_prod_gate,
    load_config,
    load_settings,
    startup_warning,
)


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_example_config_loads(cfg_file: Path) -> None:
    cfg = load_config(cfg_file)
    assert cfg.risk.max_capital_deployed == 300
    assert cfg.hosts.demo.rest.endswith("/trade-api/v2")


def test_defaults_to_demo_and_no_real_orders(cfg_file: Path, tmp_path: Path) -> None:
    s = load_settings(cfg_file, env_file=tmp_path / "missing.env")
    assert s.env is Env.DEMO
    assert not s.real_orders_allowed
    assert "demo" in s.hosts.rest
    assert "VPN" in startup_warning(s)


def test_money_from_yaml_is_decimal(cfg_file: Path) -> None:
    from decimal import Decimal

    assert isinstance(load_config(cfg_file).risk.max_order_cost, Decimal)


@pytest.mark.parametrize(
    "path,value",
    [
        (("risk", "max_capital_deployed"), "5000"),  # > bankroll
        (("risk", "max_order_cost"), "80"),  # > per-market
        (("risk", "daily_loss_stop"), "100"),  # > weekly
        (("risk", "max_drawdown"), "400"),  # > 50% bankroll
        (("risk", "max_resting_orders"), 100000),
        (("risk", "bankroll"), "-1"),
        (("fees", "taker_rate"), "7"),  # 100x off
        (("rate_limits", "read_burst_seconds"), 0),
        (("rate_limits", "write_burst_seconds"), 60),
        (("safety", "order_expiry_seconds"), 0),
        (("safety", "expiry_refresh_seconds"), 500),  # > half of the 600 s expiry
        (("safety", "clock_jump_seconds"), 1),
        (("alerts", "fill_batch_minutes"), 0),
        (("hosts", "demo", "rest"), "http://insecure/trade-api/v2"),
        (("hosts", "prod", "rest"), "https://external-api.demo.kalshi.co/trade-api/v2"),
        (("exclusions", "banned_categories"), ["Sports"]),
        (("exclusions", "banned_ticker_patterns"), []),
    ],
)
def test_absurd_values_rejected(
    tmp_path: Path, example_cfg: dict, path: tuple, value: object
) -> None:
    d = example_cfg
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, example_cfg))


def test_unknown_config_key_rejected(tmp_path: Path, example_cfg: dict) -> None:
    example_cfg["risk"]["max_capitl_deployed"] = "1"  # typo must not be silently ignored
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, example_cfg))


def test_float_money_in_yaml_is_still_exact(tmp_path: Path, example_cfg: dict) -> None:
    example_cfg["risk"]["max_order_cost"] = 15.1  # unquoted in yaml
    cfg = load_config(_write(tmp_path, example_cfg))
    assert str(cfg.risk.max_order_cost) in {"15.1", "15.10"}


def _settings(cfg: AppConfig, **env: object) -> Settings:
    return Settings(cfg, Secrets(_env_file=None, **env))  # type: ignore[call-arg]


def test_prod_gate(cfg_file: Path, tmp_path: Path) -> None:
    cfg = load_config(cfg_file)
    marker = tmp_path / ".prod_confirmed"

    check_prod_gate(_settings(cfg), marker, ask=lambda _: pytest.fail("demo must not ask"))

    with pytest.raises(ConfigError, match="LOCATION_OK"):
        check_prod_gate(_settings(cfg, ENV="prod", I_ACCEPT_REAL_MONEY_RISK=True), marker)
    with pytest.raises(ConfigError, match="I_ACCEPT"):
        check_prod_gate(_settings(cfg, ENV="prod", LOCATION_OK=True), marker)

    ok = _settings(cfg, ENV="prod", LOCATION_OK=True, I_ACCEPT_REAL_MONEY_RISK=True)
    assert ok.real_orders_allowed
    with pytest.raises(ConfigError, match="phrase"):
        check_prod_gate(ok, marker, ask=lambda _: "yes")
    assert not marker.exists()
    check_prod_gate(ok, marker, ask=lambda _: PROD_CONFIRM_PHRASE)
    assert marker.exists()
    check_prod_gate(ok, marker, ask=lambda _: pytest.fail("asked twice"))


def test_location_flag_blocks_real_orders(cfg_file: Path) -> None:
    cfg = load_config(cfg_file)
    s = _settings(cfg, ENV="prod", LOCATION_OK=False, I_ACCEPT_REAL_MONEY_RISK=True)
    assert not s.real_orders_allowed
    assert "REFUSE" in startup_warning(s)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_key_file_permissions_posix(tmp_path: Path) -> None:
    k = tmp_path / "k.pem"
    with pytest.raises(ConfigError, match="not found"):
        check_key_file(k)
    k.write_text("x")
    os.chmod(k, 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        check_key_file(k)
    os.chmod(k, 0o600)
    check_key_file(k)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_key_file_permissions_windows(tmp_path: Path) -> None:
    k = tmp_path / "k.pem"
    with pytest.raises(ConfigError, match="not found"):
        check_key_file(k)
    k.write_text("x")
    # Fresh file in a temp dir inherits the folder's ACL -> refused, with the icacls fix.
    with pytest.raises(ConfigError, match="icacls"):
        check_key_file(k)
    user = os.environ["USERNAME"]
    subprocess.run(
        ["icacls", str(k), "/inheritance:r", "/grant:r", f"{user}:(R)"],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        check_key_file(k)
        # Adding anyone else (here: the built-in Users group, by SID) is refused again.
        subprocess.run(
            ["icacls", str(k), "/grant", "*S-1-5-32-545:(R)"], check=True, capture_output=True
        )
        with pytest.raises(ConfigError, match="another account"):
            check_key_file(k)
    finally:  # give ourselves full control back so pytest can clean up tmp_path
        subprocess.run(["icacls", str(k), "/reset"], capture_output=True)


def test_secrets_never_print() -> None:
    s = Secrets(_env_file=None, KALSHI_KEY_ID="abc-secret-id")  # type: ignore[call-arg]
    assert "abc-secret-id" not in repr(s)
    assert "abc-secret-id" not in str(s.model_dump())
