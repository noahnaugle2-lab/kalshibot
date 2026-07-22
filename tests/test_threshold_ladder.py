import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_threshold_ladder.py"
SPEC = importlib.util.spec_from_file_location("live_threshold_ladder", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
metric = MODULE.metric
rank = MODULE.rank


def _row(ts, asset, regime, btc_move, edge, pnl):
    return {
        "created_ts": ts,
        "asset": asset,
        "regime": regime,
        "btc_move": btc_move,
        "edge": edge,
        "pnl": pnl,
    }


def test_metric_reports_profit_factor():
    assert metric([{"pnl": 3}, {"pnl": -2}, {"pnl": 1}]) == {
        "n": 3,
        "wins": 2,
        "net": 2.0,
        "profit_factor": 2.0,
    }


def test_rank_keeps_regimes_and_thresholds_separate():
    rows = [
        _row(1, "SOL", "MID", 0.0011, 0.03, 2),
        _row(2, "SOL", "LATE", 0.0011, 0.03, -5),
        _row(3, "SOL", "MID", 0.0011, 0.03, 2),
        _row(4, "SOL", "LATE", 0.0011, 0.03, -5),
    ]

    result = rank(rows, validation_fraction=0.5)
    mid = next(
        candidate for candidate in result["candidates"]
        if candidate["asset"] == "SOL"
        and candidate["regimes"] == ["MID"]
        and candidate["btc_threshold"] == 0.001
        and candidate["edge_threshold"] == 0.03
    )

    assert mid["all"]["n"] == 2
    assert mid["all"]["net"] == 4.0
