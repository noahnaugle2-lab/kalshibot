#!/usr/bin/env bash
# Invoked as a privileged systemd ExecStartPre; the trading user has no Docker access.
set -euo pipefail
out="/home/kalshi/kalshibot/data/backups"
mkdir -p "$out"
docker run --rm \
  -v kalshibot_n8n_data:/data:ro \
  -v "$out":/backup \
  alpine:3.22 \
  tar czf "/backup/n8n-$(date -u +%Y%m%dT%H%M%SZ).tgz" -C /data .
chown -R kalshi:kalshi "$out"
find "$out" -name 'n8n-*.tgz' -mtime +14 -delete
