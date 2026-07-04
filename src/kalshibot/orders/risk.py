"""Deterministic risk layer — the final authority over every order.

Strategies propose, Claude (later) advises, this layer disposes. Every check
is a pure function of explicit state; nothing here consults a model. Applied
AFTER the smart-money modifier so no upstream layer can bypass a limit.

Enforced (spec section 8):
- settlement blackout: no entries in the final 90s of a window
- per-market position cap and per-asset notional cap (equal allocation)
- max daily loss per asset and global (halt the offender / halt everything)
- thin-market footprint: order size capped to a fraction of resting depth
  at the target price
- event blackout calendar: no new entries for affected assets in a window
  around configured macro/asset events
- paused assets and the global kill switch
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.strategies.base import PositionState, StrategySignal

logger = logging.getLogger(__name__)


class BlackoutEvent(BaseModel):
    label: str
    start_ts: float
    end_ts: float
    assets: list[str] = []  # empty = all assets

    def covers(self, asset: str, ts: float) -> bool:
        return self.start_ts <= ts <= self.end_ts and (
            not self.assets or asset in self.assets
        )


class RiskConfig(BaseModel):
    bankroll_usd: float = 1000.0
    n_assets: int = 9                      # equal allocation during evaluation
    max_bankroll_fraction_per_trade: float = 0.05
    max_position_contracts: int = 100
    max_daily_loss_per_asset_usd: float = 20.0
    max_daily_loss_global_usd: float = 100.0
    depth_cap_fraction: float = 0.25       # thin-market footprint control
    settlement_blackout_s: float = 90.0

    @property
    def per_asset_allocation(self) -> float:
        return self.bankroll_usd / self.n_assets


@dataclass
class Verdict:
    approved: bool
    contracts: float = 0.0
    reason: str = ""


@dataclass
class RiskManager:
    config: RiskConfig
    blackouts: list[BlackoutEvent] = field(default_factory=list)
    kill_switch: bool = False
    paused_assets: set[str] = field(default_factory=set)
    daily_pnl: dict[str, float] = field(default_factory=dict)   # asset -> net pnl
    _day: str = ""

    # ------------------------------------------------------------- state

    def record_pnl(self, asset: str, pnl_net: float, now: float | None = None) -> None:
        self._roll_day(now)
        self.daily_pnl[asset] = self.daily_pnl.get(asset, 0.0) + pnl_net

    def _roll_day(self, now: float | None = None) -> None:
        day = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self.daily_pnl = {}

    @property
    def global_daily_pnl(self) -> float:
        return sum(self.daily_pnl.values())

    # -------------------------------------------------------------- check

    def check(
        self,
        asset: str,
        signal: StrategySignal,
        snapshot: FeatureSnapshot,
        position: PositionState,
        resting_depth_at_price: float | None = None,
        now: float | None = None,
    ) -> Verdict:
        """Approve (possibly downsized) or veto an entry signal."""
        now = now if now is not None else time.time()
        self._roll_day(now)
        cfg = self.config

        if self.kill_switch:
            return Verdict(False, reason="kill switch engaged")
        if asset in self.paused_assets:
            return Verdict(False, reason=f"{asset} paused")
        if snapshot.regime == Regime.SETTLEMENT or (
            snapshot.seconds_remaining <= cfg.settlement_blackout_s
        ):
            return Verdict(False, reason="settlement blackout (final 90s)")
        for event in self.blackouts:
            if event.covers(asset, now):
                return Verdict(False, reason=f"event blackout: {event.label}")
        if self.daily_pnl.get(asset, 0.0) <= -cfg.max_daily_loss_per_asset_usd:
            return Verdict(False, reason=f"{asset} daily loss limit hit")
        if self.global_daily_pnl <= -cfg.max_daily_loss_global_usd:
            return Verdict(False, reason="global daily loss limit hit")
        if position.side is not None:
            return Verdict(False, reason="already positioned in this market")
        if signal.intent not in (OrderIntent.BUY_YES, OrderIntent.BUY_NO):
            return Verdict(False, reason="only BUY entries in v1 (exits at settlement)")
        if not 0 < signal.limit_price < 1:
            return Verdict(False, reason=f"bad limit price {signal.limit_price}")

        contracts = float(signal.contracts)
        contracts = min(contracts, float(cfg.max_position_contracts))
        # notional caps: per-trade bankroll fraction and per-asset allocation
        max_notional = min(
            cfg.bankroll_usd * cfg.max_bankroll_fraction_per_trade,
            cfg.per_asset_allocation,
        )
        if signal.limit_price > 0:
            contracts = min(contracts, max_notional / signal.limit_price)
        # thin-market footprint: never take more than a fraction of what rests
        if resting_depth_at_price is not None:
            contracts = min(contracts, resting_depth_at_price * cfg.depth_cap_fraction)

        contracts = float(int(contracts))  # whole contracts, conservative
        if contracts < 1:
            return Verdict(False, reason="size rounds to zero after caps")
        return Verdict(True, contracts=contracts, reason="ok")
