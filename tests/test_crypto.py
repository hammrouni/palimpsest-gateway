"""Tests for crypto utilities."""

from app.core.crypto import generate_key, hash_key, prefix_of
from app.core.ids import new_id


def test_generate_key_format():
    """Generated key starts with pgw_live_ prefix."""
    key = generate_key()
    assert key.startswith("pgw_live_")
    assert len(key) > len("pgw_live_")


def test_generate_key_unique():
    """Each generated key is unique."""
    keys = [generate_key() for _ in range(10)]
    assert len(set(keys)) == 10


def test_hash_key_hex():
    """Hashed key is hex string (SHA-256 = 64 hex chars)."""
    key = generate_key()
    hashed = hash_key(key)
    assert len(hashed) == 64
    assert all(c in "0123456789abcdef" for c in hashed)


def test_hash_key_deterministic():
    """Hash of same key is always identical."""
    key = generate_key()
    h1 = hash_key(key)
    h2 = hash_key(key)
    assert h1 == h2


def test_hash_key_different():
    """Different keys produce different hashes."""
    key1 = generate_key()
    key2 = generate_key()
    assert hash_key(key1) != hash_key(key2)


def test_prefix_of_default():
    """Prefix extracts first 12 chars by default."""
    key = "pgw_live_1234567890abcdefghij"
    assert prefix_of(key) == "pgw_live_123"


def test_prefix_of_custom_length():
    """Prefix respects custom length."""
    key = "pgw_live_1234567890abcdefghij"
    assert prefix_of(key, 8) == "pgw_live"
    assert prefix_of(key, 20) == "pgw_live_1234567890a"


def test_new_id_format():
    """Generated ID is a string UUID."""
    id_val = new_id()
    assert isinstance(id_val, str)
    assert len(id_val) == 36
    assert id_val.count("-") == 4


def test_new_id_unique():
    """Each generated ID is unique."""
    ids = [new_id() for _ in range(10)]
    assert len(set(ids)) == 10
