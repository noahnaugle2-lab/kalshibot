"""WebAuthn passkey (Face ID / Touch ID) auth for the dashboard.

Design:
- Bootstrap trust: the FIRST passkey enrollment is gated by the bearer
  DASHBOARD_TOKEN (register endpoints depend on require_auth). Nobody can
  register a device without the token, so the token is the root of trust.
- After enrolling, a Face ID / Touch ID assertion issues a signed session
  cookie (`wa_session`); the bearer token stays a permanent fallback so a
  lost/broken passkey never locks you out.
- Challenges are held in a short-lived signed cookie (`wa_chal`), so the
  server stays stateless across the two-step ceremony (survives restarts,
  no server-side challenge store).

rp_id / origin come from settings and MUST match the host the browser sees
(kalshi.naugle.us / https://kalshi.naugle.us) — a passkey is cryptographically
bound to them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_NAME = "KalshiBot"
# stable user handle for the single dashboard owner
USER_ID = b"kalshibot-dashboard-owner"
USER_NAME = "kalshibot"
CHALLENGE_TTL_S = 300
SESSION_TTL_S = 30 * 86400


# --------------------------------------------------------------- signed cookies

def sign(secret: str, payload: dict, ttl: int) -> str:
    """Return a compact `body.sig` token (HMAC-SHA256), expiring in `ttl`s."""
    body = {"d": payload, "exp": int(time.time()) + ttl}
    raw = json.dumps(body, separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(raw).rstrip(b"=")
    sig = hmac.new(secret.encode(), b, hashlib.sha256).digest()
    s = base64.urlsafe_b64encode(sig).rstrip(b"=")
    return b.decode() + "." + s.decode()


def verify(secret: str, token: str | None) -> dict | None:
    """Return the payload if the token is authentic and unexpired, else None."""
    if not token or "." not in token:
        return None
    try:
        b, s = token.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), b.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        if not hmac.compare_digest(s, expected):
            return None
        body = json.loads(base64.urlsafe_b64decode(b + "==="))
        if int(body.get("exp", 0)) < time.time():
            return None
        return body.get("d")
    except Exception:
        return None


# ------------------------------------------------------------------ credentials

def list_credentials(db) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.query(
            "SELECT credential_id, public_key, sign_count FROM webauthn_credentials"
        )
    ]


def has_credentials(db) -> bool:
    return bool(db.query("SELECT 1 FROM webauthn_credentials LIMIT 1"))


def save_credential(
    db, credential_id: str, public_key: str, sign_count: int, transports: str, label: str
) -> None:
    db.write_now(
        "webauthn_credentials",
        {
            "credential_id": credential_id,
            "public_key": public_key,
            "sign_count": sign_count,
            "transports": transports,
            "label": label,
            "created_ts": time.time(),
        },
    )


def update_sign_count(db, credential_id: str, sign_count: int) -> None:
    db.write_now_sql(
        "UPDATE webauthn_credentials SET sign_count=? WHERE credential_id=?",
        (sign_count, credential_id),
    )


# -------------------------------------------------------------------- ceremonies

# Consumed challenges (single-use per WebAuthn spec). The challenge lives in a
# stateless signed cookie, so without this a captured (challenge, assertion)
# pair could be replayed within the 300s TTL. In-memory is sufficient: entries
# self-expire in CHALLENGE_TTL_S and a restart is a shorter window than the TTL.
_consumed_challenges: dict[str, float] = {}


def _consume_challenge(challenge_b64: str) -> None:
    """Mark a challenge used; raise if it was already used (replay)."""
    now = time.time()
    for k in [k for k, exp in _consumed_challenges.items() if exp < now]:
        _consumed_challenges.pop(k, None)
    if challenge_b64 in _consumed_challenges:
        raise ValueError("challenge already used (replay rejected)")
    _consumed_challenges[challenge_b64] = now + CHALLENGE_TTL_S


def registration_options(db, rp_id: str) -> tuple[str, str]:
    """Return (options_json, challenge_b64) for a new passkey enrollment."""
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=USER_ID,
        user_name=USER_NAME,
        user_display_name="Dashboard owner",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in list_credentials(db)
        ],
    )
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def verify_registration(
    db, credential: dict, challenge_b64: str, rp_id: str, origin: str, label: str
) -> None:
    """Verify an attestation and persist the new credential. Raises on failure."""
    _consume_challenge(challenge_b64)
    v = verify_registration_response(
        credential=json.dumps(credential),
        expected_challenge=base64url_to_bytes(challenge_b64),
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    save_credential(
        db,
        bytes_to_base64url(v.credential_id),
        bytes_to_base64url(v.credential_public_key),
        v.sign_count,
        json.dumps(credential.get("response", {}).get("transports", [])),
        label or "passkey",
    )


def authentication_options(db, rp_id: str) -> tuple[str, str]:
    """Return (options_json, challenge_b64) for a login assertion."""
    creds = list_credentials(db)
    if not creds:
        raise ValueError("no passkeys registered")
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"]))
            for c in creds
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def verify_authentication(
    db, credential: dict, challenge_b64: str, rp_id: str, origin: str
) -> None:
    """Verify a login assertion + bump the stored sign counter. Raises on failure."""
    cred_id = credential.get("id") or credential.get("rawId")
    rows = db.query(
        "SELECT public_key, sign_count FROM webauthn_credentials WHERE credential_id=?",
        (cred_id,),
    )
    if not rows:
        raise ValueError("unknown credential")
    _consume_challenge(challenge_b64)
    v = verify_authentication_response(
        credential=json.dumps(credential),
        expected_challenge=base64url_to_bytes(challenge_b64),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=base64url_to_bytes(rows[0]["public_key"]),
        credential_current_sign_count=rows[0]["sign_count"],
    )
    update_sign_count(db, cred_id, v.new_sign_count)
