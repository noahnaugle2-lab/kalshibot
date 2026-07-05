# KalshiBot

Autonomous trading bot for Kalshi 15-minute crypto up/down markets. Runs nine
markets (BTC, ETH, SOL, ZEC, HYPE, XRP, DOGE, BNB, NEAR) in parallel with
per-asset strategies, measures each independently, and ranks them by
predictability and profit/loss ratio.

**Current phase: 2 of 12** — observation mode: price feeds, feature engine,
and tape recording (no trading). Completed: Kalshi client + auth + discovery
(1). Next: fill simulator + replay engine (3), historical analysis (4),
smart-money signal (5), shadow trading loop (6), evaluation framework (7),
Claude decision engine (8), dashboard (9), n8n + dead man's switch (10),
deployment (11), 2-week shadow campaign (12).

## Modes (SHADOW / DEMO / LIVE)

| Mode | Market data | Orders | Purpose |
|------|-------------|--------|---------|
| `SHADOW` (default) | Production, read-only | Local fill simulator | **All strategy evaluation happens here** |
| `DEMO` | Demo exchange | Real orders, fake money | Order plumbing tests only — never strategy evaluation |
| `LIVE` | Production | Real money | Off until manually enabled after a 2-week shadow campaign |

## Setup

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env   # then fill in credentials
```

### Kalshi API keys

Market data (series, markets, books, trades) is public — discovery and shadow
data collection work with **no credentials**. Credentials are needed for
portfolio/order endpoints:

1. Production: kalshi.com → account settings → API keys. Demo (separate
   account + keys): demo.kalshi.co.
2. Save the downloaded RSA private key to `./secrets/` (gitignored).
3. Fill `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` (and the `_DEMO_` pair for
   demo) in `.env`.

Auth is RSA-PSS: each request sends the key ID, a millisecond timestamp, and a
base64 RSA-PSS-SHA256 signature over `timestamp + METHOD + path` (query string
excluded). Implemented in `src/kalshibot/kalshi/auth.py`.

## Usage

```bash
# Verify auth: local signature proof without keys; live balance call with keys
python scripts/check_auth.py --env demo

# Discover 15-minute series for all nine assets (production, public, no keys)
python scripts/discover_series.py

# Order plumbing check against the demo exchange (needs funded demo account)
python scripts/demo_order_plumbing.py

# Observation mode: record spot ticks, Kalshi books/trades/settlements, and
# feature snapshots for all assets to data/kalshibot.db — no trading
python scripts/run_observer.py                                # foreground
nohup python scripts/run_observer.py >> logs/observer.log 2>&1 &   # detached

# Tests
pytest
```

### Spot feed venues

Coinbase Exchange WS is the primary reference feed: it lists all nine assets
(including HYPE-USD and BNB-USD) and is a CF Benchmarks constituent exchange —
the same index family Kalshi uses for settlement. Binance.US is the automatic
per-asset backup (binance.com geo-blocks US IPs). Failover picks the freshest
venue at read time and never mixes ticks from two venues in one return series.

Discovery never hardcodes series tickers: it lists Crypto-category series,
filters `frequency == "fifteen_min"`, maps assets by ticker pattern with a
title fallback, and verifies a live market's window is exactly 15 minutes.
Missing assets are logged and re-checked daily. Output is cached at
`data/discovery/series_map.json`.

## Layout

```
config/assets.yaml        per-asset config (strategy, thresholds, allocation)
src/kalshibot/kalshi/     API client: auth, models, client, discovery, rate limits
src/kalshibot/<pkg>/      feeds, features, strategies, decision, orders,
                          evaluation, smartmoney, persistence, dashboard (later phases)
scripts/                  operational entry points
tests/                    unit tests
data/                     runtime data (gitignored)
```

## Deployment

- **Public URL**: https://kalshi.naugle.us via Cloudflare Tunnel `kalshibot`
  (config: `deploy/cloudflared-kalshibot.yml`, separate from other tunnels on
  the machine). DNS routed with
  `cloudflared tunnel --config deploy/cloudflared-kalshibot.yml route dns kalshibot kalshi.naugle.us`.
- **Auth**: every `/api` route and `/ws/live` require `DASHBOARD_TOKEN`
  (bearer / `?token=`); failed attempts are rate-limited per IP; API docs are
  disabled. The n8n REST surface (`/n8n/*`) uses its own
  `N8N_API_BEARER_TOKEN`. Adding Cloudflare Access in front (Cloudflare
  dashboard → Zero Trust → Access) is recommended defense in depth.
- **n8n**: `cd deploy && docker compose up -d` (binds 127.0.0.1:5678).
  **Create the n8n owner account at http://127.0.0.1:5678 BEFORE routing
  n8n.naugle.us DNS** — a fresh n8n lets its first visitor claim it. Then:
  `cloudflared tunnel --config deploy/cloudflared-kalshibot.yml route dns kalshibot n8n.naugle.us`.
- **Dead man's switch**: create a check at healthchecks.io, set
  `HEALTHCHECKS_PING_URL` in `.env`. Pings fire only when feeds, books, scan
  loop, and DB are all healthy; unhealthy states ping `/fail` with reasons.
- **Supervision**: launchd plists in `deploy/launchd/` (bot + tunnel,
  restart-on-crash and on-reboot). **macOS TCC caveat**: launchd agents are
  denied access to `~/Documents`, where this repo lives, so the services hang
  unless you either (a) grant Full Disk Access to
  `/opt/homebrew/bin/cloudflared` and the venv's `python` in System Settings →
  Privacy & Security, or (b) move the repo outside `~/Documents` and update
  the paths in the plists + tunnel config. Until one of those is done, run
  both with `nohup` (see Usage).

### Exposure checklist
- [ ] `curl https://kalshi.naugle.us/api/status` → 401 (auth enforced)
- [ ] `/docs`, `/openapi.json` → 404 (disabled)
- [ ] WS `wss://kalshi.naugle.us/ws/live?token=<bad>` → rejected
- [ ] n8n owner account exists before n8n.naugle.us resolves
- [ ] `DASHBOARD_TOKEN` and `N8N_API_BEARER_TOKEN` set and distinct
- [ ] SQLite/metrics/tape not reachable from any public route

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).

Trading involves risk. This software is provided as-is, without warranty of
any kind (see LICENSE sections 15–16); nothing here is financial advice.
