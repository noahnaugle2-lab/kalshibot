# Handoff: KalshiBot Dashboard

## Overview
Web dashboard for KalshiBot — a bot that trades Kalshi's 15-minute crypto up/down binary markets across nine assets (BTC, ETH, SOL, ZEC, HYPE, XRP, DOGE, BNB, NEAR) in parallel, ranking them by predictability and P/L. The dashboard is the window into that comparison plus operational control (kill switch, pause toggles, config). Five views: Live grid, Leaderboard, Smart Money, History, Config — plus a persistent header, kill-switch flow, WS-disconnect state, and a 401 login screen.

The full product brief (data contract, non-negotiables, view specs) lives in the bot repo at `docs/dashboard-design-brief.md`. This handoff is the *visual + behavioral* spec; the brief is the *product* spec. Where they conflict, the brief wins.

## About the Design Files
`KalshiBot Dashboard.dc.html` is a **design reference created in HTML** — an interactive prototype showing intended look and behavior, not production code. The task is to **recreate this design in the target stack** defined by the brief: React 18 + TypeScript + Vite in a `dashboard/` directory, Tailwind CSS + shadcn/ui, Recharts (or hand-rolled SVG for sparklines), lucide-react icons, plain native WebSocket. Build against a mock layer (`VITE_API_MOCK=1`) implementing `types.ts` from this bundle.

## Fidelity
**High-fidelity.** Colors, typography, spacing, and interactions are final design intent — recreate pixel-perfectly using Tailwind utilities/shadcn primitives. The only prototype-grade shortcuts (replace with real implementations) are noted in "Prototype shortcuts" at the bottom.

## Design Tokens

### Color — dark theme (primary)
| Token | Value | Use |
|---|---|---|
| bg | `#090A0E` | page background |
| surface | `#0D0F15` | cards, tables, panels |
| surface-2 | `#11131C` | inputs, inner chips, bar tracks (`#151827` for bars) |
| border | `#1D2130` | card/table borders |
| border-subtle | `#171A26` | row dividers |
| border-strong | `#232838` / `#2A2E42` | inputs, inactive buttons |
| text | `#E4E9F2` | primary |
| text-dim | `#8B94A3` | secondary, labels |
| text-faint | `#5C6675` | micro-labels, letterspaced headers |
| text-ghost | `#464F60` | tickers, axis labels |
| accent (indigo) | `#5B67F1` | market bars, links, active states; banner bg `#14172B`, banner text `#A9B2FF` |
| green (up/YES/profit) | `#2FD180` | pair with ▲/+ signs, never color alone |
| red (down/NO/loss) | `#F0525F` | pair with ▼/−; kill: bg `#33141A`, border `#6E2530` |
| amber (warning) | `#E0A93E` | stale, low-sample, DEMO, disconnect; bg `#2A2210` |

Regime chips: EARLY `#1A2334`/`#7FA3D8` · MID `#12291C`/`#4ACB82` · LATE `#2E2410`/`#E0A93E` · SETTLEMENT `#33141A`/`#F0616F` (+" · LOCKED" text).

Stable per-asset categorical colors (chips, sparklines, equity lines, legends — never repurpose):
BTC `#F7931A` · ETH `#7C8CF8` · SOL `#22D3A5` · ZEC `#B45AF2` · HYPE `#4ED6C0` · XRP `#5EA8F5` · DOGE `#D9B44A` · BNB `#EFB90B` · NEAR `#F0619E`

### Typography
- Single family: **JetBrains Mono** (400–800), everything mono.
- `font-variant-numeric: tabular-nums` on every numeric cell/figure.
- Scale: 8px micro-labels (uppercase, letter-spacing 0.1–0.12em, text-faint) · 9px table body/buttons · 10px body/copy · 11–13px emphasized values & view titles (800) · 15–16px asset symbols (800) · 18px summary stats (800) · 22–26px countdowns (800).
- Number formats: prices 1 decimal in cents ("99.2¢"); PnL 2 decimals with explicit sign ("+$9.85", "−$3.40", use U+2212 minus); probabilities as cents; right-align numeric columns.

