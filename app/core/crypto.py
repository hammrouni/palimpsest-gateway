"""Cryptographic utilities for API key generation and verification."""
import hashlib
import secrets


def generate_key() -> str:
    """Generate a raw API key (user realm).

    Format: pgw_live_<random>
    Returns exactly once in creation response; never retrieved after.

    Returns:
        Raw API key (not hashed)
    """
    random_part = secrets.token_urlsafe(32)
    return f"pgw_live_{random_part}"


def generate_operator_key() -> str:
    """Generate a raw operator API key (operator realm).

    Format: pgw_opk_<random> — distinct from user-realm ``pgw_live_`` keys so the
    two realms stay visually distinguishable.
    Returns exactly once in creation response; never retrieved after.

    Returns:
        Raw operator API key (not hashed)
    """
    random_part = secrets.token_urlsafe(32)
    return f"pgw_opk_{random_part}"


def hash_key(raw_key: str) -> str:
    """Hash a raw API key using SHA-256 (hex).

    Used for O(1) lookup in Redis: pgw:user:api_key:by_hash:{key_hash} -> key_id
    Never compare raw secrets; always use hash-based lookup.

    Args:
        raw_key: Raw API key string

    Returns:
        SHA-256 hex digest
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def prefix_of(raw_key: str, length: int = 12) -> str:
    """Extract short prefix for key display (human-readable).

    Stored with the key record for listing; safe to show to users.

    Args:
        raw_key: Raw API key
        length: Prefix length (default 12)

    Returns:
        Short prefix (e.g., "pgw_live_a1b" for the default length of 12)
    """
    return raw_key[:length]
