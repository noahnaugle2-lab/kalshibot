"""Merged smart-money signal and its application as a confidence modifier.

Layer A (Kalshi flow patterns) and Layer B (Polymarket repeat winners) merge
into one {lean, strength, breakdown}. It is NEVER a standalone trigger:
- agreement with a strategy signal scales size UP by weight x strength
- disagreement scales size DOWN, or vetoes when weight x strength is strong
- weight is per-asset config; weight 0 makes the whole layer inert (default
  during early evaluation, so its contribution is measured, not assumed)

Layer A only consults patterns with status='active' — promotion from
'candidate' requires held-out validation, so a fresh install contributes
NEUTRAL from Layer A until patterns earn it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshibot.kalshi.models import OrderIntent
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.flow import PATTERNS, load_window_tape

VETO_THRESHOLD = 0.5  # weight x strength beyond this on disagreement -> veto


@dataclass
class SmartMoneySignal:
    lean: str = "NEUTRAL"          # UP | DOWN | NEUTRAL
    strength: float = 0.0          # 0..1
    breakdown: dict = field(default_factory=dict)

    @classmethod
    def neutral(cls) -> "SmartMoneySignal":
        return cls()


def layer_a_lean(
    db: Database, asset: str, market_ticker: str, open_ts: float, close_ts: float
) -> tuple[str, float] | None:
    """Live lean from ACTIVE flow patterns on the current window's tape."""
    active = db.query(
        "SELECT pattern, hit_rate_30d, n_30d FROM flow_patterns "
        "WHERE asset = ? AND status = 'active'",
        (asset,),
    )
    if not active:
        return None
    tape = load_window_tape(db, market_ticker, asset, open_ts, close_ts, result="")
    score = 0.0
    for row in active:
        detector = PATTERNS.get(row["pattern"])
        if detector is None:
            continue
        lean = detector(tape)
        if lean is None:
            continue
        edge = max(0.0, (row["hit_rate_30d"] or 0.5) - 0.5)
        score += edge if lean == "UP" else -edge
    if abs(score) < 1e-9:
        return None
    return ("UP" if score > 0 else "DOWN", min(1.0, abs(score) * 4))


def merge(
    layer_a: tuple[str, float] | None,
    layer_b: dict | None,
) -> SmartMoneySignal:
    """Combine layer leans; equal weighting between layers that have an opinion."""
    votes: list[tuple[str, float, str]] = []
    if layer_a is not None:
        votes.append((layer_a[0], layer_a[1], "flow_patterns"))
    if layer_b and layer_b.get("lean") in ("UP", "DOWN"):
        votes.append((layer_b["lean"], float(layer_b.get("strength", 0)), "polymarket"))
    if not votes:
        return SmartMoneySignal.neutral()
    score = sum(s if lean == "UP" else -s for lean, s, _ in votes) / len(votes)
    if abs(score) < 1e-9:
        return SmartMoneySignal(breakdown={src: f"{lean} {s:.2f}" for lean, s, src in votes})
    return SmartMoneySignal(
        lean="UP" if score > 0 else "DOWN",
        strength=min(1.0, abs(score)),
        breakdown={src: f"{lean} {s:.2f}" for lean, s, src in votes},
    )


def apply_modifier(
    intent: OrderIntent,
    contracts: float,
    sm: SmartMoneySignal,
    weight: float,
) -> tuple[float, bool, str]:
    """(adjusted_contracts, vetoed, note). weight<=0 or NEUTRAL is a no-op."""
    if weight <= 0 or sm.lean == "NEUTRAL" or sm.strength <= 0:
        return contracts, False, "smart_money inert"
    direction = "UP" if intent in (OrderIntent.BUY_YES, OrderIntent.SELL_NO) else "DOWN"
    effect = weight * sm.strength
    if sm.lean == direction:
        return contracts * (1 + effect), False, f"smart_money agrees +{effect:.2f}"
    if effect >= VETO_THRESHOLD:
        return 0.0, True, f"smart_money veto ({sm.lean} vs {direction}, {effect:.2f})"
    return contracts * (1 - effect), False, f"smart_money disagrees -{effect:.2f}"