### Geometry & effects
- Radius: 6px cards/tables, 4px buttons/inputs/chips, 3px pills, 2px asset color squares (7–9px).
- Borders over shadows everywhere. Only shadow: edge-signal ring `box-shadow: 0 0 0 1px rgba(47,209,128,0.25)` + border `rgba(47,209,128,0.4)`.
- Spacing: card padding 11–14px; grid gaps 14px; view gutter 18–20px; content max-width 1440px centered.
- Motion: value flash 250–300ms color transition; disconnect banner 2s opacity pulse; no spinners >500ms — use skeletons.

## Persistent Header (every view)
52px bar, sticky, `#0B0D13`, bottom border. Left→right:
1. **KALSHIBOT** wordmark (14px/800) + mode chip (SHADOW: indigo-bordered).
2. Nav tabs: LIVE · LEADERBOARD · SMART MONEY · HISTORY · CONFIG (10px/700, letterspaced; active = text + `#171A2B` bg pill).
3. **PnL chip**: `24H {±$x}` | divider | `SINCE START {±$y}` | "NET" ghost label — green/red per sign, gray `$0.00` on day 1.
4. **WS indicator**: dot (green `#2FD180` connected / amber disconnected) + label.
5. Theme toggle (LIGHT/DARK).
6. Scenario toggle DAY 1 / DAY 9 (prototype-only control — omit or keep behind a dev flag).
7. **KILL** button: red text/border, fills `#33141A` on hover. When engaged, becomes DISENGAGE (solid red border).

Below the bar, **mode banner** (non-negotiable, always visible): SHADOW = indigo strip "SHADOW MODE — SIMULATED FILLS · NO REAL ORDERS"; DEMO = amber; LIVE = red **with pulsing border**. Mode is read-only in the UI.

Conditional strips below it: kill engaged (red, "⛔ KILL SWITCH ENGAGED — N RESTING ORDERS CANCELLED · ALL TRADING LOOPS HALTED") and WS disconnect (amber, pulsing, "⚠ WEBSOCKET DISCONNECTED — RECONNECTING IN {n}S · LAST DATA {m}S OLD · NOTHING BELOW IS LIVE").

## Screens / Views
Every view opens with: title (13px/800/letterspaced) then a 10px/`#8B94A3` two-line summary (line-height 1.6, max-width 860px). Exact copy is in the prototype — reuse it.

### 1. Live grid (`/`, default)
3×3 grid of asset cards (gap 14). Sort control above: FIXED / |EDGE| / SESSION PNL segmented buttons. Card anatomy, top→bottom (dividers `#171A26` between blocks):
- **Header row**: asset color square, symbol (15px/800), regime chip, conditional chips (PAUSED gray, STALE amber with live age, HALTED red when kill engaged), right-aligned countdown mm:ss (22px/800; red in SETTLEMENT).
- Ticker line (8px ghost, truncated).
- **Sparkline** (48px tall, full-bleed): asset-colored polyline, dashed strike line `#3A4354`; overlaid text: spot (flashes green/red 250ms on tick) + signed distance with ▲/▼ + "vs {strike}".
- **Probability block** (core comparison): two horizontal bars, MARKET (indigo fill) vs MODEL (gray `#454B66` fill), 11px tracks `#151827`, values right (11px/700, cents 1dp). Below: edge callout "edge NO +2.8¢ net — below 3¢ threshold" (gray) or green when it clears the asset's threshold — card also gets the green ring.
- **Book row**: YES bid/ask · SPR · DEPTH · BOOK age (9px, labels dim).
- **Position row** (only when open): "POS YES 20 @ 41.0¢" (side colored) + "uPnL +$3.40".
- **Footer**: smart-money lean "▲ 0.62" (green/red/neutral gray ●) + session PnL "+$9.85 net · 7t".
- Stale cards: opacity 0.55 + amber STALE chip with age (anything >5s). Paused: opacity 0.75.
- Click card → **detail drawer**: right-side 420px panel over scrim; header (symbol, regime chip, ✕), snapshot-age line, 80px sparkline, full FeatureSnapshot as a 2-col key/value grid (nulls rendered "null" in ghost), "TRADES · THIS WINDOW" list or dashed empty state.

