"""Ed25519 signatures for envelopes.

Two layers:
- Agent signature: an agent that registers a public key must sign every
  envelope it sends. The signature covers the canonical form of the
  EnvelopeIn (everything except `signature`).
- Server countersignature: the server signs every stored envelope
  (content + seq + sender + thread + timestamp), so anyone holding the
  server's public key (GET /v1/keys) can check a message was not altered
  after it was accepted. This is what federation and audit build on.

Canonical form: the envelope as JSON with every field present (defaults
filled), except `memory_refs` which is omitted when empty, keys sorted,
no whitespace, UTF-8. Python clients get this for
free via `canonical_bytes`; other languages must reproduce it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .models import Envelope, EnvelopeIn

_RAW = serialization.Encoding.Raw


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64), raw 32-byte keys, base64url."""
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(_RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    return _b64(priv), _b64(pub)


def public_from_private(private_b64: str) -> str:
    k = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64))
    return _b64(k.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw))


def valid_public_key(public_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_b64))
        return True
    except Exception:
        return False


# Fields added after v0.2 are left out of the canonical form while empty, so
# v0.2 clients' signatures and stored countersignatures keep verifying.
_OMIT_WHEN_EMPTY = ("memory_refs",)


def _canon(obj: Any) -> bytes:
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if not (k in _OMIT_WHEN_EMPTY and not v)}
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_bytes(env: EnvelopeIn | dict[str, Any]) -> bytes:
    if isinstance(env, dict):
        env = EnvelopeIn.model_validate(env)
    data = EnvelopeIn.model_validate(env.model_dump()).model_dump(mode="json", exclude={"signature"})
    return _canon(data)


def sign(env: EnvelopeIn | dict[str, Any], private_b64: str) -> str:
    k = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64))
    return _b64(k.sign(canonical_bytes(env)))


def verify(env: EnvelopeIn, signature: str, public_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_b64)).verify(_unb64(signature), canonical_bytes(env))
        return True
    except (InvalidSignature, ValueError):
        return False


# ------------------------------------------------------------------ server

def stored_bytes(env: Envelope) -> bytes:
    """What the server countersigns: the full stored envelope minus its own
    signature and the derived `verified` flag."""
    return _canon(env.model_dump(mode="json", exclude={"server_sig", "verified"}))


class ServerSigner:
    def __init__(self, private_b64: str | None = None, key_file: str | None = None) -> None:
        private_b64 = private_b64 or os.environ.get("AMSG_SERVER_KEY")
        if not private_b64 and key_file:
            p = Path(key_file)
            if p.exists():
                private_b64 = p.read_text().strip()
            else:
                private_b64, _ = generate_keypair()
                p.write_text(private_b64)
                try:
                    p.chmod(0o600)
                except OSError:
                    pass
        if not private_b64:
            private_b64, _ = generate_keypair()
        self._key = Ed25519PrivateKey.from_private_bytes(_unb64(private_b64))
        self.public_key = public_from_private(private_b64)

    def sign(self, env: Envelope) -> str:
        return _b64(self._key.sign(stored_bytes(env)))


def verify_stored(env: Envelope, server_public_b64: str) -> bool:
    if not env.server_sig:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(server_public_b64)).verify(_unb64(env.server_sig), stored_bytes(env))
        return True
    except (InvalidSignature, ValueError):
        return False
