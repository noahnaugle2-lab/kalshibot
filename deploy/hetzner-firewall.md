# Hetzner and Cloudflare account controls

These controls cannot be applied from the repository. Complete them in the
provider consoles before treating the deployment as hardened.

## Hetzner Cloud Firewall

- Attach a firewall to the VPS.
- Permit inbound TCP/22 only from Noah's trusted source IP or VPN range.
- Do not permit inbound 5678, 8777, 80, or 443. Cloudflare Tunnel is outbound.
- Keep UFW enabled as host-level defense in depth.
- Enable Hetzner server backups. Remember that attached Volumes are excluded.

## Cloudflare Zero Trust

- Create separate self-hosted Access applications for `kalshi.naugle.us` and
  the n8n editor at `n8n.naugle.us`.
- Allow only Noah's identity and require MFA.
- Use short n8n sessions and retain Access audit logs.
- Put public n8n webhook paths on a separate hostname or Access bypass policy
  limited to the exact webhook paths. Never bypass the editor or REST API.
- Create a service token for machine-to-machine requests instead of sharing a
  human session or dashboard bearer token.

## Verification

From a machine outside Hetzner, confirm the server IP refuses ports 80, 443,
5678, and 8777. Confirm both hostnames challenge through Access, bad bearer
tokens still fail at the application, and WebSockets reconnect successfully.