### 2. Leaderboard (`/leaderboard`)
Controls row: BASIS NET/GROSS · SMART $ WITH/WITHOUT/BOTH (BOTH adds a small ±delta under PF) · REGIME ALL/EARLY/MID/LATE.
Dense table (min-width 1240px, h-scroll): columns #/CONF (conf pill high=green/med=blue/low=amber), ASSET (color square+sym), STRATEGY, PF (13px/800, sortable), P/L%, ¢/CT, ROC, HIT, BRIER M/MKT (model value green + underline when model < market), SIG/D, FILL, SPRD, MAXDD (red), N (+ badge: n<30 red INSUFF, 30–99 amber LOW — non-negotiable), REC pill (KEEP green / RETUNE amber / BENCH gray). Sortable headers (PF, P/L%, HIT, N) with ↓↑ arrows.
Row click → expansion (bg `#0A0C12`): BY REGIME mini-table, TIME OF DAY mini-table, equity sparkline (asset color, dashed zero line), streak/vol/slip stats, "→ HISTORY · {sym}" link (navigates with asset filter applied).
Footer legend: brier explanation + badge key + computed-at.
Bottom analytics row (grid `300px 1fr 1fr`): **Campaign summary** (total net PnL, trades, settled windows, avg fill, best/worst market, campaign day), **Profit factor** bar chart (bars indigo, <1.0 red `#6E2530`, center line = break-even 1.00, scale 0–2.0), **Calibration dumbbells** (indigo dot = model, gray dot = market, connector green when model better / red otherwise, scale 0.15–0.30).
Day-1 empty state: dashed border box, "CAMPAIGN DAY 1 / metrics appear after the first settled windows".

### 3. Smart money (`/smartmoney`)
- **Merged leans strip**: nine chips (color square + sym + ▲/▼ strength or gray ●).
- **Layer A — flow patterns table**: pattern id, description, HIT 30D (green ≥55%, red <50%), N (+LOW badge <100), status pill ACTIVE/BENCHED, current leans. Benched rows opacity 0.45.
- **Layer B — Polymarket wallets table**: truncated address (indigo, click-to-copy with toast), win rate + **CI range bar** (track `#151827`, CI band `#2E3550`, indigo tick at point estimate; scale 40–80%), n resolved, profit (green), typical entry ("210s after open"), open positions as chips ("BTC ▲ UP $1.2k") or "— flat".

### 4. History (`/history`)
Controls: RANGE 24H/7D/CAMPAIGN · BASIS NET/GROSS.
- **Equity chart**: legend chips per asset (color square + sym + range PnL, click toggles line, hidden = opacity 0.3); 260px SVG, overlaid per-asset cumulative-PnL polylines, dashed zero line, x-axis labels per range. Range change rebases series to 0 at range start.
- **Trade log** (server-paginated, "LOAD MORE →"): filters ASSET + SOURCE (selects); columns TIME ("JUL 03 14:22"), ASSET, INTENT (BUY_YES green / BUY_NO red), QTY, FILL, FEES, RESULT (YES/NO colored), NET PNL, STRATEGY, REGIME (regime-colored), SOURCE pill (CLAUDE indigo / BASELINE gray). Claude rows expand → reasoning paragraph + confidence + latency, smart-$ state at entry, market ticker.
Day-1 empty state mirrors leaderboard's.

### 5. Config (`/config`)
3×3 grid of per-asset cards: header (color square, sym, RUNNING green / PAUSED amber toggle → POST pause/resume + toast), STRATEGY select, EDGE THRESHOLD ¢ + MAX POSITION number inputs, SMART $ WEIGHT slider 0–1 step 0.05 (accent indigo, live value shown), "SAVE {SYM}" button → PUT + toast. Optimistic UI + rollback on server error (real build).
Below: **EVENT BLACKOUTS** list (label, UTC window, affected asset chips, EDIT) + "+ ADD ENTRY".
Every mutation → confirmation toast (bottom-center, indigo border, e.g. "PUT /api/config/assets/BTC → saved"); failures show the server error.

