"""Decision engine tests: parsing, fences, fallback, staleness (fake CLI)."""

import json
import time

import pytest

from kalshibot.decision.claude_cli import ClaudeCLIRunner, CLIResult, strip_fences
from kalshibot.decision.engine import DecisionEngine, build_prompt, parse_decision
from kalshibot.features.engine import FeatureSnapshot, Regime
from kalshibot.kalshi.models import OrderIntent
from kalshibot.persistence.db import Database
from kalshibot.smartmoney.signal import SmartMoneySignal
from kalshibot.strategies.base import PositionState, StrategySignal

GOOD = '{"action":"BUY_YES","confidence":0.7,"size_contracts":8,"limit_price_cents":54,"reasoning":"edge"}'


def test_strip_fences_variants():
    assert strip_fences(f"```json\n{GOOD}\n```") == GOOD
    assert strip_fences(f"```\n{GOOD}\n```") == GOOD
    assert strip_fences(GOOD) == GOOD


def test_parse_decision_valid_and_bounds():
    d = parse_decision(GOOD)
    assert d.action == "BUY_YES" and d.confidence == 0.7
    with pytest.raises(Exception):
        parse_decision('{"action":"YOLO","confidence":0.5}')
    with pytest.raises(Exception):
        parse_decision('{"action":"HOLD","confidence":1.5}')
    with pytest.raises(Exception):
        parse_decision("the market looks good so I would buy")


def snap(age_s: float = 0.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts=time.time() - age_s, asset="BTC", market_ticker="M",
        regime=Regime.MID, seconds_remaining=400,
    )


def sig() -> StrategySignal:
    return StrategySignal(intent=OrderIntent.BUY_YES, contracts=10, limit_price=0.5)


class FakeRunner(ClaudeCLIRunner):
    def __init__(self, result: CLIResult):
        super().__init__()
        self._result = result

    async def run(self, prompt, timeout_s=None):
        return self._result


async def decide_with(db, result: CLIResult, age_s=0.0, fallback="signal"):
    engine = DecisionEngine(FakeRunner(result), db,
                            staleness_tolerance_s=10.0, fallback=fallback)
    return await engine.decide(
        "BTC", snap(age_s), sig(), SmartMoneySignal.neutral(),
        PositionState(market_ticker="M"), 0.0, {},
    )


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "d.db")
    yield d
    d.close()


async def flush(db):
    import asyncio
    task = asyncio.get_event_loop().create_task(db.run_flusher(0.01))
    import asyncio as aio
    await aio.sleep(0.05)
    task.cancel()
    try:
        await task
    except aio.CancelledError:
        pass


async def test_valid_decision_used(db):
    decision, disp = await decide_with(db, CLIResult(ok=True, text=GOOD, latency_ms=100))
    assert disp == "claude" and decision.action == "BUY_YES"
    await flush(db)
    row = db.query("SELECT * FROM decisions")[0]
    assert row["disposition"] == "claude" and row["parsed"]


async def test_fenced_output_is_handled_upstream(db):
    # runner strips fences before the engine sees text
    fenced = CLIResult(ok=True, text=strip_fences(f"```json\n{GOOD}\n```"))
    decision, disp = await decide_with(db, fenced)
    assert disp == "claude"


async def test_unparseable_falls_back_to_signal(db):
    decision, disp = await decide_with(db, CLIResult(ok=True, text="not json"))
    assert decision is None and disp == "fallback"
    await flush(db)
    assert db.query("SELECT disposition FROM decisions")[0]["disposition"] == "fallback"


async def test_timeout_falls_back(db):
    decision, disp = await decide_with(db, CLIResult(ok=False, error="timeout after 10s"))
    assert decision is None and disp == "fallback"


async def test_fallback_hold_config(db):
    decision, disp = await decide_with(
        db, CLIResult(ok=False, error="timeout"), fallback="hold")
    assert decision is None and disp == "hold"


async def test_stale_snapshot_discards_even_valid_decision(db):
    decision, disp = await decide_with(
        db, CLIResult(ok=True, text=GOOD), age_s=30.0)
    assert decision is None and disp == "hold"
    await flush(db)
    assert db.query("SELECT disposition FROM decisions")[0]["disposition"] == "stale_discard"


async def test_stale_fallback_also_discards(db):
    decision, disp = await decide_with(
        db, CLIResult(ok=False, error="timeout"), age_s=30.0)
    assert decision is None and disp == "hold"


def test_prompt_contains_payload_and_contract():
    prompt = build_prompt(snap(), sig(), SmartMoneySignal(lean="UP", strength=0.4),
                          PositionState(market_ticker="M"), 1.5,
                          {"daily_pnl_asset": -2.0})
    assert '"intent":"BUY_YES"' in prompt
    assert "ONLY this JSON" in prompt
    assert '"lean":"UP"' in prompt.replace(" ", "")
