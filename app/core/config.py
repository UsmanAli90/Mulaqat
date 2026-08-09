"""Application settings.

Rails equivalent: `config/application.rb` + `Rails.application.credentials`,
except the values are a validated, typed object rather than a hash. If
DATABASE_URL is missing or HOST_TIMEZONE is not a real IANA name, the process
fails at import with a clear error instead of blowing up on first use.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
