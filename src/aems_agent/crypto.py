"""
Agent encryption keypair management (X25519 + XSalsa20-Poly1305 via NaCl).

Generates and persists an X25519 keypair for encrypted token handoff.
The public key is shared with the AEMS server (via the browser) so the
server can seal Canvas tokens before they transit through the browser.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from nacl.public import PrivateKey, SealedBox

_PRIVATE_KEY_FILE = "agent_private.key"
_PUBLIC_KEY_FILE = "agent_public.key"


def ensure_keypair(config_dir: Path) -> None:
    """Generate keypair if missing. Idempotent.

    Args:
        config_dir: Directory to store key files in.
    """
    priv_path = config_dir / _PRIVATE_KEY_FILE
    pub_path = config_dir / _PUBLIC_KEY_FILE
    if priv_path.exists() and pub_path.exists():
        return

    key = PrivateKey.generate()
    config_dir.mkdir(parents=True, exist_ok=True)

    priv_path.write_bytes(bytes(key))
    pub_path.write_bytes(bytes(key.public_key))

    if os.name != "nt":
        priv_path.chmod(0o600)


def load_public_key(config_dir: Path) -> bytes:
    """Return raw 32-byte public key.

    Args:
        config_dir: Directory containing agent key files.

    Returns:
        Raw 32-byte X25519 public key.
    """
    return (config_dir / _PUBLIC_KEY_FILE).read_bytes()


def load_private_key(config_dir: Path) -> PrivateKey:
    """Return NaCl PrivateKey object.

    Args:
        config_dir: Directory containing agent key files.

    Returns:
        NaCl PrivateKey instance.
    """
    raw = (config_dir / _PRIVATE_KEY_FILE).read_bytes()
    return PrivateKey(raw)


def get_key_id(config_dir: Path) -> str:
    """Return truncated SHA-256 fingerprint of the public key (16 hex chars).

    Args:
        config_dir: Directory containing agent key files.

    Returns:
        16-character hex string identifying this agent's public key.
    """
    pub = load_public_key(config_dir)
    return hashlib.sha256(pub).hexdigest()[:16]


def decrypt_sealed_box(config_dir: Path, ciphertext: bytes) -> bytes:
    """Decrypt a NaCl SealedBox message using the agent's private key.

    Args:
        config_dir: Directory containing agent key files.
        ciphertext: Encrypted bytes from a SealedBox.

    Returns:
        Decrypted plaintext bytes.
    """
    sk = load_private_key(config_dir)
    box = SealedBox(sk)
    return box.decrypt(ciphertext)
