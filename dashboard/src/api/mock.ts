// src/api/mock.ts — realistic generated fixtures + simulated WS pushes.
// Active when VITE_API_MOCK=1 (default in dev). Everything still flows
// through src/api/client.ts — this module is only reached from there.
//
// Edge cases included per the handoff "State Management" section:
//   - NEAR opens inside the SETTLEMENT blackout (<90s remaining)
//   - DOGE is a stale asset (snapshot ts frozen, age grows past 5s)
//   - DAY 1 scenario: empty leaderboard/history, zero PnL, no positions
//   - ZEC (n=41) / NEAR (n=24) low-sample & insufficient leaderboard rows
//   - whale_follow_late is a benched flow pattern
//   - kill-engaged state via POST /api/control/kill (toggles)
import type {
  Asset,
  AssetConfig,
  Blackout,
  EquityResponse,
  FeatureSnapshot,
  KillResponse,
  Lean,
  LeaderboardBasis,
  LeaderboardResponse,
  LeaderboardRow,
  LiveAsset,
  Position,
  Regime,
  SmartMoneyFilter,
  SmartMoneyResponse,
  Status,
  Trade,
  TradesResponse,
  WsMessage,
} from './types';

export type Scenario = 'day1' | 'day9';

const nowSec = () => Date.now() / 1000;

// Seeded LCG so fixtures are deterministic per scenario load.
let seed = 42;
function rng(): number {
  seed = (seed * 1664525 + 1013904223) % 4294967296;
  return seed / 4294967296;
}

