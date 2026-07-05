"""Nightly Claude analysis: ranking report in, parameter proposals out.

Proposals are NEVER auto-applied. Each proposed parameter set is replay-
validated against the recorded tape alongside a baseline replay of the
current parameters over the same windows, and both results land in the
proposals table for human review. A proposal without a replay result does
not exist as far as the table is concerned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import BaseModel, ValidationError

from kalshibot.config import AssetConfig
from kalshibot.decision.claude_cli import ClaudeCLIRunner
from kalshibot.persistence.db import Database
from kalshibot.sim.replay import ReplayEngine
from kalshibot.strategies.library import REGISTRY, build_strategy

logger = logging.getLogger(__name__)

ANALYSIS_TIMEOUT_S = 240.0

ANALYSIS_PROMPT = """You are the nightly analyst for a Kalshi 15-minute crypto trading bot \
running nine markets with per-asset strategies. Below are the current ranking report, \
per-asset calibration, and current strategy parameters.

{report}

CURRENT PER-ASSET PARAMS:
{params}

Available strategies and their parameters:
- latency_momentum: move_vol_mult, edge_threshold, contracts
- cross_asset_lead_lag: btc_move_threshold, edge_threshold, contracts
- mean_reversion_extremes: extreme, max_distance_z, edge_threshold, contracts
- thin_book_maker: min_spread_cents, model_margin, contracts

Write a brief analysis, then propose at most 3 parameter changes (only where the data \
supports them — small samples deserve no changes). Respond with ONLY this JSON:
{{"analysis": "<3-6 sentences>", "proposals": [{{"asset": "BTC", "strategy": \
"latency_momentum", "proposed_params": {{...}}, "rationale": "<1-2 sentences>"}}]}}"""


class Proposal(BaseModel):
    asset: str
    strategy: str
    proposed_params: dict
    rationale: str = ""


class AnalysisResponse(BaseModel):
    analysis: str
    proposals: list[Proposal] = []


async def run_nightly_analysis(
    db: Database,
    report_text: str,
    asset_configs: dict[str, AssetConfig],
    runner: ClaudeCLIRunner | None = None,
) -> tuple[str, int]:
    """Returns (analysis_text, n_proposals_written)."""
    runner = runner or ClaudeCLIRunner(model=None, timeout_s=ANALYSIS_TIMEOUT_S)
    params_view = {
        a: {"strategy": c.strategy, "params": c.strategy_params}
        for a, c in asset_configs.items()
    }
    prompt = ANALYSIS_PROMPT.format(
        report=report_text, params=json.dumps(params_view, indent=1)
    )
    result = await runner.run(prompt, timeout_s=ANALYSIS_TIMEOUT_S)
    if not result.ok or not result.text:
        logger.error("nightly analysis failed: %s", result.error)
        return f"(analysis failed: {result.error})", 0
    try:
        response = AnalysisResponse.model_validate(json.loads(result.text))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error("nightly analysis unparseable: %s", exc)
        return f"(analysis unparseable: {exc})", 0

    written = 0
    for proposal in response.proposals:
        if proposal.strategy not in REGISTRY:
            logger.warning("proposal for unknown strategy %s skipped", proposal.strategy)
            continue
        cfg = asset_configs.get(proposal.asset)
        current_params = cfg.strategy_params if cfg else {}
        try:
            proposed_result, baseline_result = await asyncio.to_thread(
                _replay_validate, db, proposal, current_params, cfg,
            )
        except Exception as exc:
            logger.error("replay validation failed for %s: %s", proposal.asset, exc)
            continue
        db.write_now("proposals", {
            "created_ts": time.time(),
            "asset": proposal.asset,
            "strategy": proposal.strategy,
            "current_params": json.dumps(current_params),
            "proposed_params": json.dumps(proposal.proposed_params),
            "rationale": proposal.rationale,
            "replay_run_id": proposed_result.run_id,
            "replay_summary": proposed_result.model_dump_json(),
            "baseline_replay_run_id": baseline_result.run_id if baseline_result else None,
            "baseline_replay_summary": (
                baseline_result.model_dump_json() if baseline_result else None
            ),
            "status": "pending",
        })
        written += 1
    return response.analysis, written


def _replay_validate(db, proposal: Proposal, current_params: dict, cfg):
    engine = ReplayEngine(db)
    proposed = engine.run(
        proposal.asset, build_strategy(proposal.strategy, proposal.proposed_params)
    )
    baseline = None
    if cfg and cfg.strategy:
        baseline = engine.run(
            proposal.asset, build_strategy(cfg.strategy, current_params)
        )
    return proposed, baseline
