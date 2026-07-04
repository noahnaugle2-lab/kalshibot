"""RSA-PSS request signing for the Kalshi Trade API v2.

Kalshi authenticates each request with three headers:

    KALSHI-ACCESS-KEY        the API key ID (UUID)
    KALSHI-ACCESS-TIMESTAMP  current unix time in milliseconds
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed path includes the /trade-api/v2 prefix and EXCLUDES the query string.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiSigner:
    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_pem_file(
        cls, key_id: str, pem_path: str | Path, password: bytes | None = None
    ) -> "KalshiSigner":
        pem = Path(pem_path).read_bytes()
        key = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError(f"Expected an RSA private key, got {type(key).__name__}")
        return cls(key_id, key)

    @staticmethod
    def signable_path(path: str) -> str:
        """Strip the query string; the signature covers only the bare path."""
        return path.split("?", 1)[0]

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{self.signable_path(path)}"
        signature = self._private_key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self.sign(timestamp_ms, method, path),
        }
