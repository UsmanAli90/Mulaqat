"""Application settings.

Rails equivalent: `config/application.rb` + `Rails.application.credentials`,
except the values are a validated, typed object rather than a hash. If
DATABASE_URL is missing or HOST_TIMEZONE is not a real IANA name, the process
fails at import with a clear error instead of blowing up on first use.
"""

from functools import lru_cache
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The key published in .env.example. Fine for local development, catastrophic
# in production, so `Settings` refuses to boot production with it.
EXAMPLE_TOTP_KEY = "tf8oL6aW8GfdvPXWW8N8362knn3S4TxiW4qY6SE9L6c="


class Settings(BaseSettings):
    """Typed environment configuration.

    BaseSettings is Pydantic's env-var-backed model: each field is populated
    from the matching environment variable (case-insensitive) or from .env,
    then validated with the same machinery as any other Pydantic model.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Fail loudly if .env contains a variable this class does not declare.
        # Usually that means a typo, which would otherwise silently do nothing.
        extra="forbid",
    )

    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False

    database_url: PostgresDsn
    redis_url: str = "redis://localhost:6380/0"

    # Fallback host timezone. From Phase 2 the `settings` table row is the
    # source of truth; this covers the window before that row exists.
    host_timezone: str = Field(default="Asia/Karachi")

    # Fernet key encrypting admin_users.totp_secret at rest. Required, with no
    # default: a silently-generated key would encrypt data that nothing could
    # ever decrypt again after a restart.
    totp_encryption_key: str

    @field_validator("totp_encryption_key")
    @classmethod
    def validate_fernet_key(cls, value: str) -> str:
        """Reject a key Fernet cannot use, at startup rather than at first login."""
        # Imported here rather than at module scope: app.core.encryption imports
        # this module for get_settings(), so a top-level import would be a cycle.
        from cryptography.fernet import Fernet

        try:
            Fernet(value.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "totp_encryption_key must be a valid Fernet key (44 url-safe base64 "
                "characters). Generate one with: uv run python -c "
                '"from app.core.encryption import generate_key; print(generate_key())"'
            ) from exc
        return value

    @field_validator("host_timezone")
    @classmethod
    def validate_iana_timezone(cls, value: str) -> str:
        """Reject anything zoneinfo cannot resolve.

        A field_validator is Pydantic's per-field hook — roughly an
        ActiveModel validation, but it runs during construction and can also
        transform the value, so an invalid config can never be instantiated.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"host_timezone must be a valid IANA timezone name (got {value!r}). "
                "Use a name like 'Asia/Karachi', never a UTC offset."
            ) from exc
        return value

    @model_validator(mode="after")
    def reject_example_key_in_production(self) -> Self:
        """Refuse to boot production with the key committed to .env.example.

        That key is public — it is in the repository — so anything encrypted
        under it is effectively plaintext. A `cp .env.example .env` that made
        it to a server would otherwise be silent and total.
        """
        if self.environment == "production" and self.totp_encryption_key == EXAMPLE_TOTP_KEY:
            raise ValueError(
                "totp_encryption_key is still the example key from .env.example, which is "
                "public. Generate a real one before deploying."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings, constructing it once.

    Cached because reading .env and validating on every request would be
    wasteful, and because tests can call `get_settings.cache_clear()` to pick
    up a patched environment.
    """
    # mypy cannot see that BaseSettings populates required fields from the
    # environment, so it reads this no-arg call as missing arguments.
    return Settings()  # type: ignore[call-arg]
