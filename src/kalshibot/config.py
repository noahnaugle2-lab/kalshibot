"""Application settings loaded from .env, and per-asset config from YAML."""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TARGET_ASSETS = ["BTC", "ETH", "SOL", "ZEC", "HYPE", "XRP", "DOGE", "BNB", "NEAR"]


class Mode(str, enum.Enum):
    SHADOW = "SHADOW"  # prod market data, simulated fills, no real orders
    DEMO = "DEMO"      # demo exchange, order plumbing only — never strategy eval
    LIVE = "LIVE"      # real money; requires manual flip after validation


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    mode: Mode = Field(default=Mode.SHADOW, alias="MODE")

    # LIVE has two independent interlocks so setting MODE=LIVE by itself can
    # never submit an order.  The confirmation string is intentionally exact
    # rather than a truthy value, making accidental shell/environment leakage
    # fail closed.
    live_trading_enabled: bool = Field(default=False, alias="LIVE_TRADING_ENABLED")
    live_trading_confirmation: str | None = Field(
        default=None, alias="LIVE_TRADING_CONFIRMATION"
    )
    live_allowed_assets: str = Field(default="", alias="LIVE_ALLOWED_ASSETS")
    live_max_contracts_per_order: int = Field(
        default=1, ge=1, alias="LIVE_MAX_CONTRACTS_PER_ORDER"
    )
    # A separate observer companion that evaluates the production execution
    # path but records proposals only. It must remain SHADOW mode.
    live_dry_run_enabled: bool = Field(default=False, alias="LIVE_DRY_RUN_ENABLED")

    kalshi_key_id: str | None = Field(default=None, alias="KALSHI_KEY_ID")
    kalshi_private_key_path: Path | None = Field(
        default=None, alias="KALSHI_PRIVATE_KEY_PATH"
    )
    kalshi_demo_key_id: str | None = Field(default=None, alias="KALSHI_DEMO_KEY_ID")
    kalshi_demo_private_key_path: Path | None = Field(
        default=None, alias="KALSHI_DEMO_PRIVATE_KEY_PATH"
    )

    healthchecks_ping_url: str | None = Field(default=None, alias="HEALTHCHECKS_PING_URL")
    n8n_webhook_base_url: str | None = Field(default=None, alias="N8N_WEBHOOK_BASE_URL")
    n8n_api_bearer_token: str | None = Field(default=None, alias="N8N_API_BEARER_TOKEN")
    # Telegram (nightly portfolio-break alerts; hourly PnL still runs via n8n)
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")

    dashboard_token: str | None = Field(default=None, alias="DASHBOARD_TOKEN")
    dashboard_port: int = Field(default=8777, alias="DASHBOARD_PORT")
    # Passkey (WebAuthn / Face ID / Touch ID) auth. session_secret signs the
    # login-session + challenge cookies; rp_id/origin must match the public host
    # the browser sees (the passkey is cryptographically bound to them).
    dashboard_session_secret: str | None = Field(
        default=None, alias="DASHBOARD_SESSION_SECRET"
    )
    dashboard_rp_id: str = Field(default="kalshi.naugle.us", alias="DASHBOARD_RP_ID")
    dashboard_origin: str = Field(
        default="https://kalshi.naugle.us", alias="DASHBOARD_ORIGIN"
    )

    # Decision engine transport: "api" (direct Anthropic API, ~5x cheaper per
    # call) or "cli" (Claude Code CLI, bills the local subscription).
    decision_provider: str = Field(default="api", alias="DECISION_PROVIDER")
    decision_model: str = Field(default="claude-haiku-4-5", alias="DECISION_MODEL")
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    # High-volume trade tape can grow by multiple GB/day in production. Keep a
    # short hot window in SQLite and archive older rows to compressed Parquet.
    retention_days: int = Field(default=7, ge=1, le=90, alias="RETENTION_DAYS")
    disk_warning_percent: float = Field(default=80, alias="DISK_WARNING_PERCENT")
    disk_critical_percent: float = Field(default=90, alias="DISK_CRITICAL_PERCENT")
    backup_max_age_hours: float = Field(default=26, alias="BACKUP_MAX_AGE_HOURS")


class AssetConfig(BaseModel):
    symbol: str
    spot_symbol_binance: str | None = None
    spot_symbol_coinbase: str | None = None
    enabled: bool = True
    paused: bool = False
    capital_allocation: str = "equal"
    edge_threshold_cents: float = Field(default=3, ge=0, le=100)
    max_position_contracts: int = Field(default=100, gt=0)
    strategy: str | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    smart_money_weight: float = Field(default=0.0, ge=0, le=1)
    late_window_enabled: bool = False
    ai_enabled: bool = False  # per-asset Claude decision layer (A/B vs baseline)


def load_asset_configs(path: Path | None = None) -> dict[str, AssetConfig]:
    """Load per-asset config, applying `defaults` under each asset entry."""
    path = path or PROJECT_ROOT / "config" / "assets.yaml"
    raw = yaml.safe_load(path.read_text())
    defaults: dict[str, Any] = raw.get("defaults", {})
    configs: dict[str, AssetConfig] = {}
    for symbol, overrides in raw.get("assets", {}).items():
        merged = {**defaults, **(overrides or {}), "symbol": symbol}
        configs[symbol] = AssetConfig.model_validate(merged)
    return configs


def load_risk(path: Path | None = None):
    """Build the RiskManager from the risk + blackouts sections of assets.yaml."""
    from datetime import datetime, timezone

    from kalshibot.orders.risk import BlackoutEvent, RiskConfig, RiskManager

    path = path or PROJECT_ROOT / "config" / "assets.yaml"
    raw = yaml.safe_load(path.read_text())
    risk_raw = raw.get("risk") or {}
    config = RiskConfig(
        bankroll_usd=risk_raw.get("bankroll_usd", 1000),
        n_assets=len(raw.get("assets") or {}) or 9,
        max_bankroll_fraction_per_trade=risk_raw.get("max_bankroll_fraction_per_trade", 0.05),
        max_position_contracts=risk_raw.get("max_position_contracts", 100),
        max_daily_loss_per_asset_usd=risk_raw.get("max_daily_loss_per_asset_usd", 20),
        max_daily_loss_global_usd=risk_raw.get("max_daily_loss_global_usd", 100),
        depth_cap_fraction=risk_raw.get("depth_cap_fraction", 0.25),
        settlement_blackout_s=risk_raw.get("settlement_blackout_s", 90),
    )

    def to_ts(v: Any) -> float:
        if isinstance(v, (int, float)):
            return float(v)
        dt = datetime.fromisoformat(str(v))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    blackouts = [
        BlackoutEvent(
            label=b.get("label", "event"),
            start_ts=to_ts(b["start"]),
            end_ts=to_ts(b["end"]),
            assets=b.get("assets") or [],
        )
        for b in (raw.get("blackouts") or [])
    ]
    return RiskManager(config=config, blackouts=blackouts)
