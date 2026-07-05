// src/api/types.ts — KalshiBot dashboard data contract.
// This file IS the deliverable boundary: the FastAPI backend implements this
// exact shape. Do not drift from it without versioning.
// All timestamps are epoch seconds (float). All prices/probabilities are
// dollars 0–1 (display as cents).
//
// Contract amendments vs the original handoff (backend moved to
// FeatureSnapshot v2): schema_version is a number, smart_lean/smart_strength
// were added, and most market/feature fields are nullable — the UI must
// render nulls gracefully.

export type Mode = 'SHADOW' | 'DEMO' | 'LIVE';
export type Regime = 'EARLY' | 'MID' | 'LATE' | 'SETTLEMENT';
export type Asset =
  | 'BTC' | 'ETH' | 'SOL' | 'ZEC' | 'HYPE'
  | 'XRP' | 'DOGE' | 'BNB' | 'NEAR';
export type Lean = 'UP' | 'DOWN';

// ---- GET /api/status ----
export interface FeedStatus {
  connected: boolean;
  ticks_per_min?: number;
  req_per_sec?: number;
}
export interface Status {
  mode: Mode;
  started_at: number;
  clock_offset_ms: number;
  kill_switch_engaged: boolean;
  feeds: {
    coinbase: FeedStatus;
    binance_us: FeedStatus;
    kalshi: FeedStatus;
  };
  db: { size_mb: number; signals_rows: number };
}

// ---- FeatureSnapshot (§4.1, v2) ----
export interface FeatureSnapshot {
  schema_version: number;
  ts: number;
  asset: Asset;
  market_ticker: string;
  spot: number | null;
  spot_source: 'coinbase' | 'binance_us' | null;
  spot_age_seconds: number | null;
  floor_strike: number | null;
  distance_dollars: number | null;
  distance_z: number | null;
  seconds_remaining: number;
  regime: Regime;
  ret_30s: number | null;
  ret_1m: number | null;
  ret_5m: number | null;
  realized_vol_5m: number | null;
  btc_ret_30s: number | null;      // null for BTC itself
  btc_ret_1m: number | null;
  btc_implied_prob: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  implied_prob: number | null;
  spread_cents: number | null;
  depth_yes_within_2c: number | null;
  depth_no_within_2c: number | null;
  flow_imbalance: number | null;
  book_age_seconds: number | null;
  model_prob: number | null;
  edge_yes_gross: number | null;
  edge_yes_net: number | null;
  edge_no_gross: number | null;
  edge_no_net: number | null;
  smart_lean?: Lean | null;
  smart_strength?: number | null;
}

// ---- GET /api/live → LiveAsset[] (length 9) ----
export interface SmartMoneyLean {
  lean: Lean;
  strength: number;                // 0–1
  source_breakdown: Record<string, number>;
}
export interface Position {
  side: 'yes' | 'no';
  contracts: number;
  avg_price: number;
  unrealized_pnl: number;
}
export interface SessionPnl { gross: number; net: number; trades: number }
export interface LiveAsset {
  asset: Asset;
  paused: boolean;
  market: {
    ticker: string;
    open_ts: number;
    close_ts: number;
    floor_strike: number;
    status: string;
  };
  snapshot: FeatureSnapshot;
  smart_money: SmartMoneyLean | null;
  position: Position | null;
  session_pnl: SessionPnl;
}

// ---- GET /api/leaderboard ----
export type LeaderboardBasis = 'net' | 'gross';
export type SmartMoneyFilter = 'with' | 'without' | 'both';
export type Confidence = 'high' | 'medium' | 'low';
export type Recommendation = 'keep' | 'retune' | 'bench';
export interface LeaderboardRow {
  rank: number;
  asset: Asset;
  strategy: string;
  profit_factor: number;
  pl_ratio_pct: number;
  net_pnl_per_contract: number;
  return_on_capital: number;
  hit_rate: number;
  brier_model: number;
  brier_market: number;
  signals_per_day: number;
  fill_rate: number;
  avg_spread_cents: number;
  avg_slippage_cents: number;
  max_drawdown: number;
  longest_losing_streak: number;
  pnl_volatility: number;
  n_trades: number;
  n_settled_windows: number;
  confidence: Confidence;
  recommendation: Recommendation;
}
export interface LeaderboardResponse {
  computed_at: number;
  rows: LeaderboardRow[];
}

// ---- GET /api/trades ----
export interface ClaudeDecision {
  confidence: number;
  reasoning: string;
  latency_ms: number;
}
export interface Trade {
  id: number;
  ts: number;
  asset: Asset;
  market_ticker: string;
  intent: 'BUY_YES' | 'BUY_NO';
  contracts: number;
  limit_price: number;
  avg_fill_price: number;
  fees: number;
  result: 'yes' | 'no' | null;     // null = unsettled
  pnl_net: number;
  strategy: string;
  regime_at_entry: Regime;
  smart_money_lean: Lean | null;
  decision_source: 'claude' | 'baseline' | null;
  claude: ClaudeDecision | null;
}
export interface TradesResponse { cursor: string; trades: Trade[] }

// ---- GET /api/equity ----
export interface EquitySeries {
  asset: Asset;
  points: [ts: number, cumulative_pnl: number][];
}
export interface EquityResponse { series: EquitySeries[] }

// ---- GET /api/smartmoney ----
export interface FlowPattern {
  id: string;
  description: string;
  hit_rate_30d: number;
  n_30d: number;
  status: 'active' | 'benched';
  current_leans: Partial<Record<Asset, Lean | null>>;
}
export interface Wallet {
  address: string;
  win_rate: number;
  ci_low: number;
  ci_high: number;
  n_resolved: number;
  profit_usd: number;
  avg_entry_seconds_after_open: number;
  current_positions: { asset: Asset; side: Lean; size_usd: number }[];
}
export interface SmartMoneyResponse {
  patterns: FlowPattern[];
  wallets: Wallet[];
  merged_leans: Partial<Record<Asset, { lean: Lean; strength: number }>>;
}

// ---- Config ----
export interface AssetConfig {
  enabled: boolean;
  paused: boolean;
  strategy: string | null;
  strategy_params: Record<string, unknown>;
  edge_threshold_cents: number;
  max_position_contracts: number;
  smart_money_weight: number;      // 0–1
  late_window_enabled: boolean;
}
export interface Blackout {
  label: string;
  start: number;
  end: number;
  affected_assets: Asset[] | 'ALL';
}

// ---- Control ----
export interface KillResponse { engaged: boolean; cancelled_orders: number }

// ---- WS /ws/live push messages ----
export type WsMessage =
  | { type: 'snapshot'; asset: Asset; data: LiveAsset }
  | { type: 'trade'; data: Trade }
  | { type: 'settlement'; data: { asset: Asset; market_ticker: string; result: 'yes' | 'no'; pnl_net: number } }
  | { type: 'status'; data: Status };
