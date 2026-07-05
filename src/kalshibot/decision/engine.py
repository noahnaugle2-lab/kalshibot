"""Claude decision engine: advisory trade decisions with deterministic fallback.

Contract (spec section 7):
- invoked ONLY on qualifying strategy signals, never on every scan
- async and non-blocking: the trading loop dispatches and moves on; the
  decision executes (or falls back) when the CLI returns
- staleness guard: the feature snapshot is timestamped; if the market has
  moved past tolerance by the time Claude answers, the decision is discarded
- on timeout / error / unparseable output: fall back to the deterministic
  strategy signal (or HOLD, per config)
- Claude's output is ADVISORY: the risk layer re-checks whatever comes out
- every prompt, raw stdout, parsed decision, latency, and disposition is
  logged to the decisions table
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from kalshibot.decision.claude_cli import ClaudeCLIRunner, CLIResult
from kalshibot.features.engine import FeatureSnapshot
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.signal import SmartMoneySignal
from kalshibot.strategies.base import PositionState, StrategySignal

logger = logging.getLogger(__name__)

PROMPT_VERSION = 1
DEFAULT_STALENESS_TOLERANCE_S = 10.0


class TradeDecision(BaseModel):
    action: Literal["BUY_YES", "BUY_NO", "SELL", "HOLD"]
    confidence: float = Field(ge=0, le=1)
    size_contracts: float = Field(default=0, ge=0)
    limit_price_cents: float | None = Field(default=None, ge=0, le=100)
    reasoning: str = ""


PROMPT_TEMPLATE = """You are the decision layer of a trading bot for Kalshi 15-minute crypto \
binary markets. A deterministic strategy produced a qualifying signal; decide whether to \
take it, adjust it, or hold. You cannot exceed the proposed size. Contracts pay $0.99 if \
correct, $0 if wrong; prices are in cents (1-99).

DATA:
{payload}

Respond with ONLY this JSON, no markdown, no commentary:
{{"action": "BUY_YES"|"BUY_NO"|"HOLD", "confidence": <0-1>, "size_contracts": <int>, \
"limit_price_cents": <1-99>, "reasoning": "<one sentence>"}}"""


def build_prompt(
    snapshot: FeatureSnapshot,
    signal: StrategySignal,
    smart: SmartMoneySignal,
    position: PositionState,
    session_pnl: float,
    risk_state: dict,
) -> str:
    payload = {
        "snapshot": snapshot.model_dump(exclude_none=True),
        "strategy_signal": {
            "intent": signal.intent.value,
            "contracts": signal.contracts,
            "limit_price": signal.limit_price,
            "execution": signal.execution,
            "reason": signal.reason,
        },
        "smart_money": {
            "lean": smart.lean, "strength": smart.strength,
            "breakdown": smart.breakdown,
        },
        "position": position.model_dump(exclude_none=True),
        "session_pnl_usd": round(session_pnl, 2),
        "risk_state": risk_state,
    }
    return PROMPT_TEMPLATE.format(payload=json.dumps(payload, separators=(",", ":")))


def parse_decision(text: str) -> TradeDecision:
    return TradeDecision.model_validate(json.loads(text))


class DecisionEngine:
    def __init__(
        self,
        runner: ClaudeCLIRunner,
        db: Database,
        *,
        staleness_tolerance_s: float = DEFAULT_STALENESS_TOLERANCE_S,
        fallback: Literal["signal", "hold"] = "signal",
    ) -> None:
        self.runner = runner
        self.db = db
        self.staleness_tolerance_s = staleness_tolerance_s
        self.fallback = fallback

    async def decide(
        self,
        asset: str,
        snapshot: FeatureSnapshot,
        signal: StrategySignal,
        smart: SmartMoneySignal,
        position: PositionState,
        session_pnl: float,
        risk_state: dict,
    ) -> tuple[TradeDecision | None, str]:
        """Returns (decision, disposition).

        disposition: 'claude' (validated decision within tolerance),
        'fallback' (use deterministic signal), 'hold' (do nothing).
        A None decision with 'fallback' means: execute the original signal.
        """
        prompt = build_prompt(snapshot, signal, smart, position, session_pnl, risk_state)
        result: CLIResult = await self.runner.run(prompt)

        decision: TradeDecision | None = None
        disposition: str
        error = result.error
        if result.ok and result.text:
            try:
                decision = parse_decision(result.text)
                disposition = "claude"
            except (json.JSONDecodeError, ValidationError) as exc:
                error = f"unparseable decision: {exc}"
                disposition = self.fallback
                decision = None
        else:
            disposition = self.fallback

        # staleness: if the world moved on while Claude thought, nothing that
        # was computed from this snapshot may trade — neither Claude's answer
        # nor the original signal (it is exactly as old)
        age = time.time() - snapshot.ts
        stale = age > self.staleness_tolerance_s
        if disposition == "claude" and stale:
            disposition = "stale_discard"
        elif disposition == "signal" and stale:
            disposition = "stale_discard"
        elif disposition == "signal":
            disposition = "fallback"
        elif disposition == "hold":
            disposition = "fallback_hold"

        self.db.add("decisions", {
            "ts": time.time(),
            "asset": asset,
            "market_ticker": snapshot.market_ticker,
            "prompt_version": PROMPT_VERSION,
            "prompt": prompt,
            "raw_stdout": result.raw_stdout[:20000],
            "parsed": decision.model_dump_json() if decision else None,
            "latency_ms": result.latency_ms,
            "snapshot_age_s": age,
            "disposition": disposition,
            "error": error,
            "cost_usd": result.cost_usd,
        })
        if disposition == "claude":
            return decision, "claude"
        if disposition == "fallback":
            return None, "fallback"
        return None, "hold"
