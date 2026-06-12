# SPDX-License-Identifier: AGPL-3.0-or-later

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


def _write_secret_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path*, created owner-only (0600) from the start.

    Avoids the write-then-chmod window where the key is briefly readable by
    other local users. Windows ignores the POSIX mode and inherits ACLs from
    the parent directory instead.

    ``O_BINARY`` is essential: on Windows ``os.open`` defaults to text mode,
    so ``os.write`` would translate 0x0A bytes in the raw key to 0x0D 0x0A,
    corrupting it (the key would no longer be 32 bytes). ``O_BINARY`` is 0 on
    POSIX, so this is a no-op there.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def ensure_keypair(config_dir: Path) -> None:
    """Generate keypair if missing. Idempotent.

    Args:
        config_dir: Directory to store key files in.
    """
    priv_path = config_dir / _PRIVATE_KEY_FILE
    pub_path = config_dir / _PUBLIC_KEY_FILE
    if priv_path.exists() and pub_path.exists():
        return

    config_dir.mkdir(parents=True, exist_ok=True)

    if priv_path.exists():
        # Private key present but public key missing: rederive instead of
        # regenerating, so payloads sealed to the existing key still decrypt
        # and the advertised key id stays stable.
        key = PrivateKey(priv_path.read_bytes())
        pub_path.write_bytes(bytes(key.public_key))
        return

    key = PrivateKey.generate()
    _write_secret_bytes(priv_path, bytes(key))
    pub_path.write_bytes(bytes(key.public_key))


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
