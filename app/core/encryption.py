"""Symmetric encryption for secrets that must be recoverable.

Almost every secret in a web app should be *hashed*, not encrypted — a
password hash is one-way on purpose, and nothing ever needs the original
back. A TOTP secret is the exception: verifying a six-digit code means
recomputing it from the shared secret, so the original value has to be
recoverable and hashing is not an option. Reversible encryption is the only
choice available, not a preference.

Fernet is used because it is authenticated (AES-128-CBC plus HMAC), so a
tampered ciphertext fails loudly rather than decrypting to garbage, and
because it removes every opportunity to choose a mode or an IV badly.

Note Fernet is **non-deterministic**: encrypting the same plaintext twice
produces different ciphertexts, since each token carries a random IV and a
timestamp. That is the correct property here, and it means an encrypted
column can never be searched by value or given a useful unique index.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

__all__ = ["InvalidToken", "decrypt", "encrypt", "generate_key"]


def generate_key() -> str:
    """Mint a new Fernet key. Used by operators, not by application code.

    uv run python -c "from app.core.encryption import generate_key; print(generate_key())"
    """
    return Fernet.generate_key().decode()


@lru_cache
def _cipher() -> Fernet:
    """The process-wide cipher.

    Cached because constructing a Fernet derives key material, and doing that
    on every attribute access would be wasteful. Tests that patch the key can
    call `_cipher.cache_clear()`.
    """
    return Fernet(get_settings().totp_encryption_key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt to a URL-safe token."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by `encrypt`.

    Raises `InvalidToken` if the ciphertext was tampered with, truncated, or
    was encrypted under a different key. That last case is what a lost or
    rotated key looks like, and it is unrecoverable — see the warning in
    `.env.example`.
    """
    return _cipher().decrypt(token.encode()).decode()
