"""Strategy interface — shared by live/shadow trading and the replay engine.

A Strategy consumes FeatureSnapshots and emits StrategySignals. It never
talks to an exchange, a database, or a clock: all inputs arrive in the
snapshot and all effects go through the returned signal. That is what makes
the same strategy code runnable live, in shadow, and in replay without
divergence (a hard requirement of the evaluation framework).

Position sizing here is a *request*; the order manager / risk layer may
downsize or veto. Strategies declare the regimes they act in; the runner
enforces the declaration so a strategy can never trade outside them.
"""

from __future__ import annotations

import abc
from typing import Any

from pydantic import BaseModel

from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent


class PositionState(BaseModel):
    """The strategy's current exposure in one market."""

    market_ticker: str
    side: str | None = None          # "yes" | "no" | None (flat)
    contracts: float = 0.0
    avg_price: float = 0.0           # in the side's own terms
    entry_ts: float | None = None


class StrategySignal(BaseModel):
    intent: OrderIntent
    contracts: float
    limit_price: float               # in the intent's own terms (NO price for NO intents)
    execution: str = "taker"         # "taker" (cross now, IOC) | "maker" (rest inside spread)
    reason: str = ""
    counterfactual_id: str | None = None  # runtime linkage, ignored by strategies


class Strategy(abc.ABC):
    """Base class. Subclasses set `name`, `active_regimes`, and `on_snapshot`."""

    name: str = "base"
    version: str = "v1"
    active_regimes: frozenset[Regime] = frozenset({Regime.EARLY, Regime.MID, Regime.LATE})

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = params or {}
        configured = self.params.get("active_regimes")
        if configured is not None:
            if not isinstance(configured, (list, tuple, set, frozenset)) or not configured:
                raise ValueError("active_regimes must be a non-empty sequence")
            try:
                requested = frozenset(
                    item if isinstance(item, Regime) else Regime(str(item).upper())
                    for item in configured
                )
            except ValueError as exc:
                raise ValueError(f"invalid active_regimes: {configured}") from exc
            allowed = type(self).active_regimes
            if not requested.issubset(allowed):
                raise ValueError(
                    f"active_regimes {sorted(r.value for r in requested)} exceed "
                    f"strategy regimes {sorted(r.value for r in allowed)}"
                )
            self.active_regimes = requested

    def params_key(self) -> str:
        """Stable identity of this strategy+parameterization for versioning."""
        items = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"{self.name}:{self.version}:{items}"

    def evaluate(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        """Runner entry point: enforces regime gating, then delegates."""
        if snapshot.regime not in self.active_regimes:
            return None
        return self.on_snapshot(snapshot, position)

    @abc.abstractmethod
    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        """Return a signal, or None to do nothing this cycle."""


class NaiveEdgeTaker(Strategy):
    """Reference baseline: take model-vs-market edge when it clears a threshold.

    Enters at most once per market window, holds to settlement. Exists to
    exercise the replay engine end to end and to serve as the floor any real
    strategy must beat — not a recommendation.

    params: edge_threshold (dollars, default 0.03), contracts (default 10),
            max_price (skip near-extreme entries, default 0.90).
    """

    name = "naive_edge_taker"
    active_regimes = frozenset({Regime.MID})

    def on_snapshot(
        self, snapshot: FeatureSnapshot, position: PositionState
    ) -> StrategySignal | None:
        if position.side is not None:  # one entry per window, hold to settlement
            return None
        threshold = float(self.params.get("edge_threshold", 0.03))
        contracts = float(self.params.get("contracts", 10))
        max_price = float(self.params.get("max_price", 0.90))

        if snapshot.edge_yes_net is not None and snapshot.edge_yes_net >= threshold:
            price = snapshot.yes_ask
            if price is not None and price <= max_price:
                return StrategySignal(
                    intent=OrderIntent.BUY_YES,
                    contracts=contracts,
                    limit_price=price,
                    reason=f"edge_yes_net={snapshot.edge_yes_net:.3f}>={threshold}",
                )
        if snapshot.edge_no_net is not None and snapshot.edge_no_net >= threshold:
            if snapshot.yes_bid is not None:
                no_price = round(1 - snapshot.yes_bid, 4)
                if no_price <= max_price:
                    return StrategySignal(
                        intent=OrderIntent.BUY_NO,
                        contracts=contracts,
                        limit_price=no_price,
                        reason=f"edge_no_net={snapshot.edge_no_net:.3f}>={threshold}",
                    )
        return None
