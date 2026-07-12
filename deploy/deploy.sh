#!/usr/bin/env bash
# Build, verify, and atomically activate a release. Run as root on Hetzner.
set -euo pipefail

REPO="${REPO:-https://github.com/noahnaugle2-lab/kalshibot.git}"
BASE="/home/kalshi"
CURRENT="$BASE/kalshibot"
RELEASES="$BASE/releases"
SHARED="$BASE/shared"
REF="${1:-main}"

if ! command -v uv >/dev/null; then
    python3 -m venv /opt/kalshibot-uv
    /opt/kalshibot-uv/bin/pip install -q "uv==0.11.5"
    ln -sfn /opt/kalshibot-uv/bin/uv /usr/local/bin/uv
fi
if ! command -v npm >/dev/null; then
    apt-get update -y
    apt-get install -y nodejs npm
fi

install -d -o kalshi -g kalshi "$RELEASES" "$SHARED" \
    "$SHARED/data" "$SHARED/logs" "$SHARED/secrets"
release="$RELEASES/$(date -u +%Y%m%dT%H%M%SZ)"
sudo -u kalshi git clone --quiet --branch "$REF" --depth 1 "$REPO" "$release"

# One-time migration from the original in-place layout to shared state.
if [ -d "$CURRENT" ] && [ ! -L "$CURRENT" ]; then
    systemctl stop kalshibot kalshibot-tunnel 2>/dev/null || true
    for name in data logs secrets; do
        if [ -d "$CURRENT/$name" ]; then
            cp -a "$CURRENT/$name/." "$SHARED/$name/"
        fi
    done
    if [ -f "$CURRENT/.env" ]; then
        install -o kalshi -g kalshi -m 600 "$CURRENT/.env" "$SHARED/.env"
    fi
    mv "$CURRENT" "$RELEASES/pre-atomic-$(date -u +%Y%m%dT%H%M%SZ)"
fi

for name in data logs secrets; do
    rm -rf "$release/$name"
    ln -s "$SHARED/$name" "$release/$name"
done
if [ ! -f "$SHARED/.env" ]; then
    install -o kalshi -g kalshi -m 600 "$release/.env.example" "$SHARED/.env"
fi
ln -s "$SHARED/.env" "$release/.env"

sudo -u kalshi uv sync --frozen --all-extras --directory "$release"
(cd "$release" && sudo -u kalshi .venv/bin/pytest -q tests)
sudo -u kalshi npm --prefix "$release/dashboard" ci
sudo -u kalshi npm --prefix "$release/dashboard" run build

previous="$(readlink -f "$CURRENT" 2>/dev/null || true)"
ln -sfn "$release" "$CURRENT.next"
mv -Tf "$CURRENT.next" "$CURRENT"

# Install the release's host controls before restarting into it.
cp "$CURRENT/deploy/systemd/kalshibot.service" /etc/systemd/system/
cp "$CURRENT/deploy/systemd/kalshibot-tunnel.service" /etc/systemd/system/
cp "$CURRENT/deploy/systemd/kalshibot-backup.service" /etc/systemd/system/
cp "$CURRENT/deploy/systemd/kalshibot-backup.timer" /etc/systemd/system/
cp "$CURRENT/deploy/logrotate/kalshibot" /etc/logrotate.d/kalshibot
cp "$CURRENT/deploy/sshd-hardening.conf" /etc/ssh/sshd_config.d/60-kalshibot-hardening.conf
chown root:root /etc/logrotate.d/kalshibot
chmod 644 /etc/logrotate.d/kalshibot
sshd -t
systemctl reload ssh
systemctl daemon-reload
systemctl enable --now kalshibot-backup.timer
(cd "$CURRENT/deploy" && docker compose up -d n8n)
deluser kalshi sudo 2>/dev/null || true
deluser kalshi docker 2>/dev/null || true

if ! systemctl restart kalshibot kalshibot-tunnel; then
    if [ -n "$previous" ] && [ -d "$previous" ]; then
        ln -sfn "$previous" "$CURRENT"
        systemctl restart kalshibot kalshibot-tunnel
    fi
    exit 1
fi
sleep 3
systemctl is-active --quiet kalshibot kalshibot-tunnel

find "$RELEASES" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
    | sort -nr | awk 'NR>5 {print $2}' | xargs -r rm -rf
echo "activated $release"
