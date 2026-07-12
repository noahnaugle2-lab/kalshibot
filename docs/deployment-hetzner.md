# Migration Plan — Home Machine → Hetzner Cloud VPS

Goal: move KalshiBot off the home machine (which lost DNS for 8 hours on
2026-07-06 with no auto-recovery) onto a supervised, always-on US VPS. Same
SHADOW-mode campaign, real process supervision, and the direct-API decision
engine instead of the Claude CLI.

Nothing here runs itself — it's the runbook. Items needing **you** are marked
⚠️. Do the phases in order; the campaign keeps running on the home machine
until the final cutover, so there's no gap in data.

---

## 0. Why, in one line

A VPS with `systemd` gives crash-restart, reboot-persistence, static
networking, and a US IP next to the exchanges — fixing the exact failure
class we hit at home (DNS drop, launchd/TCC breakage, no supervision).

## 1. Provision the server ⚠️

| Choice | Value | Why |
|---|---|---|
| Provider | Hetzner Cloud | cheapest good fit |
| **Location** | **Ashburn, VA (US-East)** or Hillsboro, OR | Kalshi/Coinbase are US; also un-blocks `binance.us` backup feed (an EU IP geo-blocks it) |
| **Type** | **CAX21** (ARM, 4 vCPU / 8 GB / 80 GB) ≈ €7/mo, or **CX32** (x86, same specs) ≈ €7/mo | stack needs ~700 MB RAM; disk is the real driver |
| Image | Ubuntu 24.04 LTS | systemd, current Python |
| Volume | add a 100–200 GB block volume (~€0.05/GB/mo) **or** rely on the retention job (§6) | the SQLite tape grows ~0.3–0.5 GB/day |

> **ARM note:** CAX21 is ARM64. Everything we run is ARM-clean (Python wheels,
> n8n image, cloudflared arm64). Pick CX32 (x86) only if you'd rather not
> think about architecture.

Create it with your SSH key, note the public IP.

## 2. Base setup (as root, then a non-root user)

```bash
adduser --disabled-password --gecos "" kalshi
usermod -aG sudo,docker kalshi          # docker group added after step 4
apt update && apt -y upgrade
apt -y install python3.11 python3.11-venv git ufw
timedatectl set-ntp true                # clock discipline — the bot refuses to trade on drift
# firewall: SSH only; the dashboard is never exposed except via the tunnel
ufw allow OpenSSH && ufw --force enable
```

## 3. Clone the repo and build the venv (as `kalshi`)

Deliberately **outside `~/Documents`** (that TCC trap was macOS-only, but keep
the clean path): put it at `/home/kalshi/kalshibot`.

```bash
cd ~ && git clone https://github.com/noahnaugle2-lab/kalshibot.git
cd kalshibot
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
mkdir -p secrets data logs
```

## 4. Secrets ⚠️ — never commit these

Recreate `.env` on the server (do **not** copy the home `.env` over an
insecure channel — retype or use `scp` over SSH). Required values:

```
MODE=SHADOW
KALSHI_KEY_ID=...                 # demo key for now; prod key only at go-live
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_private_key.pem
DASHBOARD_TOKEN=<rotate — generate a fresh one, don't reuse the one from chat>
DASHBOARD_PORT=8777
DECISION_PROVIDER=api
DECISION_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-...      # ⚠️ create at console.anthropic.com; ~$17/mo est. for 3 assets
N8N_API_BEARER_TOKEN=<rotate>
N8N_WEBHOOK_BASE_URL=http://127.0.0.1:5678
HEALTHCHECKS_PING_URL=<create at healthchecks.io — the dead man's switch>
```

`chmod 600 .env secrets/*.pem`. The API key is only *used* when an asset has
`ai_enabled: true` in `config/assets.yaml` — it stays $0 until you flip that.

## 5. n8n via Docker

**Current state:** n8n is hosted on the Hetzner VPS alongside KalshiBot. The
authoritative `kalshibot_n8n_data` volume is backed up nightly. The former
laptop container has been removed; its empty volume is retained temporarily
only as a rollback artifact.

```bash
apt -y install docker.io docker-compose-plugin
cd ~/kalshibot/deploy && docker compose up -d       # binds 127.0.0.1:5678
```
The owner account is configured and the editor is protected by Cloudflare
Access. For emergency direct administration, use an SSH port-forward:
`ssh -L 5678:127.0.0.1:5678 kalshi@<ip>`.

## 6. Retention and independent backups

