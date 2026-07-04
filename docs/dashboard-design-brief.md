# KalshiBot Dashboard — Design & Build Brief

This document is self-contained: everything needed to design and build the
KalshiBot web dashboard without access to the bot codebase. Written 2026-07-04.

## 1. What this app is

KalshiBot trades Kalshi's 15-minute crypto up/down binary markets across nine
assets (BTC, ETH, SOL, ZEC, HYPE, XRP, DOGE, BNB, NEAR) in parallel, each with
its own strategy. A new contract window opens every quarter hour, 24/7; each
contract settles to $1.00 or $0.00. Contract price = implied probability, so
prices are conventionally shown in cents (e.g. "27¢").

The bot's research goal is comparative: measure all nine markets independently
and rank them by predictability and profit/loss ratio. The dashboard is the
window into that comparison, plus operational control.

The bot runs in one of three modes:
- **SHADOW** (default): real production market data, simulated fills, no real
  orders. All evaluation happens here.
- **DEMO**: demo exchange, plumbing tests only.
- **LIVE**: real money. Enabled manually only after a 2-week shadow campaign.

## 2. Non-negotiable requirements

1. **Mode banner.** Always visible on every screen, impossible to miss:
   SHADOW = indigo with "simulated fills — no real orders", DEMO = amber,
   LIVE = red with a pulsing border. Mode is read-only in the UI (changed via
   server config only).
2. **Kill switch.** Reachable from every screen (persistent header button).
   Requires an explicit confirmation step; in LIVE mode, type-to-confirm
   ("KILL"). Calls `POST /api/control/kill`, which cancels all resting orders
   and halts all trading loops. Also per-asset pause toggles.
3. **Sample-size honesty.** Every aggregate metric shows its n (trades or
   settled windows). n < 30 gets a red "insufficient" badge, 30–99 amber
   "low sample", ≥ 100 no badge. A lucky small sample must never visually
   masquerade as a winner — this is a core product requirement, not styling.
4. **Data freshness.** WS connection state indicator; per-asset staleness
   (age of last snapshot). Anything older than 5s gets a visible stale
   treatment (desaturate + timestamp). Disconnect shows a reconnecting banner
   with countdown; never silently show stale data as live.
5. **Fees visible.** Anywhere PnL or edge appears there is a gross/net-of-fees
   toggle or paired display. Kalshi fees peak exactly where these contracts
   trade (near 50¢), so gross-only numbers are misleading.
6. **Auth-ready.** All API calls go through one client module that handles a
   401 by showing a single-user login screen. Assume bearer/session auth; the
   dashboard will sit behind Cloudflare Access + app auth in production.

## 3. Tech constraints

- React 18 + TypeScript + Vite, in a `dashboard/` directory of the repo.
- Tailwind CSS + shadcn/ui components; Recharts for charts (or hand-rolled
  SVG for sparklines). lucide-react icons.
- Plain native WebSocket (no socket.io) — must survive a Cloudflare Tunnel.
- Dark theme is the default and primary design target (trading terminal);
  light theme supported via class toggle. Don't rely on color alone for
  up/down semantics (pair with arrows/signs — accessibility and printability).
- Tabular numerals everywhere numbers align (`font-variant-numeric:
  tabular-nums`); monospace for tickers and prices is welcome.
- Desktop-first (~1440px primary), usable at tablet width. Mobile: the live
  grid, mode banner, and kill switch must work on a phone; other views can
  degrade.
- **Build against the mock layer.** Implement `src/api/types.ts` (contract
  below), `src/api/client.ts` (fetch/WS wrapper), and `src/api/mock.ts`
  (realistic generated fixtures, including live-updating fake WS pushes).
  A `VITE_API_MOCK=1` env flag switches mock vs real. The FastAPI backend
  will implement this exact contract later — the contract is the deliverable
  boundary between the two Claudes.

## 4. Data contract (v1)

