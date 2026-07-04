# KalshiBot

Autonomous trading bot for Kalshi 15-minute crypto up/down markets. Runs nine
markets (BTC, ETH, SOL, ZEC, HYPE, XRP, DOGE, BNB, NEAR) in parallel with
per-asset strategies, measures each independently, and ranks them by
predictability and profit/loss ratio.

**Current phase: 1 of 12** — Kalshi API client, auth, and series discovery.
Later phases (per the development order): price feeds + tape recording, fill
simulator + replay engine, historical analysis, smart-money signal, shadow
trading loop, evaluation framework, Claude decision engine, dashboard, n8n +
dead man's switch, deployment.

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

## Phase-1 usage

```bash
# Verify auth: local signature proof without keys; live balance call with keys
python scripts/check_auth.py --env demo

# Discover 15-minute series for all nine assets (production, public, no keys)
python scripts/discover_series.py

# Tests
pytest
```

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

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).

Trading involves risk. This software is provided as-is, without warranty of
any kind (see LICENSE sections 15–16); nothing here is financial advice.
