# KalshiBot

Autonomous trading bot for Kalshi 15-minute crypto up/down markets. Runs nine
markets (BTC, ETH, SOL, ZEC, HYPE, XRP, DOGE, BNB, NEAR) in parallel with
per-asset strategies, measures each independently, and ranks them by
predictability and profit/loss ratio.

**Current phase: shadow validation campaign.** The production-data pipeline,
fill simulator, replay and evaluation tooling, smart-money signals, shadow
trading loop, Claude decision layer, authenticated dashboard, monitoring, and
deployment definitions are implemented. Real-money order execution remains
disabled. Current strategy evaluation is concentrated on the enabled assets in
`config/assets.yaml`; paused assets may remain active as data sources.

## Modes (SHADOW / DEMO / LIVE)

| Mode | Market data | Orders | Purpose |
|------|-------------|--------|---------|
| `SHADOW` (default) | Production, read-only | Local fill simulator | **All strategy evaluation happens here** |
| `DEMO` | Demo exchange | Real orders, fake money | Order plumbing tests only — never strategy evaluation |
| `LIVE` | Production | Real money | Off until manually enabled after a 2-week shadow campaign |

`MODE=LIVE` alone is deliberately insufficient. The live executor also
requires `LIVE_TRADING_ENABLED=true`, the exact confirmation phrase
`I_UNDERSTAND_REAL_ORDERS`, a non-empty allowlist in `LIVE_ALLOWED_ASSETS`, a
separate production write key, and an operator-engaged persisted kill switch
at startup. Its initial order cap defaults to one contract and its daily-loss
cap defaults to $5. The separate `kalshibot-live.service` has no install target,
uses `Restart=no`, and is never started by deployment. See
`docs/live-canary-runbook.md` for the manual canary gate.

For a production-path rehearsal without submitting orders, set
`LIVE_DRY_RUN_ENABLED=true` while retaining `MODE=SHADOW` and run
`scripts/run_live_dry_run.py`. The supervisor requires authenticated
production read access, reconciles exchange positions/orders against the
local live ledger at startup, and records only `live_proposals`.

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
                          evaluation, smartmoney, persistence, dashboard
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
  `N8N_API_BEARER_TOKEN`. Cloudflare Access with MFA is required in front of
  both the dashboard and n8n editor.
- **n8n**: runs on the Hetzner VPS via Docker and binds only to
  `127.0.0.1:5678`. Its owner account is configured, the editor is protected
  by Cloudflare Access, and its named volume is included in nightly backups.
- **Dead man's switch**: create a check at healthchecks.io, set
  `HEALTHCHECKS_PING_URL` in `.env`. Pings fire only when feeds, books, scan
  loop, DB, disk capacity, backup freshness, and write backlog are healthy;
  unhealthy states ping `/fail` with reasons. Critical disk pressure engages
  the kill switch.
- **Retention**: `kalshibot-retention.timer` keeps seven hot days in SQLite and
  archives older high-volume tape to compressed Parquet before the backup run.
- **Backups**: `kalshibot-backup.timer` creates a verified SQLite backup nightly.
  Set `BACKUP_RCLONE_DEST` for off-host copies and test restores regularly.
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
- [ ] Cloudflare Access challenges both dashboard and n8n editor hostnames
- [ ] Latest verified offsite backup is under 26 hours old
- [ ] VPS public IP refuses ports 80, 443, 5678, and 8777

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).

Trading involves risk. This software is provided as-is, without warranty of
any kind (see LICENSE sections 15–16); nothing here is financial advice.
