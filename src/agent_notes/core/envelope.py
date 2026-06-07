"""DSSE envelope support for the work-log kernel (Plan 008 P0–P1).

P0: envelopes are emitted but **not verified** — the verifier CLI is built in P1.
P1: a standalone verifier CLI checks DSSE sigs + hash chain + policy.

Design:
- Signer is an interface; local-key is the P0 implementation.
- Keyless/Sigstore is added behind the same interface in P1+.
- Predicate: custom type derived from agentattest's ``agent-provenance-v0``,
  trimmed to op granularity.
- Every op carries an envelope in its payload. The envelope itself is not
  part of the op hash (the op is the payload *inside* the envelope).

Borrowed model: in-toto Statement v1 + DSSE envelope.
Reference: https://github.com/in-toto/attestation/blob/main/spec/v1.0/statement.md
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Protocol

# ---------------------------------------------------------------------------
# Signer interface
# ---------------------------------------------------------------------------


class Signer(Protocol):
    """Interface for signing operations."""

    def sign(self, payload: bytes) -> bytes:
        """Sign the payload bytes and return the signature bytes."""
        ...

    def key_id(self) -> str:
        """Return a stable identifier for this signer's key."""
        ...


# ---------------------------------------------------------------------------
# Local-key signer (P0)
# ---------------------------------------------------------------------------


class LocalKeySigner:
    """Signer using a local Ed25519 private key.

    The key is loaded from a file path. If the file doesn't exist, a new
    keypair is generated and saved. This works offline / air-gapped.
    """

    def __init__(self, key_path: str | None = None) -> None:
        from pathlib import Path

        self._key_path = Path(key_path or self._default_key_path())
        self._private_key, self._public_key = self._load_or_generate()

    @staticmethod
    def _default_key_path() -> str:
        import os
        from pathlib import Path

        base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
        return str(Path(base).expanduser() / "agent-notes" / "signing.key")

    def _load_or_generate(self) -> tuple[bytes, bytes]:
        if self._key_path.exists():
            data = json.loads(self._key_path.read_text())
            return (
                base64.b64decode(data["private_key"]),
                base64.b64decode(data["public_key"]),
            )

        # Generate a new Ed25519 keypair.
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as exc:
            raise RuntimeError(
                "cryptography package required for local-key signing. "
                "Install with: uv pip install cryptography"
            ) from exc

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.write_text(
            json.dumps(
                {
                    "private_key": base64.b64encode(private_bytes).decode(),
                    "public_key": base64.b64encode(public_bytes).decode(),
                }
            )
        )
        self._key_path.chmod(0o600)
        return private_bytes, public_bytes

    def sign(self, payload: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as exc:
            raise RuntimeError("cryptography package required for signing") from exc

        private_key = Ed25519PrivateKey.from_private_bytes(self._private_key)
        return private_key.sign(payload)

    def key_id(self) -> str:
        return hashlib.sha256(self._public_key).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Null signer (P0 default — envelopes emitted but not signed)
# ---------------------------------------------------------------------------


class NullSigner:
    """Signer that emits a placeholder signature.

    This is the P0 default: the envelope format is present from day one so
    that flipping enforcement on in P1 requires no schema migration.
    """

    def sign(self, payload: bytes) -> bytes:
        return b"UNSIGNED"

    def key_id(self) -> str:
        return "null"


# ---------------------------------------------------------------------------
# DSSE envelope
# ---------------------------------------------------------------------------


def make_envelope(
    payload_type: str,
    payload: dict,
    signer: Signer | None = None,
) -> dict:
    """Build a DSSE-style envelope around a payload.

    The envelope format is:
    {
        "payloadType": str,
        "payload": base64(json(payload)),
        "signatures": [
            {"sig": base64(signature), "keyid": signer.key_id()}
        ]
    }

    For P0, ``signer`` defaults to ``NullSigner`` so the envelope is always
    structurally present but the signature is a placeholder.
    """
    signer = signer or NullSigner()
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = signer.sign(payload_bytes)
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload_bytes).decode(),
        "signatures": [
            {
                "sig": base64.b64encode(signature).decode(),
                "keyid": signer.key_id(),
            }
        ],
    }


def parse_envelope(envelope: dict) -> dict:
    """Parse a DSSE envelope and return the inner payload dict.

    Does NOT verify the signature — this is P0 behavior. The verifier CLI
    (P1) does the actual verification.
    """
    payload_b64 = envelope.get("payload", "")
    payload_bytes = base64.b64decode(payload_b64)
    return json.loads(payload_bytes)


def verify_envelope(envelope: dict, public_key: bytes) -> dict:
    """Verify a DSSE envelope signature and return the payload.

    This is the P1 primitive. The verifier CLI will call this.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise RuntimeError("cryptography package required for verification") from exc

    payload_b64 = envelope.get("payload", "")
    payload_bytes = base64.b64decode(payload_b64)

    for sig_entry in envelope.get("signatures", []):
        sig = base64.b64decode(sig_entry.get("sig", ""))
        public_key_obj = Ed25519PublicKey.from_public_bytes(public_key)
        try:
            public_key_obj.verify(sig, payload_bytes)
            return json.loads(payload_bytes)
        except InvalidSignature:
            continue

    raise ValueError("No valid signature found in envelope")


# ---------------------------------------------------------------------------
# Predicate helpers (agent-provenance-v0 derived)
# ---------------------------------------------------------------------------


def make_op_predicate(
    op_id: str,
    entity_id: str,
    entity_type: str,
    op_type: str,
    lamport: int,
    actor_id: str | None,
    payload: dict,
    parent_op_ids: list[str],
) -> dict:
    """Build the predicate for an op envelope.

    This is the fine-grained provenance that agent-provenance owns.
    """
    return {
        "type": "agent-provenance-v0/op",
        "op_id": op_id,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "op_type": op_type,
        "lamport": lamport,
        "actor_id": actor_id,
        "payload_hash": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "parent_op_ids": parent_op_ids,
    }


def make_snapshot_predicate(
    snapshot_op_id: str,
    entity_id: str,
    sealed_state: dict,
    input_op_ids: list[str],
) -> dict:
    """Build the predicate for a snapshot (compaction) op.

    Meta-attestation: the snapshot is a deterministic function of its inputs.
    """
    return {
        "type": "agent-provenance-v0/snapshot",
        "op_id": snapshot_op_id,
        "entity_id": entity_id,
        "sealed_state_hash": hashlib.sha256(
            json.dumps(sealed_state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "input_op_ids": input_op_ids,
    }
