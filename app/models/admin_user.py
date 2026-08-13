"""The host's admin login."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import EncryptedString
from app.models.mixins import TimestampMixin


class AdminUser(TimestampMixin, Base):
    """An administrator who can sign in to the admin panel.

    Two secrets live on this row, protected differently because they are
    different kinds of secret:

      * `password_hash` — a one-way hash. Nothing ever needs the original
        password back, so it is never recoverable, by us or by an attacker
        holding the database.
      * `totp_secret` — **encrypted, not hashed**, and this is forced rather
        than chosen. Verifying a six-digit code means recomputing it from the
        shared secret, so the original value has to be recoverable. Hashing
        would make TOTP impossible. Reversible encryption is the only option
        available.

    ========================================================================
    Threat model for the TOTP encryption — read this before relying on it
    ========================================================================

    Encrypting `totp_secret` protects against exactly three things:

      * a **leaked database dump** — a stolen `pg_dump`, an exposed backup, a
        decommissioned disk;
      * a **leaked backup file** in object storage or on a laptop;
      * a **read-only SQL injection** that can select rows but cannot read the
        application's environment or filesystem.

    In each of those the attacker gets ciphertext and no key, so the second
    factor still holds.

    It does **NOT** protect against a compromised server. The decryption key
    lives in that server's environment, so anyone who can run code there — RCE,
    a stolen deploy credential, a malicious dependency, shell access — can read
    the key and decrypt every secret. Encryption at rest is not encryption
    against the host.

    This is a real but narrow benefit. It raises the cost of a stolen backup
    from "instant second-factor bypass" to "useless without a separate
    compromise". It does not make the database safe to leak, and it is not a
    substitute for keeping the server uncompromised.

    ========================================================================
    Key rotation: DECIDED. Single key now, MultiFernet in Phase 8 if ever.
    ========================================================================

    `app/core/encryption.py` uses one Fernet key. `MultiFernet` would let a
    list of keys decrypt while the first encrypts, making rotation possible
    without re-encrypting anything. It is deliberately not used yet.

    **Why single-key is right for now:** this table holds one row, maybe a
    handful. Rotating by hand is a loop over every row — read with the old
    key, write with the new — that finishes in milliseconds and can run
    inside one transaction. MultiFernet exists to avoid coordinating a
    rewrite across a table too large or too hot to update in one pass, and
    neither applies here. Adopting it now would add a permanently
    multi-valued key setting, and a config shape you cannot simplify later,
    to solve a problem this project does not have.

    **The rotation procedure, for when it is needed:**

      1. Generate a new key: `generate_key()`.
      2. In one transaction, for each admin: read `totp_secret` (decrypts
         under the old key), then rewrite it with the new key configured.
         In practice: read every secret with the old key, restart with the
         new key, write them back.
      3. Keep the old key until the rewrite is confirmed, then destroy it.

    Because that middle step needs both keys live at once, the honest
    trigger for adopting MultiFernet is not row count — it is wanting
    zero-downtime rotation. Revisit in Phase 8 alongside the secrets and
    backup work, or the first time a rotation is actually needed.

    **A suspected key leak is NOT a rotation problem, and this is the part
    worth remembering under pressure.** Rotating the key re-encrypts the
    same plaintext seeds. If an attacker held the key long enough to decrypt,
    they now have the seeds themselves, and every new ciphertext protects a
    secret they already know. Recovery is to **re-enrol every admin's
    authenticator app** — new TOTP secrets — and rotate the encryption key as
    hygiene alongside it. Key rotation alone would restore no security while
    looking like it had.

    ========================================================================
    Neither secret may ever leave this object
    ========================================================================

    `__repr__` below is overridden to exclude both, and a test asserts it. That
    override matters more in practice than the encryption does: secrets almost
    never leak through a stolen database, they leak through a log line. The
    default SQLAlchemy repr is harmless, but any `f"{user}"`, any exception
    rendering its arguments, any structured log capturing an object, and any
    debugger dump would otherwise carry both values into a log aggregator that
    is far less protected than the database.

    The same rule extends beyond this class: never put either field in an API
    response model, never log a row dict, and never include one in an
    exception message. Phase 5 adds the auth endpoints and Phase 8 the log
    redaction pass; this is the invariant both must preserve.
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)

    # Argon2 or bcrypt digest, produced in Phase 5. One-way: there is no code
    # path anywhere that recovers the password from this.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Base32 TOTP seed, encrypted at rest by the column type. Nullable because
    # an admin exists before enrolling an authenticator app.
    #
    # Reads and writes are transparent — the attribute is plaintext in Python —
    # so treat every access as handling a live credential.
    totp_secret: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def has_totp_enrolled(self) -> bool:
        """Whether two-factor is set up, without exposing the secret itself.

        Callers that only need to know "is 2FA on?" should use this rather
        than truthiness-testing `totp_secret`, so the secret is not pulled
        into a local variable that might end up in a traceback.
        """
        return self.totp_secret is not None

    def __repr__(self) -> str:
        """Deliberately excludes password_hash and totp_secret.

        Identifying fields only. If you add a field to this class, do not add
        it here unless you are certain it is safe in a log file.
        """
        return (
            f"<AdminUser id={self.id} email={self.email!r} "
            f"active={self.is_active} totp_enrolled={self.has_totp_enrolled}>"
        )
