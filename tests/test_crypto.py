"""Tests for agent encryption keypair management."""

import os


from aems_agent.crypto import (
    decrypt_sealed_box,
    ensure_keypair,
    get_key_id,
    load_private_key,
    load_public_key,
)


def test_ensure_keypair_creates_files(tmp_path):
    """First call creates both key files."""
    ensure_keypair(tmp_path)
    assert (tmp_path / "agent_private.key").exists()
    assert (tmp_path / "agent_public.key").exists()


def test_ensure_keypair_idempotent(tmp_path):
    """Second call returns same keys."""
    ensure_keypair(tmp_path)
    pub1 = load_public_key(tmp_path)
    ensure_keypair(tmp_path)
    pub2 = load_public_key(tmp_path)
    assert pub1 == pub2


def test_load_public_key_returns_bytes(tmp_path):
    ensure_keypair(tmp_path)
    pub = load_public_key(tmp_path)
    assert isinstance(pub, bytes)
    assert len(pub) == 32  # X25519 public key is 32 bytes


def test_get_key_id_is_stable(tmp_path):
    ensure_keypair(tmp_path)
    kid1 = get_key_id(tmp_path)
    kid2 = get_key_id(tmp_path)
    assert kid1 == kid2
    assert len(kid1) == 16  # truncated hex fingerprint


def test_key_file_permissions(tmp_path):
    """Private key file should be owner-only (on Unix)."""
    ensure_keypair(tmp_path)
    if os.name != "nt":
        stat = (tmp_path / "agent_private.key").stat()
        assert oct(stat.st_mode & 0o777) == "0o600"


def test_decrypt_sealed_box_roundtrip(tmp_path):
    """Encrypt with public key, decrypt with private key."""
    from nacl.public import PublicKey, SealedBox

    ensure_keypair(tmp_path)
    pub_bytes = load_public_key(tmp_path)
    pub = PublicKey(pub_bytes)
    box = SealedBox(pub)
    plaintext = b"secret exam token"
    ciphertext = box.encrypt(plaintext)
    result = decrypt_sealed_box(tmp_path, ciphertext)
    assert result == plaintext


def test_load_private_key_type(tmp_path):
    """load_private_key returns a NaCl PrivateKey object."""
    from nacl.public import PrivateKey

    ensure_keypair(tmp_path)
    sk = load_private_key(tmp_path)
    assert isinstance(sk, PrivateKey)


def test_ensure_keypair_creates_parent_dirs(tmp_path):
    """ensure_keypair creates intermediate directories if needed."""
    nested = tmp_path / "deep" / "nested" / "dir"
    ensure_keypair(nested)
    assert (nested / "agent_private.key").exists()
    assert (nested / "agent_public.key").exists()


def test_ensure_keypair_rederives_public_key_from_private(tmp_path):
    """Losing only the public key must not rotate the keypair.

    Regenerating would silently change the key id, breaking every payload
    the server sealed to the original key.
    """
    from aems_agent.crypto import ensure_keypair, get_key_id

    ensure_keypair(tmp_path)
    original_key_id = get_key_id(tmp_path)
    original_private = (tmp_path / "agent_private.key").read_bytes()

    (tmp_path / "agent_public.key").unlink()
    ensure_keypair(tmp_path)

    assert get_key_id(tmp_path) == original_key_id
    assert (tmp_path / "agent_private.key").read_bytes() == original_private


def test_private_key_created_owner_only(tmp_path):
    """Private key must be 0600 from the moment it exists (POSIX)."""
    import os

    if os.name == "nt":
        import pytest

        pytest.skip("POSIX permission semantics only")

    from aems_agent.crypto import ensure_keypair

    ensure_keypair(tmp_path)
    mode = (tmp_path / "agent_private.key").stat().st_mode & 0o777
    assert mode == 0o600
