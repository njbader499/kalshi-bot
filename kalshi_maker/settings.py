"""Configuration: YAML (config.yaml) for behaviour, .env for secrets and environment gates.

Everything is validated at load. The bot refuses to start on absurd values.
Money is Decimal; YAML money values are strings so they never pass through float.
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .keyperms import KeyPermissionError, check_key_permissions

PROD_CONFIRM_PHRASE = "I ACCEPT REAL MONEY RISK"


class ConfigError(Exception):
    """Raised when configuration is invalid or unsafe. The bot must not start."""


class Env(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HostPair(_Strict):
    rest: str
    ws: str

    @field_validator("rest")
    @classmethod
    def _rest_https(cls, v: str) -> str:
        if not v.startswith("https://") or not v.rstrip("/").endswith("/trade-api/v2"):
            raise ValueError(f"REST host must be https://.../trade-api/v2, got {v!r}")
        return v.rstrip("/")

    @field_validator("ws")
    @classmethod
    def _ws_wss(cls, v: str) -> str:
        if not v.startswith("wss://") or not v.rstrip("/").endswith("/trade-api/ws/v2"):
            raise ValueError(f"WS host must be wss://.../trade-api/ws/v2, got {v!r}")
        return v.rstrip("/")


class Hosts(_Strict):
    demo: HostPair
    prod: HostPair

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.demo.rest == self.prod.rest or self.demo.ws == self.prod.ws:
            raise ValueError("demo and prod hosts must differ")
        if "demo" not in self.demo.rest or "demo" in self.prod.rest:
            raise ValueError("demo host must contain 'demo' and prod host must not")
        return self


def _money(lo: str, hi: str) -> Any:
    return Field(ge=Decimal(lo), le=Decimal(hi))


class RiskLimits(_Strict):
    bankroll: Decimal = _money("1", "100000")
    max_capital_deployed: Decimal = _money("0", "100000")
    max_cost_per_market: Decimal = _money("0", "100000")
    max_cost_per_event: Decimal = _money("0", "100000")
    max_cost_per_category: Decimal = _money("0", "100000")
    max_order_cost: Decimal = _money("0", "100000")
    max_resting_orders: int = Field(ge=0, le=500)
    max_markets_quoted: int = Field(ge=0, le=200)
    daily_loss_stop: Decimal = _money("0", "100000")
    weekly_loss_stop: Decimal = _money("0", "100000")
    max_drawdown: Decimal = _money("0", "100000")
    max_orders_per_minute: int = Field(ge=1, le=600)
    max_consecutive_api_errors: int = Field(ge=1, le=100)
    api_error_pause_seconds: int = Field(ge=1, le=86400)
    stale_data_seconds: int = Field(ge=1, le=3600)
    allow_taker_flatten: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        b = self.bankroll
        problems: list[str] = []
        if self.max_capital_deployed > b:
            problems.append("max_capital_deployed exceeds bankroll")
        if self.max_order_cost > self.max_cost_per_market:
            problems.append("max_order_cost exceeds max_cost_per_market")
        if self.max_cost_per_market > self.max_cost_per_event:
            problems.append("max_cost_per_market exceeds max_cost_per_event")
        if self.max_cost_per_event > self.max_cost_per_category:
            problems.append("max_cost_per_event exceeds max_cost_per_category")
        if self.max_cost_per_category > self.max_capital_deployed:
            problems.append("max_cost_per_category exceeds max_capital_deployed")
        if self.daily_loss_stop > self.weekly_loss_stop:
            problems.append("daily_loss_stop exceeds weekly_loss_stop")
        if self.weekly_loss_stop > self.max_drawdown:
            problems.append("weekly_loss_stop exceeds max_drawdown")
        if self.max_drawdown > b * Decimal("0.5"):
            problems.append("max_drawdown above 50% of bankroll")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RateLimits(_Strict):
    """Token buckets per docs.kalshi.com/getting_started/rate_limits (verified 2026-11-04).

    Basic tier: read 200 tokens/s with 3 s of burst capacity; write 100 tokens/s with
    1 s capacity (Basic write buckets do not bank idle time). GET /portfolio/balance has
    its own bucket: 1 call/s on Basic and Advanced, no burst. Default cost is 10 tokens;
    non-default costs come from GET /account/endpoint_costs (public) at startup, and the
    values in `endpoint_costs` here are the offline fallback. Keys are "METHOD /path"
    relative to /trade-api/v2, with ":param" placeholders, e.g.
    "DELETE /portfolio/events/orders/:order_id".
    """

    read_tokens_per_sec: int = Field(ge=1, le=100000)
    write_tokens_per_sec: int = Field(ge=1, le=100000)
    default_cost: int = Field(ge=1, le=1000)
    read_burst_seconds: float = Field(gt=0, le=5)
    write_burst_seconds: float = Field(gt=0, le=5)
    balance_calls_per_sec: float = Field(gt=0, le=10)
    refresh_costs_from_api: bool = True
    endpoint_costs: dict[str, int] = Field(default_factory=dict)
    max_retries: int = Field(ge=0, le=10)
    backoff_base_seconds: float = Field(gt=0, le=10)
    backoff_max_seconds: float = Field(gt=0, le=600)


class Fees(_Strict):
    taker_rate: Decimal = _money("0", "0.25")
    default_maker_rate: Decimal = _money("0", "0.25")
    allowed_maker_fee_series: list[str] = Field(default_factory=list)


class Exclusions(_Strict):
    banned_categories: list[str]
    banned_ticker_patterns: list[str]
    min_time_to_close_minutes: int = Field(ge=0, le=100000)

    @field_validator("banned_categories")
    @classmethod
    def _required_bans(cls, v: list[str]) -> list[str]:
        required = {"sports", "politics", "elections", "entertainment", "mentions"}
        missing = required - {c.lower() for c in v}
        if missing:
            raise ValueError(f"banned_categories must include {sorted(missing)} (Section 6)")
        return v

    @field_validator("banned_ticker_patterns")
    @classmethod
    def _mv_banned(cls, v: list[str]) -> list[str]:
        if not any("KXMV" in p for p in v):
            raise ValueError("banned_ticker_patterns must ban multivariate KXMV tickers")
        return v


class Safety(_Strict):
    """Laptop safety (sleep/wake, network drops, clock jumps) and order expiry."""

    order_expiry_seconds: int = Field(ge=60, le=3600)
    expiry_refresh_seconds: int = Field(ge=30, le=3000)
    clock_jump_seconds: float = Field(ge=5, le=600)
    loop_stall_seconds: float = Field(ge=5, le=600)
    clock_drift_warn_seconds: float = Field(gt=0, le=30)
    prevent_sleep: bool = True
    cancel_order_on_pause: bool = True

    @model_validator(mode="after")
    def _refresh_before_expiry(self) -> Self:
        if self.expiry_refresh_seconds * 2 > self.order_expiry_seconds:
            raise ValueError(
                "expiry_refresh_seconds must be at most half of order_expiry_seconds, so a "
                "missed refresh doesn't let live quotes expire mid-session"
            )
        return self


class MarketsCfg(_Strict):
    """Which markets the bot watches. Owner confirmed NYC, Miami, Chicago (2026-11-04)."""

    weather_series: list[str] = Field(min_length=1, max_length=20)
    rediscover_minutes: int = Field(ge=1, le=1440)
    status_log_seconds: int = Field(ge=5, le=3600)

    @field_validator("weather_series")
    @classmethod
    def _weather_only(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not t.startswith(("KXHIGH", "KXLOW"))]
        if bad:
            raise ValueError(f"weather_series must be KXHIGH*/KXLOW* series, got {bad}")
        return v


class StationModelCfg(_Strict):
    """Per-station overrides of the model parameters (fit by scripts/calibrate_weather.py).

    bias_gfs_f / bias_ecmwf_f apply to that model's ensemble members; bias_f is the fallback
    for members of an unknown model. None = inherit the global value."""

    bias_f: float | None = Field(default=None, ge=-6, le=6)
    bias_gfs_f: float | None = Field(default=None, ge=-6, le=6)
    bias_ecmwf_f: float | None = Field(default=None, ge=-6, le=6)
    sigma_f: float | None = Field(default=None, ge=0.3, le=10)
    nws_weight: float | None = Field(default=None, ge=0, le=1)
    nws_sigma_f: float | None = Field(default=None, ge=0.3, le=10)
    nws_bias_f: float | None = Field(default=None, ge=-6, le=6)


class WeatherCfg(_Strict):
    """Weather fair-value engine (model v2, see weather/model.py). The global parameters are
    priors; per-station values from the calibration script override them."""

    # Owner-approved climate-day rule (NWS LST day) must be re-verified before US DST
    # resumes 2027-03-14. Until this is set true, DST-season dates are refused.
    dst_rule_rechecked: bool = False
    sigma_f: float = Field(ge=0.3, le=10)
    nws_weight: float = Field(ge=0, le=1)
    nws_sigma_f: float = Field(ge=0.3, le=10)
    bias_f: float = Field(ge=-6, le=6)  # ensemble members only (per model via stations)
    nws_bias_f: float = Field(default=0.0, ge=-6, le=6)  # NWS is forecaster-adjusted: keep 0
    min_confidence: float = Field(ge=0, le=1)
    fair_interval_minutes: int = Field(ge=1, le=240)
    settlement_check_minutes: int = Field(ge=5, le=1440)
    # P(official daily extreme - hourly-METAR extreme = d), keys as strings ("-1", "0", ...).
    obs_offset_probs: dict[str, float] = Field(
        default_factory=lambda: {"-1": 0.05, "0": 0.45, "1": 0.40, "2": 0.10}
    )
    # Local-standard-time hours of the daytime ramp: same-day confidence is halved inside
    # [start, end) unless an official intraday CLI value exists (hourly obs lag a rising
    # temperature). Quotes resume once the peak has passed.
    ramp_start_hour: int = Field(default=10, ge=0, le=23)
    ramp_end_hour: int = Field(default=16, ge=1, le=24)
    stations: dict[str, StationModelCfg] = Field(default_factory=dict)

    @field_validator("obs_offset_probs")
    @classmethod
    def _offsets(cls, v: dict[str, float]) -> dict[str, float]:
        try:
            keys = [int(k) for k in v]
        except ValueError as e:
            raise ValueError("obs_offset_probs keys must be integers as strings") from e
        if any(not (-3 <= k <= 3) for k in keys):
            raise ValueError("obs_offset_probs keys must be in [-3, 3]")
        if abs(sum(v.values()) - 1.0) > 1e-6 or any(p < 0 for p in v.values()):
            raise ValueError("obs_offset_probs must be non-negative and sum to 1")
        return v

    @field_validator("stations")
    @classmethod
    def _station_codes(cls, v: dict[str, StationModelCfg]) -> dict[str, StationModelCfg]:
        bad = [k for k in v if not (k.startswith("CLI") and len(k) == 6 and k.isupper())]
        if bad:
            raise ValueError(f"weather.stations keys must be CLI codes like CLINYC, got {bad}")
        return v

    @model_validator(mode="after")
    def _ramp(self) -> Self:
        if not self.ramp_start_hour < self.ramp_end_hour:
            raise ValueError("ramp_start_hour must be before ramp_end_hour")
        return self

    def offsets(self) -> dict[int, float]:
        return {int(k): float(p) for k, p in self.obs_offset_probs.items()}


class MarketBlendCfg(_Strict):
    """How the quoted fair value is formed from the model and the REAL market (prod book).

    fair_quote = weight * prod_mid + (1 - weight) * model, when the prod book is two-sided
    and its spread is at most max_spread; otherwise the model alone (with `model_only_ok`)
    or no quote. If |model - prod_mid| > max_disagreement the market is not quoted at all
    and the disagreement is logged: an untuned model fading the whole market is how the
    bot ended up short every favourite bracket on 2026-11-04.
    """

    weight: Decimal = Field(default=Decimal("0.6"), ge=0, le=1)
    max_spread: Decimal = Field(default=Decimal("0.10"), ge=Decimal("0.01"), le=Decimal("0.5"))
    max_disagreement: Decimal = Field(default=Decimal("0.15"), ge=Decimal("0.02"), le=1)
    model_only_ok: bool = False  # quote on the model alone when there is no usable prod mid
    max_ref_age_seconds: int = Field(default=1500, ge=60, le=7200)


class SymmetricCfg(_Strict):
    """Mode S, `symmetric`: rest a YES bid and a NO bid around the blended fair value, inside
    the market spread, never crossing. The structural maker edge (resting orders earn the
    spread and fade takers) is the only documented edge in these markets; this mode is how
    the bot collects it without betting against the market's view.

    half_spread: distance from fair to each quote (min; widened to stay inside the book)
    min_market_spread_ticks: only quote when the book's own spread is at least this wide
    max_inventory: contracts; past this on one side only the reducing quote is posted
    skew_per_contract: shift both quotes away from the held side by this much per contract
    """

    enabled: bool = False
    half_spread: Decimal = Field(default=Decimal("0.02"), ge=Decimal("0.005"), le=Decimal("0.2"))
    min_market_spread_ticks: int = Field(default=3, ge=2, le=50)
    max_inventory: Decimal = Field(default=Decimal("30"), gt=0, le=Decimal("5000"))
    skew_per_contract: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.05"))
    min_price: Decimal = Field(default=Decimal("0.05"), ge=Decimal("0.01"), le=Decimal("0.5"))
    count: Decimal = Field(default=Decimal("1"), gt=0, le=Decimal("100"))


class TradingCfg(_Strict):
    """Order placement (Phase 4). `trading_enabled` is the OWNER'S switch: while false, no
    order is ever sent; the order manager runs dry and only records what it would do."""

    trading_enabled: bool = False
    tilt_enabled: bool = True  # Mode A quotes (favourite / NO tilt); symmetric is separate
    order_count: Decimal = Field(gt=0, le=Decimal("100"))  # MINIMUM contracts per quote
    # Edge-scaled size: at edge == tilt.edge_buffer a quote is order_count contracts; it
    # grows linearly to risk.max_order_cost at edge >= edge_full_size, then is clipped to
    # the remaining worst-case room under every risk cap. 0 disables scaling.
    edge_full_size: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.5"))
    count_hysteresis_pct: int = Field(default=25, ge=0, le=100)  # resize only on >= this change
    loop_seconds: int = Field(ge=2, le=300)
    max_fair_age_seconds: int = Field(ge=60, le=7200)
    min_seconds_between_amends: int = Field(ge=1, le=3600)


class TiltCfg(_Strict):
    """Mode A (Section 3): bids only, on the side the favorite-longshot bias favors."""

    edge_buffer: Decimal = Field(ge=0, le=Decimal("0.20"))
    favorite_threshold: Decimal = Field(ge=Decimal("0.5"), lt=1)  # bid YES when fair >= this
    longshot_threshold: Decimal = Field(gt=0, le=Decimal("0.5"))  # bid NO when fair <= this
    min_yes_price: Decimal = Field(ge=Decimal("0.01"), le=Decimal("0.5"))
    min_no_price: Decimal = Field(ge=Decimal("0.01"), le=Decimal("0.5"))
    hysteresis_ticks: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.longshot_threshold < self.favorite_threshold:
            raise ValueError("longshot_threshold must be below favorite_threshold")
        return self


class Alerts(_Strict):
    """Telegram alert policy (Phase 5). Fills are batched; risk/kill/error are instant."""

    fill_batch_minutes: int = Field(ge=1, le=1440)
    instant_kinds: list[str] = Field(default_factory=lambda: ["risk", "kill", "error"])
    daily_summary_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    daily_summary_tz: str = "Europe/Prague"

    @field_validator("daily_summary_tz")
    @classmethod
    def _tz_exists(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception as e:  # ZoneInfoNotFoundError is a KeyError: pydantic won't wrap it
            raise ValueError(f"unknown time zone {v!r}") from e
        return v


class Paths(_Strict):
    data_dir: Path
    log_dir: Path
    kill_file: Path


class LoggingCfg(_Strict):
    level: str = "INFO"
    max_bytes: int = Field(ge=1024, le=1_000_000_000)
    backup_count: int = Field(ge=1, le=1000)


class AppConfig(_Strict):
    hosts: Hosts
    risk: RiskLimits
    rate_limits: RateLimits
    fees: Fees
    exclusions: Exclusions
    safety: Safety
    markets: MarketsCfg
    weather: WeatherCfg
    trading: TradingCfg
    tilt: TiltCfg
    blend: MarketBlendCfg = Field(default_factory=MarketBlendCfg)
    symmetric: SymmetricCfg = Field(default_factory=SymmetricCfg)
    alerts: Alerts
    paths: Paths
    logging: LoggingCfg


class Secrets(BaseSettings):
    """Values from the environment / .env. Secrets are SecretStr so they never print."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    ENV: Env = Env.DEMO
    LOCATION_OK: bool = False
    I_ACCEPT_REAL_MONEY_RISK: bool = False
    KALSHI_KEY_ID: SecretStr | None = None
    KALSHI_PRIVATE_KEY_PATH: Path | None = None
    TELEGRAM_TOKEN: SecretStr | None = None
    TELEGRAM_CHAT_ID: str | None = None


