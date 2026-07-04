"""Calibration scoring: model probability vs market implied vs actual outcomes.

Answers the predictability question per asset:
- Brier score of OUR model probability against settlements
- Brier score of the MARKET's implied probability (book mid) — a market whose
  own price is poorly calibrated is exploitable
- directional hit rate of the model
- edge hit rate: when model and market disagreed by >= threshold, how often
  was the model right? This is the exploitability signal that feeds strategy
  assignment.

Snapshots from the SETTLEMENT regime (final 90s) are excluded from headline
numbers — they're untradeable by risk rule — but reported per-regime.
Results persist to `calibration_reports` (one row per asset per run) so the
nightly job builds a time series of these scores as tape accumulates.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from kalshibot.persistence.db import Database

EDGE_DISAGREEMENT_THRESHOLD = 0.02
TRADEABLE_REGIMES = ("EARLY", "MID", "LATE")


@dataclass
class RegimeStats:
    n: int = 0
    brier_model_sum: float = 0.0
    brier_market_sum: float = 0.0
    model_hits: int = 0
    model_calls: int = 0

    @property
    def brier_model(self) -> float | None:
        return self.brier_model_sum / self.n if self.n else None

    @property
    def brier_market(self) -> float | None:
        return self.brier_market_sum / self.n if self.n else None

    @property
    def hit_rate(self) -> float | None:
        return self.model_hits / self.model_calls if self.model_calls else None


@dataclass
class CalibrationReport:
    asset: str
    n_windows: int = 0
    n_snapshots: int = 0
    overall: RegimeStats = field(default_factory=RegimeStats)
    per_regime: dict[str, RegimeStats] = field(default_factory=dict)
    edge_calls: int = 0
    edge_hits: int = 0
    bins: list[dict] = field(default_factory=list)  # calibration curve, 10 bins

    @property
    def edge_hit_rate(self) -> float | None:
        return self.edge_hits / self.edge_calls if self.edge_calls else None

    def to_json(self) -> str:
        def stats(s: RegimeStats) -> dict:
            return {"n": s.n, "brier_model": s.brier_model,
                    "brier_market": s.brier_market, "hit_rate": s.hit_rate}

        return json.dumps({
            "asset": self.asset, "n_windows": self.n_windows,
            "n_snapshots": self.n_snapshots, "overall": stats(self.overall),
            "per_regime": {k: stats(v) for k, v in self.per_regime.items()},
            "edge_hit_rate": self.edge_hit_rate, "edge_calls": self.edge_calls,
            "bins": self.bins,
        })


def score_asset(db: Database, asset: str) -> CalibrationReport | None:
    """Score every snapshot of every settled window for one asset."""
    rows = db.query(
        "SELECT s.features, st.result FROM signals s "
        "JOIN settlements st ON st.market_ticker = s.market_ticker "
        "WHERE s.asset = ? AND st.result IN ('yes','no')",
        (asset,),
    )
    if not rows:
        return None

    report = CalibrationReport(asset=asset)
    windows: set[str] = set()
    bin_counts = [[0, 0.0, 0] for _ in range(10)]  # [n, sum_pred, n_yes]

    for row in rows:
        f = json.loads(row["features"])
        model_p, implied_p = f.get("model_prob"), f.get("implied_prob")
        regime = f.get("regime", "EARLY")
        if model_p is None or implied_p is None:
            continue
        y = 1.0 if row["result"] == "yes" else 0.0
        windows.add(f["market_ticker"])
        report.n_snapshots += 1

        stats = report.per_regime.setdefault(regime, RegimeStats())
        for s in ([stats, report.overall] if regime in TRADEABLE_REGIMES else [stats]):
            s.n += 1
            s.brier_model_sum += (model_p - y) ** 2
            s.brier_market_sum += (implied_p - y) ** 2
            if model_p != 0.5:
                s.model_calls += 1
                s.model_hits += int((model_p > 0.5) == (y == 1.0))

        if regime in TRADEABLE_REGIMES:
            b = min(9, int(model_p * 10))
            bin_counts[b][0] += 1
            bin_counts[b][1] += model_p
            bin_counts[b][2] += int(y)
            if abs(model_p - implied_p) >= EDGE_DISAGREEMENT_THRESHOLD:
                report.edge_calls += 1
                report.edge_hits += int((model_p > implied_p) == (y == 1.0))

    report.n_windows = len(windows)
    report.bins = [
        {"bin": i, "n": n, "mean_pred": (sp / n if n else None),
         "observed": (ny / n if n else None)}
        for i, (n, sp, ny) in enumerate(bin_counts)
    ]
    return report


def run_calibration(db: Database, assets: list[str]) -> list[CalibrationReport]:
    run_ts = time.time()
    reports = []
    for asset in assets:
        report = score_asset(db, asset)
        if report is None:
            continue
        reports.append(report)
        db.write_now("calibration_reports", {
            "run_ts": run_ts,
            "asset": asset,
            "n_windows": report.n_windows,
            "n_snapshots": report.n_snapshots,
            "brier_model": report.overall.brier_model,
            "brier_market": report.overall.brier_market,
            "hit_rate": report.overall.hit_rate,
            "edge_hit_rate": report.edge_hit_rate,
            "edge_calls": report.edge_calls,
            "report": report.to_json(),
        })
    return reports