Nightly retention archives aged tape rows to compressed Parquet. A separate
systemd timer creates a transactionally consistent SQLite backup, verifies it,
retains 14 local copies, and records freshness for the health gate. Set
`BACKUP_RCLONE_DEST` to an encrypted off-host rclone destination. Hetzner
snapshots do not include attached Volumes, so they are not a database backup.

```bash
systemctl start kalshibot-backup.service
.venv/bin/python scripts/backup.py --verify-only data/backups/<backup>.db
```

## 7. Supervision — systemd units (replaces launchd)

`/etc/systemd/system/kalshibot.service`:

```ini
[Unit]
Description=KalshiBot shadow trader
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=kalshi
WorkingDirectory=/home/kalshi/kalshibot
ExecStart=/home/kalshi/kalshibot/.venv/bin/python scripts/run_shadow.py
Restart=on-failure
RestartSec=15
StandardOutput=append:/home/kalshi/kalshibot/logs/shadow.log
StandardError=append:/home/kalshi/kalshibot/logs/shadow.log

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/kalshibot-tunnel.service` runs
`cloudflared tunnel --config deploy/cloudflared-kalshibot.yml run kalshibot`
(same pattern, `Restart=always`). Then:

```bash
systemctl daemon-reload
systemctl enable --now kalshibot kalshibot-tunnel kalshibot-backup.timer
```

This is strictly better than the home launchd setup: real crash-restart, real
reboot-persistence, no TCC/FDA issues, `journalctl -u kalshibot` for logs.

## 8. Cloudflare Tunnel and Access ⚠️

The tunnel credentials (`~/.cloudflared/<id>.json`) are machine-bound — either
copy them to the server, or `cloudflared tunnel login` fresh on the server and
re-create the `kalshibot` tunnel + routes (`kalshi.naugle.us`, and
`n8n.naugle.us` only after the owner account exists). Verify the exposure
checklist in the README (401 unauthenticated, /docs 404, WS through the
tunnel).

Cloudflare Access is required for both hostnames. Allow only Noah's identity,
require MFA, and use a service token for automation. Isolate any public n8n
webhook paths from the protected editor. See `deploy/hetzner-firewall.md`.

## 9. Cutover ⚠️

1. On the server, start the services and confirm healthy (feeds connected,
   dashboard 200 through the tunnel, a window settles).
2. **Optionally seed history:** `scp` the home `data/kalshibot.db` to the
   server before first start so the campaign continues with its full record
   rather than restarting the sample. (Copy while the home trader is stopped
   to get a clean file.)
3. Repoint `kalshi.naugle.us` DNS to the server's tunnel.
4. Stop the home machine's trader + tunnel (`pkill`, or unload launchd).
5. Watch the server for one full 15-min window + one nightly cycle.

## 10. Turning on the AI decision layer (later, optional)

The direct-API engine is wired and off. To A/B it on the winners:
- set `ai_enabled: true` under BTC/ETH/SOL (or whichever) in
  `config/assets.yaml`, `smart_money_weight` unchanged;
- ensure `ANTHROPIC_API_KEY` is set and `DECISION_PROVIDER=api`;
- restart. Each decision logs `cost_usd` to the `decisions` table — watch
  actual spend vs the ~$17/mo (3-asset) estimate for a day before widening.
- The scorecard already splits `+ai` runs from baseline, so its contribution
  is measured, not assumed.

## 11. Updates and atomic deployment

`uv.lock` pins Python resolution and n8n is pinned in Compose. Deploy a tested
release with automatic rollback instead of pulling into the live directory:

```bash
sudo /home/kalshi/kalshibot/deploy/deploy.sh main
```

Review n8n and cloudflared releases monthly and back up before upgrades.

---

## What you need to gather before starting

- [ ] ⚠️ Hetzner account + a US-region CAX21/CX32 created
- [ ] ⚠️ Anthropic API key (console.anthropic.com) — only if enabling AI
- [ ] ⚠️ Healthchecks.io ping URL (the dead man's switch, still unset)
- [ ] ⚠️ Decide: copy the tunnel credentials, or re-auth cloudflared on the server
- [ ] ⚠️ Rotate DASHBOARD_TOKEN + N8N_API_BEARER_TOKEN (the current ones appeared in chat)
- [ ] ⚠️ Configure Cloudflare Access with MFA for both applications
- [ ] ⚠️ Attach a Hetzner firewall with SSH restricted to a trusted source
- [ ] ⚠️ Configure `BACKUP_RCLONE_DEST` and complete a restore drill
- [ ] Prod Kalshi key stays home until the 2-week campaign + go-live checklist pass
