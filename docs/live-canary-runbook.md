# Funded canary runbook

This is the manual path from the production-path dry run to a one-contract
funded canary. A deployment may install `kalshibot-live.service`, but it never
creates the live environment file, enables the unit, or starts it.

## Release gate

Do not create `/etc/kalshibot-live.env` until all checks pass:

- At least 50 settled, post-change production-path dry-run outcomes.
- One-contract-normalized profit factor at least 1.5 after simulated fees and
  crossing costs.
- Positive one-contract-normalized net PnL for every asset considered for the
  canary.
- At least 80% of settled dry-run outcomes are comparable with shadow, and
  direction agrees on at least 90% of that set. Investigate every mismatch.
- Dry-run versus shadow size and disposition deltas are reviewed. Independent
  capture timing and book depth can legitimately change simulated fill size.
- No startup or continuous reconciliation mismatches.
- No unresolved `submit_unknown` live order and no open live position.
- Held-out replay remains profitable with one-contract-normalized drawdown
  inside the $5 canary budget.
- Demo exchange order plumbing passes place, reconcile, and cancel.

Use one asset and one 15-minute contract at first. Prefer the asset with the
strongest held-out and dry-run evidence. Do not expand until 25 to 50 funded
orders settle cleanly.

Run the repeatable mechanical gate report against the post-change baseline:

```bash
python scripts/canary_readiness.py --since <UTC_EPOCH> --target 50
```

The command exits nonzero until every mechanical gate passes. Held-out replay,
demo plumbing, asset selection, and operator sign-off remain explicit manual
gates in its output.

## Demo plumbing

Demo credentials are separate from production credentials. Run:

```bash
python scripts/demo_order_plumbing.py
```

The script is constructed with the Demo API host and cannot reach production.

## Prepare the host

1. Create a new production API key with write scope. Keep the existing
   read-only key in `/etc/kalshibot-live-dry-run.env`.
2. Copy `deploy/kalshibot-live.env.example` to `/etc/kalshibot-live.env` and
   set ownership to `root:root`, mode `0600`.
3. Put the write-key ID and an absolute private-key path in that file.
4. Keep `LIVE_TRADING_ENABLED=false`, leave the confirmation blank, and leave
   the asset allowlist blank while checking the unit.
5. Engage the dashboard kill switch. The funded process refuses to start if
   the latest persisted control is absent, false, or malformed.

Validate the inert configuration:

```bash
systemd-analyze verify /etc/systemd/system/kalshibot-live.service
systemctl is-enabled kalshibot-live.service   # expected: static
systemctl is-active kalshibot-live.service    # expected: inactive
```

## Arm and start

Only after the release gate is signed off, set exactly:

```dotenv
MODE=LIVE
LIVE_TRADING_ENABLED=true
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_ORDERS
LIVE_ALLOWED_ASSETS=SOL
LIVE_MAX_CONTRACTS_PER_ORDER=1
LIVE_MAX_DAILY_LOSS_USD=5
```

Then start manually. Do not enable the unit.

```bash
systemctl start kalshibot-live.service
journalctl -u kalshibot-live.service -n 100 --no-pager
tail -n 100 /home/kalshi/kalshibot/logs/funded-live.log
```

Startup must show a verified write scope, an engaged kill switch, and a clean
reconciliation. Starting the funded unit stops the conflicting dry-run unit.
It binds to the shadow service because the shadow service owns the canonical
market-data and signal stream. It ignores the market window already open at
startup and first becomes eligible in the next complete 15-minute window.

After all startup checks pass, disengage the kill switch in the dashboard.
The supervisor rereads that persisted control before every exchange write.

## Monitor and stop

Inspect the durable audit trail:

```bash
sqlite3 /home/kalshi/kalshibot/data/kalshibot.db \
  "SELECT run_ts,status,detail FROM live_reconciliations ORDER BY id DESC LIMIT 5;"
sqlite3 /home/kalshi/kalshibot/data/kalshibot.db \
  "SELECT created_ts,asset,market_ticker,status,reason FROM live_decisions ORDER BY created_ts DESC LIMIT 10;"
sqlite3 /home/kalshi/kalshibot/data/kalshibot.db \
  "SELECT market_ticker,pnl_net,settled_ts FROM live_position_outcomes ORDER BY settled_ts DESC LIMIT 10;"
```

On any anomaly, engage the dashboard kill switch and stop the unit:

```bash
systemctl stop kalshibot-live.service
```

Any ambiguous submission, reconciliation mismatch, or supervised-loop failure
terminates the process. `Restart=no` prevents automatic re-entry. Reconcile the
Kalshi portfolio with `live_orders` and `live_positions` before another start.
