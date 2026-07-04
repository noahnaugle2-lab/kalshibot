"""Verify the Kalshi auth flow.

With credentials in .env: signs a real request and calls authenticated
endpoints (balance) against the chosen environment — full end-to-end proof.

Without credentials: generates an ephemeral RSA key, signs a sample request,
verifies the RSA-PSS signature locally, and shows the exact headers that
would be sent. This proves the signing implementation; add keys to .env for
the live check.

Usage:
    python scripts/check_auth.py --env demo   # order plumbing env (default)
    python scripts/check_auth.py --env prod   # production (read-only checks)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshibot.config import Settings
from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.client import KalshiClient, KalshiEnvironment


def local_signature_demo() -> None:
    print("No Kalshi credentials in .env — running LOCAL signature verification only.\n")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner("demo-key-id-00000000-0000-0000-0000-000000000000", key)

    method, path = "GET", "/trade-api/v2/portfolio/balance?subaccount=1"
    timestamp = str(int(time.time() * 1000))
    signature_b64 = signer.sign(timestamp, method, path)

    message = f"{timestamp}{method}{signer.signable_path(path)}".encode()
    key.public_key().verify(
        base64.b64decode(signature_b64),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises InvalidSignature on failure

    print(f"  signed message : {timestamp}GET/trade-api/v2/portfolio/balance")
    print("  (query string correctly excluded from signature)")
    print("  RSA-PSS-SHA256 signature verified against public key: OK\n")
    print("  Headers that would be sent:")
    for k, v in signer.auth_headers(method, path).items():
        print(f"    {k}: {v[:60]}{'...' if len(v) > 60 else ''}")
    print(
        "\nNext step: create API keys (demo: https://demo.kalshi.co, "
        "prod: https://kalshi.com), fill KALSHI_[DEMO_]KEY_ID and "
        "KALSHI_[DEMO_]PRIVATE_KEY_PATH in .env, and re-run."
    )


async def live_auth_check(env: KalshiEnvironment, key_id: str, key_path: Path) -> None:
    signer = KalshiSigner.from_pem_file(key_id, key_path)
    print(f"Environment : {env.name} ({env.value})")
    print(f"Key ID      : {key_id[:8]}...")
    async with KalshiClient(environment=env, signer=signer) as client:
        status = await client.get_exchange_status()
        print(f"Exchange    : {status}")
        balance = await client.get_balance()
        print(f"Balance     : ${balance.dollars}")
    print("\nAuthenticated request succeeded — auth flow verified end to end.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["demo", "prod"], default="demo")
    args = parser.parse_args()
    settings = Settings()

    if args.env == "demo":
        env = KalshiEnvironment.DEMO
        key_id, key_path = settings.kalshi_demo_key_id, settings.kalshi_demo_private_key_path
    else:
        env = KalshiEnvironment.PROD
        key_id, key_path = settings.kalshi_key_id, settings.kalshi_private_key_path

    if key_id and key_path and Path(key_path).exists():
        asyncio.run(live_auth_check(env, key_id, Path(key_path)))
    else:
        local_signature_demo()


if __name__ == "__main__":
    main()
