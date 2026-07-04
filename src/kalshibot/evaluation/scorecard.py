"""Market evaluation scorecard: rank the nine markets apples to apples.

One scorecard per (asset, strategy) from settled sim positions (shadow or
replay — kept separate by run kind), joined with the latest calibration
report for the predictability axis. Every metric is recomputable from raw
rows; this module only aggregates.

Axes (spec section 5):
- predictability: brier_model vs brier_market, hit rate, edge hit rate
- profitability: profit factor, P/L ratio %, net pnl per contract, return
  on capital — gross and net side by side, so a strategy that only works
  pre-fee is exposed immediately
- tradability: fill rate (from sim_orders), signals/day, avg entry spread
- risk: max drawdown, longest losing streak, per-trade pnl volatility
- breakouts by entry regime and by UTC hour-of-day bucket

Ranking confidence comes from sample size: <30 trades = "insufficient",
30-99 = "low", 100+ = "ok". Recommendations (keep/retune/bench) only fire
at 30+ trades; below that the honest answer is "keep collecting".
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshibot.persistence.db import Database

MIN_TRADES_FOR_RECOMMENDATION = 30
MIN_TRADES_FOR_CONFIDENCE = 100


@dataclass
class ProfitStats:
    trades: int = 0
    wins: int = 0
    gross_win_sum: float = 0.0
    gross_loss_sum: float = 0.0   # stored positive
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    fees: float = 0.0
    contracts: float = 0.0
    capital: float = 0.0          # sum of entry cost (contracts x price)
    pnls: list[float] = field(default_factory=list)  # net, in trade order

    def add(self, pnl_gross: float, pnl_net: float, fees: float,
            contracts: float, capital: float) -> None:
        self.trades += 1
        self.wins += int(pnl_gross > 0)
        if pnl_gross > 0:
            self.gross_win_sum += pnl_gross
        else:
            self.gross_loss_sum += -pnl_gross
        self.pnl_gross += pnl_gross
        self.pnl_net += pnl_net
        self.fees += fees
        self.contracts += contracts
        self.capital += capital
        self.pnls.append(pnl_net)

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss_sum <= 0:
            return None if self.gross_win_sum <= 0 else float("inf")
        return self.gross_win_sum / self.gross_loss_sum

    @property
    def pl_ratio_pct(self) -> float | None:
        """avg win / avg loss as a percentage (the spec's P/L ratio %)."""
        losses = self.trades - self.wins
        if self.wins == 0 or losses == 0:
            return None
        avg_win = self.gross_win_sum / self.wins
        avg_loss = self.gross_loss_sum / losses
        return (avg_win / avg_loss) * 100 if avg_loss > 0 else None

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None

    @property
    def pnl_per_contract(self) -> float | None:
        return self.pnl_net / self.contracts if self.contracts else None

    @property
    def return_on_capital(self) -> float | None:
        return self.pnl_net / self.capital if self.capital > 0 else None

    @property
    def max_drawdown(self) -> float:
        peak = equity = 0.0
        max_dd = 0.0
        for pnl in self.pnls:
            equity += pnl
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        return max_dd

    @property
    def longest_losing_streak(self) -> int:
        worst = current = 0
        for pnl in self.pnls:
            current = current + 1 if pnl <= 0 else 0
            worst = max(worst, current)
        return worst

    @property
    def pnl_volatility(self) -> float | None:
        if len(self.pnls) < 2:
            return None
        mean = sum(self.pnls) / len(self.pnls)
        return math.sqrt(sum((p - mean) ** 2 for p in self.pnls) / (len(self.pnls) - 1))

    def as_dict(self) -> dict:
        return {
            "trades": self.trades, "wins": self.wins,
            "hit_rate": self.hit_rate, "profit_factor": self.profit_factor,
            "pl_ratio_pct": self.pl_ratio_pct,
            "pnl_gross": round(self.pnl_gross, 4), "pnl_net": round(self.pnl_net, 4),
            "fees": round(self.fees, 4), "pnl_per_contract": self.pnl_per_contract,
            "return_on_capital": self.return_on_capital,
            "max_drawdown": round(self.max_drawdown, 4),
            "longest_losing_streak": self.longest_losing_streak,
            "pnl_volatility": self.pnl_volatility,
        }


@dataclass
class Scorecard:
    asset: str
    strategy: str
    kind: str                      # shadow | replay
    overall: ProfitStats
    by_regime: dict[str, ProfitStats]
    by_hour: dict[int, ProfitStats]
    signals_per_day: float | None = None
    fill_rate_maker: float | None = None
    fill_rate_taker: float | None = None
    brier_model: float | None = None
    brier_market: float | None = None
    model_hit_rate: float | None = None
    edge_hit_rate: float | None = None

    @property
    def confidence(self) -> str:
        n = self.overall.trades
        if n >= MIN_TRADES_FOR_CONFIDENCE:
            return "ok"
        if n >= MIN_TRADES_FOR_RECOMMENDATION:
            return "low"
        return "insufficient"

    @property
    def recommendation(self) -> str:
        if self.overall.trades < MIN_TRADES_FOR_RECOMMENDATION:
            return "collect"
        pf = self.overall.profit_factor
        if pf is None:
            return "collect"
        if self.overall.pnl_net > 0 and pf >= 1.2:
            return "keep"
        if pf >= 0.8:
            return "retune"
        return "bench"


def build_scorecards(db: Database, kind: str = "shadow") -> list[Scorecard]:
    positions = db.query(
        "SELECT p.*, r.strategy AS strategy_key FROM sim_positions p "
        "JOIN sim_runs r ON r.run_id = p.run_id WHERE r.kind = ? "
        "ORDER BY p.entry_ts",
        (kind,),
    )
    cards: dict[tuple[str, str], Scorecard] = {}
    for pos in positions:
        key = (pos["asset"], pos["strategy_key"])
        card = cards.get(key)
        if card is None:
            card = cards[key] = Scorecard(
                asset=pos["asset"], strategy=pos["strategy_key"], kind=kind,
                overall=ProfitStats(), by_regime={}, by_hour={},
            )
        capital = (pos["contracts"] or 0) * (pos["avg_price"] or 0)
        args = (pos["pnl_gross"], pos["pnl_net"], pos["fees"] or 0,
                pos["contracts"] or 0, capital)
        card.overall.add(*args)
        regime = pos["entry_regime"] or "UNKNOWN"
        card.by_regime.setdefault(regime, ProfitStats()).add(*args)
        if pos["entry_ts"]:
            hour = datetime.fromtimestamp(pos["entry_ts"], tz=timezone.utc).hour
            card.by_hour.setdefault(hour, ProfitStats()).add(*args)

    for card in cards.values():
        _attach_tradability(db, card, kind)
        _attach_calibration(db, card)
    return sorted(
        cards.values(),
        key=lambda c: (c.overall.profit_factor or 0, c.overall.pnl_net),
        reverse=True,
    )


def _attach_tradability(db: Database, card: Scorecard, kind: str) -> None:
    orders = db.query(
        "SELECT o.execution, o.status, COUNT(*) AS n, MIN(o.ts) AS t0, MAX(o.ts) AS t1 "
        "FROM sim_orders o JOIN sim_runs r ON r.run_id = o.run_id "
        "WHERE o.asset = ? AND r.kind = ? AND r.strategy = ? "
        "GROUP BY o.execution, o.status",
        (card.asset, kind, card.strategy),
    )
    maker_total = maker_filled = taker_total = taker_filled = 0
    t0 = t1 = None
    for row in orders:
        n = row["n"]
        t0 = min(t0, row["t0"]) if t0 else row["t0"]
        t1 = max(t1, row["t1"]) if t1 else row["t1"]
        if row["execution"] == "maker":
            maker_total += n
            if row["status"] == "filled":
                maker_filled += n
        else:
            taker_total += n
            if row["status"] in ("filled", "partial"):
                taker_filled += n
    card.fill_rate_maker = maker_filled / maker_total if maker_total else None
    card.fill_rate_taker = taker_filled / taker_total if taker_total else None
    if t0 and t1 and t1 > t0:
        days = max((t1 - t0) / 86400, 1 / 96)  # floor: one window
        card.signals_per_day = (maker_total + taker_total) / days


def _attach_calibration(db: Database, card: Scorecard) -> None:
    rows = db.query(
        "SELECT * FROM calibration_reports WHERE asset = ? "
        "ORDER BY run_ts DESC LIMIT 1", (card.asset,),
    )
    if rows:
        card.brier_model = rows[0]["brier_model"]
        card.brier_market = rows[0]["brier_market"]
        card.model_hit_rate = rows[0]["hit_rate"]
        card.edge_hit_rate = rows[0]["edge_hit_rate"]


def persist_scorecards(db: Database, cards: list[Scorecard]) -> float:
    run_ts = time.time()
    for rank, card in enumerate(cards, start=1):
        db.write_now("scorecards", {
            "run_ts": run_ts, "rank": rank, "kind": card.kind,
            "asset": card.asset, "strategy": card.strategy,
            "trades": card.overall.trades,
            "profit_factor": _finite(card.overall.profit_factor),
            "pl_ratio_pct": card.overall.pl_ratio_pct,
            "pnl_gross": round(card.overall.pnl_gross, 4),
            "pnl_net": round(card.overall.pnl_net, 4),
            "hit_rate": card.overall.hit_rate,
            "brier_model": card.brier_model, "brier_market": card.brier_market,
            "signals_per_day": card.signals_per_day,
            "fill_rate_maker": card.fill_rate_maker,
            "fill_rate_taker": card.fill_rate_taker,
            "max_drawdown": card.overall.max_drawdown,
            "confidence": card.confidence,
            "recommendation": card.recommendation,
            "detail": json.dumps({
                "overall": card.overall.as_dict(),
                "by_regime": {k: v.as_dict() for k, v in card.by_regime.items()},
                "by_hour": {str(k): v.as_dict() for k, v in card.by_hour.items()},
            }),
        })
    return run_ts


def _finite(x: float | None) -> float | None:
    return None if x is None or math.isinf(x) else x


def render_ranking(cards: list[Scorecard]) -> str:
    lines = [
        "MARKET RANKING (shadow campaign)",
        f"{'#':<3}{'ASSET':<6}{'STRATEGY':<24}{'N':>4} {'PF':>6} {'P/L%':>7} "
        f"{'NET':>8} {'GROSS':>8} {'HIT':>5} {'Bm':>6} {'Bmkt':>6} {'CONF':>12} REC",
    ]
    for i, c in enumerate(cards, start=1):
        pf = c.overall.profit_factor
        pf_s = "inf" if pf is not None and math.isinf(pf) else (f"{pf:.2f}" if pf else "-")
        lines.append(
            f"{i:<3}{c.asset:<6}{c.strategy.split(':')[0]:<24}{c.overall.trades:>4} "
            f"{pf_s:>6} {_fmt(c.overall.pl_ratio_pct, 1):>7} "
            f"{c.overall.pnl_net:>8.2f} {c.overall.pnl_gross:>8.2f} "
            f"{_fmt(c.overall.hit_rate, 2):>5} {_fmt(c.brier_model, 3):>6} "
            f"{_fmt(c.brier_market, 3):>6} {c.confidence:>12} {c.recommendation}"
        )
    return "\n".join(lines)


def _fmt(x: float | None, places: int) -> str:
    return "-" if x is None else f"{x:.{places}f}"
