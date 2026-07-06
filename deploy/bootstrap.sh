#!/usr/bin/env bash
# One-shot server setup for KalshiBot on a fresh Ubuntu 24.04 Hetzner VPS.
# Idempotent — safe to re-run. Run as root (or with sudo):
#
#   scp deploy/bootstrap.sh root@<server-ip>:/root/
#   ssh root@<server-ip> 'bash /root/bootstrap.sh'
#
# Then: fill /home/kalshi/kalshibot/.env, create the n8n owner account,
# authenticate cloudflared, and `systemctl enable --now kalshibot kalshibot-tunnel`.
set -euo pipefail

REPO="https://github.com/noahnaugle2-lab/kalshibot.git"
USER_HOME="/home/kalshi"
APP_DIR="$USER_HOME/kalshibot"
ARCH="$(dpkg --print-architecture)"   # arm64 or amd64

echo "==> [1/8] packages"
apt-get update -y
apt-get install -y python3.11 python3.11-venv git ufw curl ca-certificates \
    docker.io docker-compose-plugin

echo "==> [2/8] user + firewall + NTP"
id kalshi &>/dev/null || adduser --disabled-password --gecos "" kalshi
usermod -aG sudo,docker kalshi
timedatectl set-ntp true
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "==> [3/8] clone repo"
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u kalshi git clone "$REPO" "$APP_DIR"
else
    sudo -u kalshi git -C "$APP_DIR" pull --ff-only
fi
sudo -u kalshi mkdir -p "$APP_DIR/secrets" "$APP_DIR/data" "$APP_DIR/logs"

echo "==> [4/8] python venv"
sudo -u kalshi python3.11 -m venv "$APP_DIR/.venv"
sudo -u kalshi "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u kalshi "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

echo "==> [5/8] cloudflared ($ARCH)"
if ! command -v cloudflared &>/dev/null; then
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH" \
        -o /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
fi

echo "==> [6/8] n8n container"
(cd "$APP_DIR/deploy" && docker compose up -d)

echo "==> [7/8] systemd units"
cp "$APP_DIR/deploy/systemd/kalshibot.service" /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/kalshibot-tunnel.service" /etc/systemd/system/
systemctl daemon-reload
# NOT started yet — needs .env + cloudflared auth first (step 8)

echo "==> [8/8] .env scaffold"
if [ ! -f "$APP_DIR/.env" ]; then
    sudo -u kalshi cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

cat <<'DONE'

============================================================
Bootstrap complete. Remaining manual steps (need your input):

1. Fill  /home/kalshi/kalshibot/.env  (MODE=SHADOW, Kalshi demo key,
   fresh DASHBOARD_TOKEN, ANTHROPIC_API_KEY if enabling AI, n8n token,
   HEALTHCHECKS_PING_URL). Put the Kalshi PEM in ./secrets/ (chmod 600).

2. cloudflared auth + tunnel:
     sudo -u kalshi cloudflared tunnel login
     sudo -u kalshi cloudflared tunnel create kalshibot   # OR copy the
       existing tunnel's ~/.cloudflared/<id>.json from the home machine
   Ensure deploy/cloudflared-kalshibot.yml points at the tunnel + creds file.

3. n8n owner account: SSH-forward and open it BEFORE routing its DNS:
     ssh -L 5678:127.0.0.1:5678 kalshi@<server-ip>   # then visit localhost:5678

4. Seed history (optional): scp the home data/kalshibot.db into
   /home/kalshi/kalshibot/data/ (copy while the home trader is stopped).

5. Start it:
     systemctl enable --now kalshibot kalshibot-tunnel
     journalctl -u kalshibot -f

6. Route DNS + verify exposure checklist (README):
     sudo -u kalshi cloudflared tunnel --config deploy/cloudflared-kalshibot.yml \
       route dns kalshibot kalshi.naugle.us
============================================================
DONE