class Settings:
    """Validated config + secrets, plus environment gating."""

    def __init__(self, config: AppConfig, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets

    @property
    def env(self) -> Env:
        return self.secrets.ENV

    @property
    def hosts(self) -> HostPair:
        return self.config.hosts.prod if self.env is Env.PROD else self.config.hosts.demo

    @property
    def real_orders_allowed(self) -> bool:
        """Real-money orders need prod + location flag + risk acceptance. Demo is never real."""
        s = self.secrets
        return s.ENV is Env.PROD and s.LOCATION_OK and s.I_ACCEPT_REAL_MONEY_RISK


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"config file not found: {path} (copy config/config.example.yaml)")
    # utf-8-sig: tolerate a BOM (Windows PowerShell 5.1 writes one with -Encoding utf8)
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} is not a mapping")
    try:
        return AppConfig.model_validate(raw)
    except Exception as e:  # pydantic ValidationError -> clear refusal
        raise ConfigError(f"invalid config {path}:\n{e}") from e


def check_key_file(path: Path) -> None:
    """Private key must exist, be a regular file and be owner-only.

    Windows: DACL grants only the current user (see keyperms.py). POSIX: chmod 600.
    """
    try:
        check_key_permissions(path)
    except KeyPermissionError as e:
        raise ConfigError(str(e)) from e


