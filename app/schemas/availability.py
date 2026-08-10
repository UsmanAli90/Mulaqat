"""API-boundary schemas for availability.

The storage layer speaks integers (minutes from midnight); the API speaks
"22:00". This module is the only place the two meet, so no route handler and
no frontend ever has to know minutes exist.

`WallClockMinutes` does both directions:

  * **in** — `BeforeValidator` runs before the int validation, so a JSON body
    carrying `"22:00"` is converted to 1320 and then range-checked. Passing an
    integer straight through also works, which keeps the seed script and tests
    readable.
  * **out** — `PlainSerializer` renders the integer back to `"22:00"` whenever
    the model is dumped to JSON, so responses never leak the storage format.

That pairing is the Pydantic v2 equivalent of a Rails attribute with a custom
type plus a presenter, expressed once as a reusable annotation.
"""

from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator
from pydantic.functional_validators import BeforeValidator

from app.core.wall_clock import MINUTES_PER_DAY, hhmm_to_minutes, minutes_to_hhmm
from app.models.availability import DateOverrideType


def _coerce_to_minutes(value: Any) -> Any:
    """Accept "HH:MM" or a bare int; leave anything else for Pydantic to reject."""
    if isinstance(value, str):
        return hhmm_to_minutes(value)
    return value


WallClockMinutes = Annotated[
    int,
    BeforeValidator(_coerce_to_minutes),
    Field(ge=0, le=MINUTES_PER_DAY, description='Wall-clock time, e.g. "22:00". Host-local.'),
    PlainSerializer(minutes_to_hhmm, return_type=str, when_used="json"),
]


class AvailabilityRuleIn(BaseModel):
    """A weekly availability window as submitted by the admin panel."""

    model_config = ConfigDict(extra="forbid")

    day_of_week: int = Field(ge=0, le=6, description="0 = Monday, 6 = Sunday")
    start_minute: WallClockMinutes
    end_minute: WallClockMinutes
    is_active: bool = True

    @model_validator(mode="after")
    def check_end_after_start(self) -> Self:
        """Mirrors the database CHECK so the API returns 422, not a 500.

        The constraint in the database is the real guarantee; this exists so a
        bad payload is rejected with a readable field error instead of
        surfacing as an IntegrityError from the driver.
        """
        if self.end_minute <= self.start_minute:
            raise ValueError(
                f"end ({minutes_to_hhmm(self.end_minute)}) must be after "
                f"start ({minutes_to_hhmm(self.start_minute)}). A window crossing midnight "
                "is entered as two rules, one per day."
            )
        return self


class DateOverrideIn(BaseModel):
    """A per-date deviation as submitted by the admin panel."""

    model_config = ConfigDict(extra="forbid")

    date: Any
    type: DateOverrideType
    start_minute: WallClockMinutes | None = None
    end_minute: WallClockMinutes | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_hours_match_type(self) -> Self:
        """Mirrors ck_date_overrides_hours_match_type."""
        if self.type is DateOverrideType.BLOCKED:
            if self.start_minute is not None or self.end_minute is not None:
                raise ValueError("a blocked date must not carry hours")
        else:
            if self.start_minute is None or self.end_minute is None:
                raise ValueError("custom_hours requires both a start and an end")
            if self.end_minute <= self.start_minute:
                raise ValueError("end must be after start")
        return self