All timestamps are epoch seconds (float). All prices/probabilities are in
dollars 0–1 (display as cents). `regime` ∈ EARLY | MID | LATE | SETTLEMENT.

### REST

```
GET /api/status
{
  "mode": "SHADOW",
  "started_at": 1783190000.0,
  "clock_offset_ms": -12.0,
  "kill_switch_engaged": false,
  "feeds": {
    "coinbase": {"connected": true, "ticks_per_min": 420},
    "binance_us": {"connected": true, "ticks_per_min": 14},
    "kalshi": {"connected": true, "req_per_sec": 5.8}
  },
  "db": {"size_mb": 412.5, "signals_rows": 1200450}
}

GET /api/live   → array of 9:
{
  "asset": "BTC",
  "paused": false,
  "market": {
    "ticker": "KXBTC15M-26JUL041645-45",
    "open_ts": 1783196700.0, "close_ts": 1783197600.0,
    "floor_strike": 63124.02, "status": "active"
  },
  "snapshot": { ...FeatureSnapshot, see §4.1... },
  "smart_money": {"lean": "UP", "strength": 0.62,
                  "source_breakdown": {"flow_patterns": 0.7, "polymarket": 0.5}},
  "position": {"side": "yes", "contracts": 20, "avg_price": 0.41,
               "unrealized_pnl": 1.80} | null,
  "session_pnl": {"gross": 12.40, "net": 9.85, "trades": 7}
}

GET /api/leaderboard?basis=net|gross&smart_money=with|without|both
                    &regime=all|EARLY|MID|LATE&version=latest
→ {"computed_at": ..., "rows": [
  {
    "rank": 1, "asset": "ETH", "strategy": "latency_momentum_v1",
    "profit_factor": 1.84, "pl_ratio_pct": 142.0,
    "net_pnl_per_contract": 0.031, "return_on_capital": 0.19,
    "hit_rate": 0.58, "brier_model": 0.19, "brier_market": 0.24,
    "signals_per_day": 41.0, "fill_rate": 0.83, "avg_spread_cents": 1.2,
    "avg_slippage_cents": 0.4, "max_drawdown": -42.0,
    "longest_losing_streak": 6, "pnl_volatility": 8.2,
    "n_trades": 214, "n_settled_windows": 288,
    "confidence": "medium",            // ranking confidence from sample size
    "recommendation": "keep"           // keep | retune | bench
  }, ...
]}

GET /api/trades?asset=&strategy=&limit=50&cursor=
→ {"cursor": "...", "trades": [{
    "id": 991, "ts": ..., "asset": "BTC", "market_ticker": "...",
    "intent": "BUY_YES", "contracts": 20, "limit_price": 0.41,
    "avg_fill_price": 0.41, "fees": 0.34, "result": "yes" | "no" | null,
    "pnl_net": 11.46, "strategy": "latency_momentum_v1",
    "regime_at_entry": "MID", "smart_money_lean": "UP",
    "decision_source": "claude" | "baseline" | null,
    "claude": {"confidence": 0.72, "reasoning": "...", "latency_ms": 3400} | null
  }]}

GET /api/equity?assets=BTC,ETH&basis=net
→ {"series": [{"asset": "BTC", "points": [[ts, cumulative_pnl], ...]}]}

GET /api/smartmoney
→ {"patterns": [{"id": "sweep_mid_window", "description": "...",
     "hit_rate_30d": 0.61, "n_30d": 140, "status": "active" | "benched",
     "current_leans": {"BTC": "UP", "ETH": null}}],
   "wallets": [{"address": "0x12ab…", "win_rate": 0.64,
     "ci_low": 0.58, "ci_high": 0.70, "n_resolved": 312,
     "profit_usd": 48210.0, "avg_entry_seconds_after_open": 210,
     "current_positions": [{"asset": "BTC", "side": "UP", "size_usd": 1200}]}],
   "merged_leans": {"BTC": {"lean": "UP", "strength": 0.62}, ...}}

GET  /api/config                       → full config (assets, weights, blackouts)
PUT  /api/config/assets/{symbol}       → update one asset's strategy/params
GET  /api/blackouts / PUT /api/blackouts → event blackout calendar entries
POST /api/control/kill                 → {"engaged": true, "cancelled_orders": 3}
POST /api/control/pause/{symbol} | /api/control/resume/{symbol}
```

