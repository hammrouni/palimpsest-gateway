"""Fernet-based encrypted-at-rest helper for operator + provider credentials."""
import os

from cryptography.fernet import Fernet, InvalidToken


class SecretsManager:
    """Fernet-based symmetric encryption for operator & provider keys."""

    def __init__(self, master_key: str | None = None):
        """Initialize with Fernet master key.

        Args:
            master_key: Fernet key (base64, 44 chars). If None, read from PALIMPSEST_GATE_MASTER_KEY env.

        Raises:
            ValueError: If no key is provided and env is not set.
        """
        if master_key is None:
            master_key = os.environ.get("PALIMPSEST_GATE_MASTER_KEY")
            if not master_key:
                raise ValueError(
                    "PALIMPSEST_GATE_MASTER_KEY not set; cannot initialize SecretsManager"
                )

        self.master_key = master_key
        self.cipher = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)

    def encrypt(self, raw_secret: str) -> str:
        """Encrypt a raw secret (raw key, provider key, etc.).

        Args:
            raw_secret: Raw plaintext secret

        Returns:
            Fernet ciphertext (base64 string)
        """
        ciphertext = self.cipher.encrypt(raw_secret.encode("utf-8"))
        return ciphertext.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet ciphertext, with no expiry check (``ttl=None``).

        Raises:
            ValueError: If the ciphertext is invalid or was made with another key
        """
        if isinstance(ciphertext, str):
            ciphertext = ciphertext.encode("utf-8")

        try:
            plaintext = self.cipher.decrypt(ciphertext, ttl=None)
            return plaintext.decode("utf-8")
        except InvalidToken as e:
            raise ValueError(f"Failed to decrypt secret: {e}") from e


# Global singleton (initialized with env at startup)
_secrets_manager: SecretsManager | None = None


def get_secrets_manager() -> SecretsManager:
    """Get the global SecretsManager (lazy-initialized from env)."""
    global _secrets_manager
    if _secrets_manager is None:
        _secrets_manager = SecretsManager()
    return _secrets_manager


def encrypt_secret(raw_secret: str) -> str:
    """Convenience: encrypt using the global manager."""
    return get_secrets_manager().encrypt(raw_secret)


def decrypt_secret(ciphertext: str) -> str:
    """Convenience: decrypt using the global manager."""
    return get_secrets_manager().decrypt(ciphertext)