### Kill switch flow (non-negotiable)
KILL → modal (red-bordered, 420px): explains cancels all resting orders + halts all loops, notes current mode; CANCEL / "ENGAGE — CANCEL N ORDERS" (solid red). **In LIVE mode the confirm requires typing "KILL"** (not in prototype — must be built). Engaged: red header strip, DISENGAGE button, HALTED chip on every live card.

### Login (401 flow)
Any API 401 → full-screen route: centered 360px column — wordmark + mode chip, card with amber "SESSION EXPIRED — API RETURNED 401", DASHBOARD TOKEN password input (indigo focus border), solid-indigo SIGN IN. Footnote: single-user, Cloudflare Access + app auth in production. One API client module owns this redirect.

## Interactions & Behavior
- **WS updates**: snapshots ~2s/asset; countdowns tick locally every 1s (mm:ss); regime chips change color at boundaries (EARLY>600s, MID 600–300, LATE 300–90, SETTLEMENT <90 = trading blackout).
- **Value flash**: spot text flashes green (up) / red (down) for ~300ms per update, then returns to text color (`transition: color 0.25s`). Skip for stale assets.
- **Freshness**: per-asset staleness >5s → desaturate + STALE chip with live-incrementing age. Full WS drop → amber pulsing banner with reconnect countdown, ALL cards get stale treatment, spot updates stop (countdowns keep ticking). Never show stale data as live.
- **Fees**: every PnL/edge surface has gross/net toggle or paired display; net is default everywhere.
- Sort/filter states are client-side; leaderboard regime filter and smart-money with/without re-query the API in the real build (`?regime=`, `&smart_money=`).
- Hover states: nav tabs text-brighten; table rows bg `#11131C`; buttons brighten border to indigo; KILL fills red-tint.

## State Management
- Global: mode, kill_engaged, ws connection state, auth state, theme, live snapshots (9), status feed.
- Per-view: live sort; leaderboard basis/smFilter/regime/sort/expanded row; history range/basis/filters/hidden equity lines/expanded trade; config drafts per asset (dirty-tracked for optimistic save/rollback).
- Data: REST fetch on mount per §4 endpoints + WS `/ws/live` pushes merged into the snapshot store. Mock layer must simulate WS pushes on timers and include the edge cases: a SETTLEMENT-blackout asset, a stale asset (>5s), day-1 empty state, low-sample leaderboard rows, a benched pattern, kill-engaged state.

## Assets
No image assets. Icons in the prototype are unicode (▲▼●⚠⛔✕☼☾⧉) — replace with lucide-react equivalents (arrow-up/down, circle, alert-triangle, octagon-x, x, sun, moon, copy, lock for SETTLEMENT chips, pause). JetBrains Mono via Google Fonts (weights 400–800).

## Prototype shortcuts (do NOT copy)
- **Light theme** in the prototype is a CSS invert/hue-rotate filter — real build uses a proper Tailwind `dark:` class toggle with a real light palette (dark remains default/primary).
- Mock data is generated inline in the prototype; real build puts fixtures in `src/api/mock.ts` behind `VITE_API_MOCK=1`.
- Scenario/kill/WS/login toggles are prototype controls; real equivalents come from `/api/status`, real WS state, and real 401s.
- Type-to-confirm "KILL" in LIVE mode is specified but not prototyped.
- Tablet width should remain usable; mobile was explicitly descoped for the prototype but the brief requires live grid + banner + kill switch working on a phone — implement per brief.

## Files
- `KalshiBot Dashboard.dc.html` — the interactive hi-fi prototype (open in a browser; all five views, live ticking data, all edge-case states).
- `screenshots/` — reference captures: the five views, the WS-disconnected live grid, and the kill-switch modal.
- `types.ts` — the API contract (place at `dashboard/src/api/types.ts`; matches brief §4 exactly).
- Bot repo: `docs/dashboard-design-brief.md` (product spec), `config/assets.yaml` (per-asset config schema the Config view edits).