### WebSocket `WS /ws/live`

Server pushes JSON messages:
```
{"type": "snapshot", "asset": "BTC", "data": {…same shape as /api/live item…}}   // ~every 2s per asset
{"type": "trade",     "data": {…same shape as a /api/trades item…}}              // on fill
{"type": "settlement","data": {"asset": "BTC", "market_ticker": "...",
                               "result": "yes", "pnl_net": 11.46}}               // each window close
{"type": "status",    "data": {…same shape as /api/status…}}                     // every 10s + on change
```

### 4.1 FeatureSnapshot (real sample, recorded live 2026-07-04)

This exact JSON shape arrives in `snapshot`. Note the late-window state: 205s
remaining, book converged to 99.2¢ — this asymmetric extreme state is common
and the UI must render it gracefully (bars pinned near 100%, tiny spreads).

```json
{
  "schema_version": 1,
  "ts": 1783197694.93854,
  "asset": "BTC",
  "market_ticker": "KXBTC15M-26JUL041645-45",
  "spot": 63209.98,
  "spot_source": "coinbase",
  "spot_age_seconds": 0.12,
  "floor_strike": 63124.02,
  "distance_dollars": 85.96,
  "distance_z": 1.69,
  "seconds_remaining": 205.06,
  "regime": "LATE",
  "ret_30s": -0.0000593,
  "ret_1m": 0.000255,
  "ret_5m": 0.000256,
  "realized_vol_5m": 0.0000562,
  "btc_ret_30s": null,
  "btc_ret_1m": null,
  "btc_implied_prob": null,
  "yes_bid": 0.992,
  "yes_ask": 0.993,
  "implied_prob": 0.9925,
  "spread_cents": 0.1,
  "depth_yes_within_2c": 79794.16,
  "depth_no_within_2c": 20741.67,
  "flow_imbalance": 0.587,
  "book_age_seconds": 1.61,
  "model_prob": 0.9545,
  "edge_yes_gross": -0.0385,
  "edge_yes_net": -0.0485,
  "edge_no_gross": 0.0375,
  "edge_no_net": 0.0275
}
```
(`btc_*` fields are null for BTC itself, populated for the eight alts.)

## 5. Views

### 5.1 Live grid (default route `/`)

Grid of nine asset cards (3×3 at desktop). Each card:
- Header: asset symbol, market ticker (mono, truncated), per-asset pause toggle.
- **Countdown** to window close (mm:ss) with a **regime chip**:
  EARLY (slate/blue) → MID (green) → LATE (amber) → SETTLEMENT (red, lock
  icon — trading blackout, final 90s).
- Spot vs strike: current spot, floor_strike, signed distance with arrow;
  a subtle 15-minute spot sparkline with a horizontal strike line.
- **Implied vs model probability**: the core comparison. Two aligned bars or
  a bullet chart: market implied (from book mid) vs model probability, with
  the net edge called out numerically (e.g. "edge NO +2.8¢ net"). Highlight
  when |edge_net| clears the asset's threshold.
- Book row: yes bid/ask in cents, spread, depth badge, book age.
- Smart money chip: lean arrow + strength (e.g. "▲ 0.62"), neutral = gray dot.
- Position row (when open): side, contracts, avg price, unrealized PnL.
- Session PnL (net) in the footer, colored & signed.
Card sort options: fixed order | by |edge| | by session PnL. Stale cards
desaturate. Clicking a card opens a detail drawer (full snapshot fields,
larger sparkline, recent trades in this window).