function regimeOf(secondsRemaining: number): Regime {
  if (secondsRemaining > 600) return 'EARLY';
  if (secondsRemaining > 300) return 'MID';
  if (secondsRemaining > 90) return 'LATE';
  return 'SETTLEMENT';
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

// ---------------------------------------------------------------- asset sims

interface SimAsset {
  sym: Asset;
  strike: number;
  spot: number;
  vol: number;
  dp: number;
  openTs: number;
  closeTs: number;
  ticker: string;
  imp: number;
  mod: number;
  spreadC: number; // spread in cents
  depthYes: number;
  depthNo: number;
  bookAge: number;
  pos: Position | null;
  smLean: Lean | null;
  smStr: number;
  sessNet: number;
  sessTrades: number;
  stale: boolean;
  frozenTs: number; // snapshot ts when stale
  nullRets: boolean; // ZEC: thin feed → null return features
  nullFlow: boolean; // XRP: paused → null flow imbalance
  series: number[];
}

interface SimDef {
  sym: Asset;
  strike: number;
  spot: number;
  vol: number;
  dp: number;
  cd: number; // seconds remaining at world init
  imp: number;
  mod: number;
  spreadC: number;
  depthYes: number;
  bookAge: number;
  pos: Position | null;
  smLean: Lean | null;
  smStr: number;
  sessNet: number;
  sessTrades: number;
  paused?: boolean;
  stale?: boolean;
  nullRets?: boolean;
  nullFlow?: boolean;
}

// Mirrors the hi-fi prototype's fixture values.
const SIM_DEFS: SimDef[] = [
  { sym: 'BTC', strike: 63124.02, spot: 63209.98, vol: 9, dp: 2, cd: 205, imp: 0.9925, mod: 0.9545, spreadC: 0.1, depthYes: 79794, bookAge: 1.6, pos: null, smLean: 'UP', smStr: 0.62, sessNet: 4.12, sessTrades: 5 },
  { sym: 'ETH', strike: 2838.1, spot: 2841.37, vol: 0.7, dp: 2, cd: 512, imp: 0.58, mod: 0.66, spreadC: 1.0, depthYes: 12400, bookAge: 0.4, pos: { side: 'yes', contracts: 20, avg_price: 0.41, unrealized_pnl: 3.4 }, smLean: 'UP', smStr: 0.62, sessNet: 9.85, sessTrades: 7 },
  { sym: 'SOL', strike: 143.2, spot: 142.55, vol: 0.05, dp: 2, cd: 748, imp: 0.38, mod: 0.35, spreadC: 1.0, depthYes: 8100, bookAge: 0.8, pos: null, smLean: 'DOWN', smStr: 0.41, sessNet: -2.1, sessTrades: 3 },
  { sym: 'ZEC', strike: 47.95, spot: 48.12, vol: 0.02, dp: 2, cd: 385, imp: 0.61, mod: 0.68, spreadC: 2.5, depthYes: 1200, bookAge: 2.1, pos: null, smLean: null, smStr: 0, sessNet: 0.85, sessTrades: 2, nullRets: true },
  { sym: 'HYPE', strike: 39.18, spot: 39.402, vol: 0.02, dp: 3, cd: 640, imp: 0.72, mod: 0.79, spreadC: 1.0, depthYes: 3400, bookAge: 0.6, pos: { side: 'yes', contracts: 40, avg_price: 0.66, unrealized_pnl: 2.4 }, smLean: 'UP', smStr: 0.55, sessNet: 6.2, sessTrades: 4 },
  { sym: 'XRP', strike: 2.8092, spot: 2.814, vol: 0.0009, dp: 4, cd: 121, imp: 0.77, mod: 0.74, spreadC: 1.0, depthYes: 5500, bookAge: 1.1, pos: null, smLean: null, smStr: 0, sessNet: 0, sessTrades: 0, paused: true, nullFlow: true },
  { sym: 'DOGE', strike: 0.3141, spot: 0.31245, vol: 0.00008, dp: 5, cd: 335, imp: 0.29, mod: 0.31, spreadC: 1.0, depthYes: 6700, bookAge: 12.4, pos: null, smLean: 'DOWN', smStr: 0.38, sessNet: -1.35, sessTrades: 2, stale: true },
  { sym: 'BNB', strike: 610.9, spot: 612.44, vol: 0.25, dp: 2, cd: 458, imp: 0.64, mod: 0.61, spreadC: 1.0, depthYes: 4200, bookAge: 0.9, pos: { side: 'no', contracts: 15, avg_price: 0.35, unrealized_pnl: -0.45 }, smLean: 'UP', smStr: 0.3, sessNet: 1.9, sessTrades: 3 },
  { sym: 'NEAR', strike: 4.201, spot: 4.182, vol: 0.002, dp: 3, cd: 62, imp: 0.18, mod: 0.15, spreadC: 1.0, depthYes: 2800, bookAge: 0.7, pos: null, smLean: 'DOWN', smStr: 0.52, sessNet: -3.4, sessTrades: 4 },
];

const STRATEGY_BY_ASSET: Record<Asset, string> = {
  BTC: 'latency_momentum_v1', ETH: 'latency_momentum_v1', SOL: 'latency_momentum_v1',
  ZEC: 'mean_revert_v2', HYPE: 'vol_breakout_v1', XRP: 'latency_momentum_v1',
  DOGE: 'vol_breakout_v1', BNB: 'mean_revert_v2', NEAR: 'mean_revert_v2',
};

interface LbBase {
  asset: Asset; strategy: string; pf: number; pl: number; npc: number; roc: number;
  hit: number; bm: number; bk: number; sigd: number; fill: number; sprd: number;
  slip: number; dd: number; streak: number; vol: number; n: number; nw: number;
  conf: 'high' | 'medium' | 'low'; rec: 'keep' | 'retune' | 'bench'; smDelta: number;
}

const LB_BASE: LbBase[] = [
  { asset: 'ETH', strategy: 'latency_momentum_v1', pf: 1.84, pl: 142, npc: 0.031, roc: 0.19, hit: 0.58, bm: 0.19, bk: 0.24, sigd: 41, fill: 0.83, sprd: 1.2, slip: 0.4, dd: -42, streak: 6, vol: 8.2, n: 214, nw: 288, conf: 'high', rec: 'keep', smDelta: 0.11 },
  { asset: 'BTC', strategy: 'latency_momentum_v1', pf: 1.52, pl: 118, npc: 0.024, roc: 0.15, hit: 0.56, bm: 0.21, bk: 0.23, sigd: 38, fill: 0.87, sprd: 0.8, slip: 0.3, dd: -38, streak: 5, vol: 7.1, n: 302, nw: 288, conf: 'high', rec: 'keep', smDelta: 0.06 },
  { asset: 'HYPE', strategy: 'vol_breakout_v1', pf: 1.41, pl: 104, npc: 0.028, roc: 0.14, hit: 0.57, bm: 0.2, bk: 0.22, sigd: 22, fill: 0.74, sprd: 2.1, slip: 0.9, dd: -51, streak: 7, vol: 9.8, n: 96, nw: 265, conf: 'medium', rec: 'keep', smDelta: 0.14 },
  { asset: 'ZEC', strategy: 'mean_revert_v2', pf: 1.21, pl: 64, npc: 0.014, roc: 0.08, hit: 0.54, bm: 0.23, bk: 0.24, sigd: 9, fill: 0.61, sprd: 2.5, slip: 1.2, dd: -29, streak: 4, vol: 5.5, n: 41, nw: 240, conf: 'low', rec: 'retune', smDelta: -0.02 },
  { asset: 'SOL', strategy: 'latency_momentum_v1', pf: 1.18, pl: 58, npc: 0.011, roc: 0.07, hit: 0.53, bm: 0.22, bk: 0.22, sigd: 33, fill: 0.81, sprd: 1.1, slip: 0.5, dd: -47, streak: 8, vol: 8.9, n: 187, nw: 288, conf: 'medium', rec: 'retune', smDelta: 0.03 },
  { asset: 'BNB', strategy: 'mean_revert_v2', pf: 1.09, pl: 31, npc: 0.006, roc: 0.04, hit: 0.52, bm: 0.23, bk: 0.22, sigd: 18, fill: 0.78, sprd: 1.4, slip: 0.6, dd: -44, streak: 6, vol: 6.4, n: 143, nw: 281, conf: 'medium', rec: 'retune', smDelta: -0.04 },
  { asset: 'XRP', strategy: 'latency_momentum_v1', pf: 0.94, pl: -18, npc: -0.004, roc: -0.02, hit: 0.5, bm: 0.24, bk: 0.23, sigd: 27, fill: 0.84, sprd: 1.0, slip: 0.4, dd: -56, streak: 9, vol: 7.7, n: 118, nw: 288, conf: 'medium', rec: 'bench', smDelta: 0.01 },
  { asset: 'DOGE', strategy: 'vol_breakout_v1', pf: 0.87, pl: -34, npc: -0.008, roc: -0.05, hit: 0.49, bm: 0.25, bk: 0.24, sigd: 15, fill: 0.72, sprd: 1.8, slip: 0.8, dd: -63, streak: 11, vol: 9.1, n: 77, nw: 270, conf: 'low', rec: 'bench', smDelta: -0.06 },
  { asset: 'NEAR', strategy: 'mean_revert_v2', pf: 0.71, pl: -52, npc: -0.013, roc: -0.09, hit: 0.46, bm: 0.26, bk: 0.24, sigd: 7, fill: 0.58, sprd: 2.4, slip: 1.1, dd: -71, streak: 12, vol: 6.9, n: 24, nw: 233, conf: 'low', rec: 'bench', smDelta: 0 },
];

const REASONINGS = [
  'Spot momentum +0.26% over 1m with the book lagging: implied 58c vs model 66c. Flow imbalance 0.59 confirms directional pressure. Entering YES in MID regime; edge clears the threshold after spread and fees.',
  'Mean-reversion setup: 5m return -0.42% with realized vol elevated and depth thin on the continuation side. Book is overpricing the move. Buying NO; sized down for slippage on a 2.5c spread.',
  'Late-window convergence trade: model 88c vs book 84c with 210s remaining and spot 1.4 sigma above strike. Smart-money lean UP 0.55 raises confidence. Entering YES; will not add past the 90s blackout.',
  'Vol breakout: 30s return spiked 3.1x the 5m realized vol baseline with flow imbalance 0.71. Book has not repriced. Buying YES at the ask; expecting reprice within 60s.',
];

// ---------------------------------------------------------------- world

interface World {
  scenario: Scenario;
  t0: number;
  startedAt: number;
  computedAt: number;
  kill: boolean;
  sims: SimAsset[];
  trades: Trade[];
  nextTradeId: number;
  equity: Record<Asset, [number, number][]>;
  config: Record<Asset, AssetConfig>;
  blackouts: Blackout[];
}

let world: World = initWorld('day9');

function mkTicker(sym: Asset, closeTs: number, strike: number): string {
  const d = new Date(closeTs * 1000);
  const months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  const stamp = `${String(d.getUTCFullYear()).slice(2)}${months[d.getUTCMonth()]}${String(d.getUTCDate()).padStart(2, '0')}${String(d.getUTCHours()).padStart(2, '0')}${String(d.getUTCMinutes()).padStart(2, '0')}`;
  return `KX${sym}15M-${stamp}-${Math.max(1, Math.round(strike) % 89)}`;
}

function walk(n: number, start: number, vol: number): number[] {
  const p = [start];
  for (let i = 1; i < n; i++) p.push(p[i - 1] + (rng() - 0.46) * vol);
  return p;
}

function initWorld(scenario: Scenario): World {
  seed = 42;
  const t0 = nowSec();
  const day1 = scenario === 'day1';

  const sims: SimAsset[] = SIM_DEFS.map((d) => {
    const closeTs = t0 + d.cd;
    const series = walk(48, d.spot - d.vol * 8, d.vol * 1.6);
    series[series.length - 1] = d.spot;
    return {
      sym: d.sym,
      strike: d.strike,
      spot: d.spot,
      vol: d.vol,
      dp: d.dp,
      openTs: closeTs - 900,
      closeTs,
      ticker: mkTicker(d.sym, closeTs, d.strike),
      imp: d.imp,
      mod: d.mod,
      spreadC: d.spreadC,
      depthYes: d.depthYes,
      depthNo: d.depthYes * 0.26,
      bookAge: d.bookAge,
      pos: day1 ? null : d.pos ? { ...d.pos } : null,
      smLean: d.smLean,
      smStr: d.smStr,
      sessNet: day1 ? 0 : d.sessNet,
      sessTrades: day1 ? 0 : d.sessTrades,
      stale: !!d.stale,
      frozenTs: t0 - 12,
      nullRets: !!d.nullRets,
      nullFlow: !!d.nullFlow,
      series,
    };
  });

  // ---- config ----
  const config = {} as Record<Asset, AssetConfig>;
  for (const d of SIM_DEFS) {
    config[d.sym] = {
      enabled: true,
      paused: !!d.paused,
      strategy: STRATEGY_BY_ASSET[d.sym],
      strategy_params: {},
      edge_threshold_cents: d.sym === 'ZEC' || d.sym === 'HYPE' || d.sym === 'NEAR' ? 5 : 3,
      max_position_contracts: 100,
      smart_money_weight: d.sym === 'ETH' || d.sym === 'HYPE' ? 0.4 : 0,
      late_window_enabled: true,
    };
  }

  // ---- equity: 90 points over 9 days per asset ----
  const equity = {} as Record<Asset, [number, number][]>;
  for (const d of SIM_DEFS) {
    if (day1) {
      equity[d.sym] = [[t0, 0]];
      continue;
    }
    const lbr = LB_BASE.find((r) => r.asset === d.sym)!;
    const drift = (lbr.pf - 1) * 1.6;
    const pts: [number, number][] = [[t0 - 777600, 0]];
    let v = 0;
    for (let i = 1; i < 90; i++) {
      v += drift + (rng() - 0.5) * 6;
      pts.push([t0 - 777600 + i * (777600 / 89), v]);
    }
    equity[d.sym] = pts;
  }

  // ---- trades: ~84 settled over 9 days + a few unsettled in-window fills ----
  const trades: Trade[] = [];
  let id = 991;
  if (!day1) {
    // Unsettled fills inside the current window for assets holding positions.
    const inWindow: Array<[Asset, number, 'BUY_YES' | 'BUY_NO', number, number]> = [
      ['ETH', t0 - 120, 'BUY_YES', 10, 0.415],
      ['ETH', t0 - 300, 'BUY_YES', 10, 0.405],
      ['HYPE', t0 - 200, 'BUY_YES', 40, 0.66],
      ['BNB', t0 - 400, 'BUY_NO', 15, 0.35],
    ];
    for (const [asset, ts, intent, qty, price] of inWindow) {
      const sim = sims.find((s) => s.sym === asset)!;
      trades.push({
        id: id--,
        ts,
        asset,
        market_ticker: sim.ticker,
        intent,
        contracts: qty,
        limit_price: price,
        avg_fill_price: price,
        fees: +(qty * 0.017 * (1 - Math.abs(price - 0.5))).toFixed(2),
        result: null,
        pnl_net: 0,
        strategy: STRATEGY_BY_ASSET[asset],
        regime_at_entry: 'MID',
        smart_money_lean: sim.smLean,
        decision_source: 'claude',
        claude: {
          confidence: +(0.6 + rng() * 0.25).toFixed(2),
          reasoning: REASONINGS[Math.floor(rng() * REASONINGS.length)],
          latency_ms: Math.round(1800 + rng() * 3200),
        },
      });
    }
    // Settled history back through the campaign.
    let ts = t0 - 900;
    const syms = SIM_DEFS.map((d) => d.sym);
    const regs: Regime[] = ['EARLY', 'MID', 'MID', 'LATE'];
    for (let i = 0; i < 84; i++) {
      const sym = syms[Math.floor(rng() * 9)];
      const yes = rng() > 0.45;
      const win = rng() > 0.44;
      const price = 0.25 + rng() * 0.5;
      const qty = 5 + Math.floor(rng() * 8) * 5;
      const fees = qty * 0.017 * (1 - Math.abs(price - 0.5));
      const pnl = win ? qty * (1 - price) - fees : -(qty * price) - fees;
      const src: 'claude' | 'baseline' = rng() > 0.4 ? 'claude' : 'baseline';
      ts -= 4000 + Math.floor(rng() * 10500);
      const d = new Date(ts * 1000);
      trades.push({
        id: id--,
        ts,
        asset: sym,
        market_ticker: mkTicker(sym, ts + 600 - ((ts + 600) % 900), 40 + Math.floor(rng() * 40)),
        intent: yes ? 'BUY_YES' : 'BUY_NO',
        contracts: qty,
        limit_price: +price.toFixed(3),
        avg_fill_price: +price.toFixed(3),
        fees: +fees.toFixed(2),
        result: win ? (yes ? 'yes' : 'no') : yes ? 'no' : 'yes',
        pnl_net: +pnl.toFixed(2),
        strategy: STRATEGY_BY_ASSET[sym],
        regime_at_entry: regs[Math.floor(rng() * 4)] ?? 'MID',
        smart_money_lean: rng() > 0.5 ? (rng() > 0.45 ? 'UP' : 'DOWN') : null,
        decision_source: src,
        claude:
          src === 'claude'
            ? {
                confidence: +(0.55 + rng() * 0.35).toFixed(2),
                reasoning: REASONINGS[(Math.floor(rng() * REASONINGS.length) + d.getUTCHours()) % REASONINGS.length],
                latency_ms: Math.round(1800 + rng() * 3200),
              }
            : null,
      });
    }
  }

  trades.sort((a, b) => b.ts - a.ts);

  // ---- blackouts ----
  const blackouts: Blackout[] = [
    { label: 'FOMC rate decision', start: Date.UTC(2026, 6, 29, 18, 0) / 1000, end: Date.UTC(2026, 6, 29, 19, 30) / 1000, affected_assets: 'ALL' },
    { label: 'CPI print', start: Date.UTC(2026, 6, 15, 12, 25) / 1000, end: Date.UTC(2026, 6, 15, 12, 50) / 1000, affected_assets: 'ALL' },
    { label: 'ETH Pectra upgrade window', start: Date.UTC(2026, 6, 21, 6, 0) / 1000, end: Date.UTC(2026, 6, 21, 12, 0) / 1000, affected_assets: ['ETH'] },
  ];

  return {
    scenario,
    t0,
    startedAt: day1 ? t0 - 4 * 3600 : t0 - (8 * 86400 + 10 * 3600), // campaign day 1 / day 9
    computedAt: t0 - 180,
    kill: false,
    sims,
    trades,
    nextTradeId: 1200,
    equity,
    config,
    blackouts,
  };
}

export function setScenario(s: Scenario): void {
  world = initWorld(s);
}

export function getScenario(): Scenario {
  return world.scenario;
}

// ---------------------------------------------------------------- snapshots

function buildSnapshot(sim: SimAsset): FeatureSnapshot {
  const now = nowSec();
  const ts = sim.stale ? sim.frozenTs : now;
  const remaining = Math.max(0, sim.closeTs - now);
  const half = sim.spreadC / 200;
  const yesBid = clamp(sim.imp - half, 0.005, 0.99);
  const yesAsk = clamp(sim.imp + half, 0.01, 0.995);
  const btc = world.sims[0];
  const edgeYesGross = sim.mod - sim.imp;
  return {
    schema_version: 2,
    ts,
    asset: sim.sym,
    market_ticker: sim.ticker,
    spot: sim.spot,
    spot_source: sim.stale ? 'binance_us' : 'coinbase',
    spot_age_seconds: sim.stale ? +(now - sim.frozenTs).toFixed(1) : +(0.05 + rng() * 0.4).toFixed(2),
    floor_strike: sim.strike,
    distance_dollars: sim.spot - sim.strike,
    distance_z: (sim.spot - sim.strike) / (sim.strike * 0.0015),
    seconds_remaining: remaining,
    regime: regimeOf(remaining),
    ret_30s: sim.nullRets ? null : +((rng() * 2 - 1) * 2e-4).toFixed(7),
    ret_1m: sim.nullRets ? null : +((rng() * 2 - 1) * 5e-4).toFixed(7),
    ret_5m: sim.nullRets ? null : +((rng() * 2 - 1) * 1e-3).toFixed(7),
    realized_vol_5m: sim.nullRets ? null : +(rng() * 1e-4).toFixed(8),
    btc_ret_30s: sim.sym === 'BTC' ? null : +((rng() * 2 - 1) * 2e-4).toFixed(7),
    btc_ret_1m: sim.sym === 'BTC' ? null : +((rng() * 2 - 1) * 5e-4).toFixed(7),
    btc_implied_prob: sim.sym === 'BTC' ? null : btc.imp,
    yes_bid: yesBid,
    yes_ask: yesAsk,
    implied_prob: sim.imp,
    spread_cents: +((yesAsk - yesBid) * 100).toFixed(1),
    depth_yes_within_2c: sim.depthYes,
    depth_no_within_2c: sim.depthNo,
    flow_imbalance: sim.nullFlow ? null : +(0.4 + rng() * 0.3).toFixed(3),
    book_age_seconds: sim.stale ? +(now - sim.frozenTs).toFixed(1) : sim.bookAge,
    model_prob: sim.mod,
    edge_yes_gross: edgeYesGross,
    edge_yes_net: edgeYesGross - 0.01,
    edge_no_gross: -edgeYesGross,
    edge_no_net: -edgeYesGross - 0.01,
    smart_lean: sim.smLean,
    smart_strength: sim.smLean ? sim.smStr : null,
  };
}

function buildLiveAsset(sim: SimAsset): LiveAsset {
  const cfg = world.config[sim.sym];
  return {
    asset: sim.sym,
    paused: cfg.paused,
    market: {
      ticker: sim.ticker,
      open_ts: sim.openTs,
      close_ts: sim.closeTs,
      floor_strike: sim.strike,
      status: 'active',
    },
    snapshot: buildSnapshot(sim),
    smart_money: sim.smLean
      ? { lean: sim.smLean, strength: sim.smStr, source_breakdown: { flow_patterns: 0.7, polymarket: 0.5 } }
      : null,
    position: sim.pos,
    session_pnl: {
      gross: +(sim.sessNet + sim.sessTrades * 0.34).toFixed(2),
      net: sim.sessNet,
      trades: sim.sessTrades, wins: 4,
    },
  };
}

// Advance the simulated world by one tick (~2s); returns settlement events.
function advanceWorld(): WsMessage[] {
  const now = nowSec();
  const events: WsMessage[] = [];
  for (const sim of world.sims) {
    // Window roll → settlement.
    if (now >= sim.closeTs) {
      const result: 'yes' | 'no' = sim.spot >= sim.strike ? 'yes' : 'no';
      let pnl = 0;
      if (sim.pos) {
        pnl = sim.pos.side === result ? sim.pos.contracts * (1 - sim.pos.avg_price) : -sim.pos.contracts * sim.pos.avg_price;
        pnl = +pnl.toFixed(2);
        sim.sessNet = +(sim.sessNet + pnl).toFixed(2);
        sim.pos = null;
      }
      events.push({ type: 'settlement', data: { asset: sim.sym, market_ticker: sim.ticker, result, pnl_net: pnl } });
      sim.openTs = sim.closeTs;
      sim.closeTs += 900;
      sim.strike = +(sim.spot + (rng() - 0.5) * sim.vol).toFixed(sim.dp);
      sim.ticker = mkTicker(sim.sym, sim.closeTs, sim.strike);
      sim.imp = clamp(0.5 + (rng() - 0.5) * 0.12, 0.05, 0.95);
      sim.mod = clamp(sim.imp + (rng() - 0.5) * 0.14, 0.03, 0.97);
    }
    // Price/probability drift (stale assets keep a frozen feed).
    if (!sim.stale) {
      sim.spot += (rng() - 0.5) * sim.vol * 2;
      sim.series.push(sim.spot);
      if (sim.series.length > 240) sim.series.shift();
      sim.imp = clamp(sim.imp + (rng() - 0.5) * 0.006, 0.02, 0.985);
      sim.mod = clamp(sim.mod + (rng() - 0.5) * 0.006, 0.02, 0.99);
      if (sim.pos) {
        sim.pos = { ...sim.pos, unrealized_pnl: +(sim.pos.unrealized_pnl + (rng() - 0.5) * 0.3).toFixed(2) };
      }
    }
  }
  return events;
}

function genLiveTrade(): Trade | null {
  const candidates = world.sims.filter((s) => !s.stale && !world.config[s.sym].paused);
  if (!candidates.length) return null;
  const sim = candidates[Math.floor(rng() * candidates.length)];
  const yes = rng() > 0.5;
  const price = clamp(sim.imp + (rng() - 0.5) * 0.02, 0.03, 0.97);
  const qty = 5 + Math.floor(rng() * 6) * 5;
  const t: Trade = {
    id: world.nextTradeId++,
    ts: nowSec(),
    asset: sim.sym,
    market_ticker: sim.ticker,
    intent: yes ? 'BUY_YES' : 'BUY_NO',
    contracts: qty,
    limit_price: +price.toFixed(3),
    avg_fill_price: +price.toFixed(3),
    fees: +(qty * 0.017 * (1 - Math.abs(price - 0.5))).toFixed(2),
    result: null,
    pnl_net: 0,
    strategy: STRATEGY_BY_ASSET[sim.sym],
    regime_at_entry: regimeOf(Math.max(0, sim.closeTs - nowSec())),
    smart_money_lean: sim.smLean,
    decision_source: rng() > 0.4 ? 'claude' : 'baseline',
    claude: null,
  };
  if (t.decision_source === 'claude') {
    t.claude = {
      confidence: +(0.55 + rng() * 0.35).toFixed(2),
      reasoning: REASONINGS[Math.floor(rng() * REASONINGS.length)],
      latency_ms: Math.round(1800 + rng() * 3200),
    };
  }
  sim.sessTrades += 1;
  return t;
}

// ---------------------------------------------------------------- REST mocks

export function getStatus(): Status {
  return {
    mode: 'SHADOW',
    started_at: world.startedAt,
    clock_offset_ms: -12,
    kill_switch_engaged: world.kill,
    feeds: {
      coinbase: { connected: true, ticks_per_min: 420 },
      binance_us: { connected: true, ticks_per_min: 14 },
      kalshi: { connected: true, req_per_sec: 5.8 },
    },
    db: { size_mb: 412.5, signals_rows: 1200450 },
  };
}

export function getLive(): LiveAsset[] {
  return world.sims.map(buildLiveAsset);
}

const REGIME_FACTOR: Record<string, number> = { all: 1, EARLY: 0.86, MID: 1.14, LATE: 0.94 };
const REGIME_N: Record<string, number> = { all: 1, EARLY: 0.32, MID: 0.44, LATE: 0.24 };

export function getLeaderboard(params: {
  basis?: LeaderboardBasis;
  smart_money?: SmartMoneyFilter;
  regime?: 'all' | Regime;
}): LeaderboardResponse {
  if (world.scenario === 'day1') return { computed_at: world.computedAt, rows: [] };
  const basis = params.basis ?? 'net';
  const sm = params.smart_money ?? 'with';
  const regime = params.regime ?? 'all';
  const f = REGIME_FACTOR[regime] ?? 1;
  const nf = REGIME_N[regime] ?? 1;
  const rows: LeaderboardRow[] = LB_BASE.map((r) => {
    const smOff = sm === 'without' ? r.smDelta : 0;
    const pf = +(((basis === 'gross' ? r.pf + 0.22 : r.pf) - smOff) * f).toFixed(2);
    return {
      rank: 0,
      asset: r.asset,
      strategy: r.strategy,
      profit_factor: pf,
      pl_ratio_pct: Math.round((basis === 'gross' ? r.pl + 24 : r.pl) * f - smOff * 40),
      net_pnl_per_contract: +(r.npc * f).toFixed(3),
      return_on_capital: +(r.roc * f).toFixed(2),
      hit_rate: +clamp(r.hit + (f - 1) * 0.05 - smOff * 0.05, 0.3, 0.75).toFixed(2),
      brier_model: r.bm,
      brier_market: r.bk,
      signals_per_day: r.sigd,
      fill_rate: r.fill,
      avg_spread_cents: r.sprd,
      avg_slippage_cents: r.slip,
      max_drawdown: r.dd,
      longest_losing_streak: r.streak,
      pnl_volatility: r.vol,
      n_trades: Math.max(1, Math.round(r.n * nf)),
      n_settled_windows: Math.max(1, Math.round(r.nw * nf)),
      confidence: r.conf,
      recommendation: r.rec,
    };
  });
  rows.sort((a, b) => (b.profit_factor ?? 0) - (a.profit_factor ?? 0));
  rows.forEach((r, i) => (r.rank = i + 1));
  return { computed_at: world.computedAt, rows };
}

export function getTrades(params: { asset?: Asset; strategy?: string; limit?: number; cursor?: string }): TradesResponse {
  const limit = Math.min(params.limit ?? 50, 500);
  const offset = params.cursor ? parseInt(params.cursor, 10) || 0 : 0;
  let filtered = world.trades;
  if (params.asset) filtered = filtered.filter((t) => t.asset === params.asset);
  if (params.strategy) filtered = filtered.filter((t) => t.strategy === params.strategy);
  const page = filtered.slice(offset, offset + limit);
  const next = offset + limit < filtered.length ? String(offset + limit) : '';
  return { cursor: next, trades: page };
}

export function getEquity(params: { assets?: Asset[]; basis?: LeaderboardBasis }): EquityResponse {
  const basis = params.basis ?? 'net';
  const assets = params.assets?.length ? params.assets : (Object.keys(world.equity) as Asset[]);
  return {
    series: assets
      .filter((a) => world.equity[a])
      .map((a) => ({
        asset: a,
        points: world.equity[a].map(([ts, v]) => [ts, basis === 'gross' ? +(v * 1.22).toFixed(2) : v] as [number, number]),
      })),
  };
}

export function getSmartMoney(): SmartMoneyResponse {
  const merged: SmartMoneyResponse['merged_leans'] = {};
  for (const sim of world.sims) {
    if (sim.smLean) merged[sim.sym] = { lean: sim.smLean, strength: sim.smStr };
  }
  return {
    patterns: [
      { id: 'sweep_mid_window', description: 'Aggressive taker sweep >$2k in MID regime, same direction twice within 90s', hit_rate_30d: 0.61, n_30d: 140, status: 'active', current_leans: { BTC: 'UP', ETH: 'UP' } },
      { id: 'fade_open_spike', description: 'First-minute price spike >8c that reverts; fade the spike direction', hit_rate_30d: 0.57, n_30d: 212, status: 'active', current_leans: { SOL: 'DOWN' } },
      { id: 'polymarket_divergence', description: 'Polymarket hourly implied diverges >6c from Kalshi book mid', hit_rate_30d: 0.59, n_30d: 98, status: 'active', current_leans: { HYPE: 'UP', DOGE: 'DOWN' } },
      { id: 'whale_follow_late', description: 'Follow single >$5k order in LATE regime', hit_rate_30d: 0.48, n_30d: 67, status: 'benched', current_leans: {} },
    ],
    wallets: [
      { address: '0x12ab…9f3c', win_rate: 0.64, ci_low: 0.58, ci_high: 0.7, n_resolved: 312, profit_usd: 48210, avg_entry_seconds_after_open: 210, current_positions: [{ asset: 'BTC', side: 'UP', size_usd: 1200 }] },
      { address: '0x88fe…12aa', win_rate: 0.61, ci_low: 0.55, ci_high: 0.67, n_resolved: 256, profit_usd: 31400, avg_entry_seconds_after_open: 340, current_positions: [{ asset: 'ETH', side: 'UP', size_usd: 800 }, { asset: 'SOL', side: 'DOWN', size_usd: 450 }] },
      { address: '0x3c9d…77b2', win_rate: 0.58, ci_low: 0.49, ci_high: 0.66, n_resolved: 120, profit_usd: 12800, avg_entry_seconds_after_open: 150, current_positions: [] },
    ],
    merged_leans: merged,
    wallet_consensus: {
      rankings: [],
      latest_observations: [
        { id: 1, ts: nowSec(), asset: 'SOL', market_ticker: 'KXSOL15M-MOCK', elapsed_s: 120,
          active_wallets: 12, effective_wallets: 10.4, dominant_share: 0.69,
          lean: 'UP', eligible: 1, reason: 'eligible' },
        { id: 2, ts: nowSec(), asset: 'XRP', market_ticker: 'KXXRP15M-MOCK', elapsed_s: 120,
          active_wallets: 6, effective_wallets: 5.7, dominant_share: 0.62,
          lean: 'DOWN', eligible: 0, reason: 'active wallets 6 < 8' },
      ],
      summary: [],
      counterfactual_summary: [],
      latest_decisions: [],
    },
  };
}

export interface FullConfig {
  assets: Record<Asset, AssetConfig>;
  blackouts: Blackout[];
  strategies?: string[];
}

export function getConfig(): FullConfig {
  return {
    assets: world.config,
    blackouts: world.blackouts,
    strategies: [
      'cross_asset_lead_lag',
      'latency_momentum',
      'mean_reversion_extremes',
      'naive_edge_taker',
      'thin_book_maker',
    ],
  };
}

export function putAssetConfig(sym: Asset, cfg: Partial<AssetConfig>): AssetConfig {
  world.config[sym] = { ...world.config[sym], ...cfg };
  return world.config[sym];
}

export function getBlackouts(): Blackout[] {
  return world.blackouts;
}

export function putBlackouts(list: Blackout[]): Blackout[] {
  world.blackouts = list;
  return world.blackouts;
}

export function postKill(): KillResponse {
  world.kill = !world.kill;
  const cancelled = world.kill ? world.sims.filter((s) => s.pos !== null).length : 0;
  return { engaged: world.kill, cancelled_orders: cancelled };
}

export function postPause(sym: Asset): { paused: boolean } {
  world.config[sym] = { ...world.config[sym], paused: true };
  return { paused: true };
}

export function postResume(sym: Asset): { paused: boolean } {
  world.config[sym] = { ...world.config[sym], paused: false };
  return { paused: false };
}

/** Mock-only nicety: seed the UI's rolling spot history so the sparklines are
 *  fully drawn at first paint (the real backend has no history endpoint —
 *  the UI accumulates spots from WS snapshots). */
export function getSpotHistory(): Partial<Record<Asset, number[]>> {
  const out: Partial<Record<Asset, number[]>> = {};
  for (const sim of world.sims) out[sim.sym] = [...sim.series];
  return out;
}

// ---------------------------------------------------------------- mock WS

export interface MockSocketHandlers {
  onOpen(): void;
  onMessage(m: WsMessage): void;
  onClose(): void;
}

export interface MockSocketHandle {
  close(): void; // silent close (client unmount)
  forceClose(): void; // simulated drop → fires onClose
}

let activeSocket: MockSocketHandle | null = null;

export function openMockSocket(h: MockSocketHandlers): MockSocketHandle {
  let closed = false;
  let tickN = 0;

  const pushAll = () => {
    for (const sim of world.sims) {
      h.onMessage({ type: 'snapshot', asset: sim.sym, data: buildLiveAsset(sim) });
    }
  };

  const openT = setTimeout(() => {
    if (closed) return;
    h.onOpen();
    pushAll();
  }, 120);

  const iv = setInterval(() => {
    if (closed) return;
    tickN++;
    const events = advanceWorld();
    pushAll();
    for (const ev of events) h.onMessage(ev);
    if (tickN % 5 === 0) h.onMessage({ type: 'status', data: getStatus() });
    if (world.scenario === 'day9' && !world.kill && rng() < 0.05) {
      const t = genLiveTrade();
      if (t) {
        world.trades.unshift(t);
        h.onMessage({ type: 'trade', data: t });
      }
    }
  }, 2000);

  const teardown = () => {
    closed = true;
    clearTimeout(openT);
    clearInterval(iv);
    if (activeSocket === handle) activeSocket = null;
  };

  const handle: MockSocketHandle = {
    close() {
      teardown();
    },
    forceClose() {
      if (closed) return;
      teardown();
      h.onClose();
    },
  };
  activeSocket = handle;
  return handle;
}

/** Dev-only: simulate a WS drop; the client reconnect loop takes over. */
export function simulateDrop(): void {
  activeSocket?.forceClose();
}
