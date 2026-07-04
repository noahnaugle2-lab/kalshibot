"""RSA-PSS signing tests: round-trip verification and header structure."""

import base64
import time

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshibot.kalshi.auth import KalshiSigner


@pytest.fixture(scope="module")
def keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def signer(keypair: rsa.RSAPrivateKey) -> KalshiSigner:
    return KalshiSigner("test-key-id", keypair)


def _verify(keypair: rsa.RSAPrivateKey, signature_b64: str, message: str) -> None:
    keypair.public_key().verify(
        base64.b64decode(signature_b64),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signature_verifies_against_public_key(signer, keypair):
    sig = signer.sign("1700000000000", "GET", "/trade-api/v2/portfolio/balance")
    _verify(keypair, sig, "1700000000000GET/trade-api/v2/portfolio/balance")


def test_query_string_excluded_from_signature(signer, keypair):
    sig = signer.sign("1700000000000", "GET", "/trade-api/v2/markets?limit=100&cursor=abc")
    _verify(keypair, sig, "1700000000000GET/trade-api/v2/markets")


def test_method_uppercased(signer, keypair):
    sig = signer.sign("1700000000000", "post", "/trade-api/v2/portfolio/orders")
    _verify(keypair, sig, "1700000000000POST/trade-api/v2/portfolio/orders")


def test_tampered_message_fails_verification(signer, keypair):
    sig = signer.sign("1700000000000", "GET", "/trade-api/v2/portfolio/balance")
    with pytest.raises(InvalidSignature):
        _verify(keypair, sig, "1700000000001GET/trade-api/v2/portfolio/balance")


def test_auth_headers_shape(signer):
    before_ms = int(time.time() * 1000)
    headers = signer.auth_headers("GET", "/trade-api/v2/portfolio/balance")
    after_ms = int(time.time() * 1000)

    assert set(headers) == {
        "KALSHI-ACCESS-KEY",
        "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert before_ms <= int(headers["KALSHI-ACCESS-TIMESTAMP"]) <= after_ms
    base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])  # valid base64


def test_pem_roundtrip(tmp_path, keypair):
    from cryptography.hazmat.primitives import serialization

    pem_path = tmp_path / "key.pem"
    pem_path.write_bytes(
        keypair.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    signer = KalshiSigner.from_pem_file("k", pem_path)
    sig = signer.sign("1700000000000", "GET", "/trade-api/v2/exchange/status")
    _verify(keypair, sig, "1700000000000GET/trade-api/v2/exchange/status")