### 5.2 Leaderboard (`/leaderboard`) — the money screen

The reason this project exists: nine markets ranked head-to-head.
- Dense sortable table, one row per asset (columns from `/api/leaderboard`).
  Default sort: profit factor desc.
- Controls: basis toggle (net default / gross), smart-money (with/without —
  "both" shows paired sub-columns with deltas), regime filter, version picker.
- Sample-size badges per row (§2.3). A "confidence" pill on the rank column
  (high/medium/low from the API).
- Brier scores shown as model vs market side by side; when brier_model <
  brier_market (bot better calibrated than the market), mark that cell —
  that's the exploitability signal.
- Recommendation column renders keep/retune/bench as pills.
- Expandable row detail: per-regime and time-of-day breakdown mini-tables,
  equity sparkline, link to History filtered to that asset.

### 5.3 Smart money (`/smartmoney`)

Two panels + a summary strip:
- Current merged leans strip: nine chips (asset + lean arrow + strength).
- Layer A — flow patterns table: pattern name/description, rolling 30d hit
  rate with n, status (active/benched), current leans. Benched rows muted.
- Layer B — Polymarket wallets table: truncated address (copyable), win rate
  with confidence interval rendered as a range bar, n resolved, profit,
  typical entry timing, current open positions as chips.

### 5.4 History (`/history`)

- Equity curves: overlaid per-asset cumulative net PnL lines (toggle assets
  on/off; legend chips reuse each asset's stable color from the palette).
- Trade log table (server-paginated): time, asset, intent, size, fill price,
  fees, result, net PnL, strategy, regime, decision source (claude/baseline
  badge). Expandable row: Claude confidence + full reasoning text + latency,
  smart-money state at entry.
- Filters: asset, strategy, regime, decision source, date range.

### 5.5 Config (`/config`)

- Per-asset cards: strategy dropdown, parameter fields (typed from config
  schema), edge threshold, smart-money weight slider (0–1), max position,
  enabled/paused. Save per asset (PUT), with optimistic UI + rollback on error.
- Blackout calendar: list editor of {label, start, end, affected assets}.
- Global: kill switch (same component as header), mode display (read-only).
- Every mutation shows a confirmation toast; failures show the server error.

## 6. Visual language

- Terminal-density dark UI: near-black background (not pure #000), 1–2 accent
  colors, generous use of borders/dividers over shadows. Think TradingView /
  Linear density, not marketing-site whitespace.
- Semantic colors: green = up/YES/profit, red = down/NO/loss, and each asset
  gets a stable categorical color used consistently in charts and chips.
- Numbers are the interface: right-align numeric columns, tabular figures,
  consistent decimals (prices 1 decimal in cents "27.5¢"; PnL 2 decimals with
  explicit sign; probabilities as cents or % but pick one per context).
- Motion: subtle. Value changes may flash (green/red 300ms). Countdown chips
  change color at regime boundaries. No spinners longer than 500ms — use
  skeletons.
- Empty states matter: pre-data leaderboard ("campaign day 1 — metrics appear
  after the first settled windows"), no-position cards, benched patterns.

## 7. Out of scope for this build

- Real backend (FastAPI implements the contract later; mock layer only).
- Auth implementation beyond the 401 → login screen flow (stub accepted).
- n8n screens, deployment config, replay-engine controls (later phase).

## 8. Definition of done

- `cd dashboard && npm install && npm run dev` runs with `VITE_API_MOCK=1`
  showing all five views with live-updating mock data (WS simulation included),
  both themes, at 1440px and 390px widths.
- Mock data includes edge cases: an asset in SETTLEMENT blackout, a stale
  asset (>5s), an empty-history day-1 state, a low-sample leaderboard row,
  a benched flow pattern, and kill-switch-engaged state.
- `npm run build` passes with no TypeScript errors.
- Types in `src/api/types.ts` match §4 exactly — they are the contract.
```