def check_prod_gate(
    settings: Settings,
    confirm_marker: Path,
    ask: Any = input,
) -> None:
    """Refuse prod unless ENV=prod, LOCATION_OK, I_ACCEPT_REAL_MONEY_RISK, and a typed
    confirmation on first prod start (remembered in confirm_marker)."""
    s = settings.secrets
    if s.ENV is not Env.PROD:
        return
    if not s.LOCATION_OK:
        raise ConfigError("ENV=prod but LOCATION_OK is false. Real orders refused.")
    if not s.I_ACCEPT_REAL_MONEY_RISK:
        raise ConfigError("ENV=prod but I_ACCEPT_REAL_MONEY_RISK is false.")
    if confirm_marker.exists():
        return
    typed = ask(f"First prod start. Type exactly '{PROD_CONFIRM_PHRASE}' to continue: ")
    if typed.strip() != PROD_CONFIRM_PHRASE:
        raise ConfigError("prod confirmation phrase not entered; refusing to start")
    confirm_marker.parent.mkdir(parents=True, exist_ok=True)
    confirm_marker.write_text("confirmed\n")


def startup_warning(settings: Settings) -> str:
    s = settings.secrets
    lines = [
        "=" * 72,
        f" ENV={s.ENV.value}   LOCATION_OK={s.LOCATION_OK}   "
        f"real orders allowed={settings.real_orders_allowed}",
        " Kalshi eligibility depends on your PHYSICAL location. VPNs violate the terms.",
        " LOCATION_OK must only be true while you are physically in the US.",
    ]
    if s.ENV is Env.PROD and not s.LOCATION_OK:
        lines.append(" LOCATION_OK=false: the bot will REFUSE to place real orders.")
    lines.append("=" * 72)
    return "\n".join(lines)


def load_settings(config_path: Path, env_file: Path | None = Path(".env")) -> Settings:
    cfg = load_config(config_path)
    if env_file is not None and env_file.exists():
        secrets = Secrets(_env_file=env_file)
    else:
        secrets = Secrets(_env_file=None)
    if os.environ.get("ENV") and os.environ["ENV"] not in {"demo", "prod"}:
        raise ConfigError("ENV must be 'demo' or 'prod'")
    return Settings(cfg, secrets)
