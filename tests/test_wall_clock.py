"""Tests for minutes-from-midnight conversion and the API boundary.

Two layers:

  * `app/core/wall_clock.py` — the pure conversion functions.
  * `app/schemas/availability.py` — the Pydantic annotation that keeps "22:00"
    on the API side and integers on the storage side.
"""

import pytest
from pydantic import ValidationError

from app.core.wall_clock import MINUTES_PER_DAY, hhmm_to_minutes, minutes_to_hhmm
from app.models import DateOverrideType
from app.schemas.availability import AvailabilityRuleIn, DateOverrideIn

CONVERSIONS = [
    ("00:00", 0),
    ("00:01", 1),
    ("09:00", 540),
    ("09:30", 570),
    ("12:00", 720),
    ("12:15", 735),
    ("20:00", 1200),
    ("22:00", 1320),
    ("23:59", 1439),
    ("24:00", MINUTES_PER_DAY),
]


@pytest.mark.parametrize(("text_value", "minutes"), CONVERSIONS)
def test_hhmm_to_minutes(text_value: str, minutes: int) -> None:
    assert hhmm_to_minutes(text_value) == minutes


@pytest.mark.parametrize(("text_value", "minutes"), CONVERSIONS)
def test_minutes_to_hhmm(text_value: str, minutes: int) -> None:
    assert minutes_to_hhmm(minutes) == text_value


@pytest.mark.parametrize(("text_value", "minutes"), CONVERSIONS)
def test_conversion_round_trips(text_value: str, minutes: int) -> None:
    """Both directions compose back to the identity, which is the real property."""
    assert hhmm_to_minutes(minutes_to_hhmm(minutes)) == minutes
    assert minutes_to_hhmm(hhmm_to_minutes(text_value)) == text_value


def test_single_digit_hour_is_accepted() -> None:
    """Tolerated on input so "9:00" from a hand-written payload works."""
    assert hhmm_to_minutes("9:00") == 540


@pytest.mark.parametrize(
    "bad",
    ["25:00", "24:01", "99:99", "22", "22:0", "22:60", "", "abc", "-1:00", "22:00:00"],
)
def test_hhmm_to_minutes_rejects_nonsense(bad: str) -> None:
    with pytest.raises(ValueError):
        hhmm_to_minutes(bad)


@pytest.mark.parametrize("bad", [-1, MINUTES_PER_DAY + 1, 99999])
def test_minutes_to_hhmm_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError):
        minutes_to_hhmm(bad)


def test_midnight_end_is_representable() -> None:
    """The whole reason for the change: 24:00 is a real value, not 23:59:59.

    Under TIME columns a window ending at midnight had to be stored as
    23:59:59, and the missing second cost the last slot of the window.
    """
    assert hhmm_to_minutes("24:00") == 1440
    assert minutes_to_hhmm(1440) == "24:00"


# ---------------------------------------------------------------------------
# API boundary
# ---------------------------------------------------------------------------


def test_rule_schema_accepts_hhmm_strings() -> None:
    """A JSON body speaks "22:00"; the model holds integers."""
    rule = AvailabilityRuleIn.model_validate(
        {"day_of_week": 0, "start_minute": "22:00", "end_minute": "24:00"}
    )

    assert rule.start_minute == 1320
    assert rule.end_minute == 1440


def test_rule_schema_accepts_bare_integers() -> None:
    """Keeps the seed script and tests readable without a conversion dance."""
    rule = AvailabilityRuleIn.model_validate(
        {"day_of_week": 0, "start_minute": 1320, "end_minute": 1440}
    )

    assert rule.start_minute == 1320


def test_rule_schema_serialises_back_to_hhmm() -> None:
    """Responses must never leak the storage format."""
    rule = AvailabilityRuleIn.model_validate(
        {"day_of_week": 0, "start_minute": "20:00", "end_minute": "23:00"}
    )

    dumped = rule.model_dump(mode="json")

    assert dumped["start_minute"] == "20:00"
    assert dumped["end_minute"] == "23:00"


def test_rule_schema_rejects_end_before_start() -> None:
    """Mirrors the database CHECK so the API answers 422, not 500."""
    with pytest.raises(ValidationError, match="must be after"):
        AvailabilityRuleIn.model_validate(
            {"day_of_week": 0, "start_minute": "23:00", "end_minute": "20:00"}
        )


def test_rule_schema_rejects_overnight_window_with_a_useful_message() -> None:
    """The error should tell the host what to do instead."""
    with pytest.raises(ValidationError, match="two rules"):
        AvailabilityRuleIn.model_validate(
            {"day_of_week": 0, "start_minute": "22:00", "end_minute": "02:00"}
        )


def test_rule_schema_rejects_out_of_range_time() -> None:
    with pytest.raises(ValidationError):
        AvailabilityRuleIn.model_validate(
            {"day_of_week": 0, "start_minute": "22:00", "end_minute": "25:00"}
        )


def test_rule_schema_rejects_bad_day_of_week() -> None:
    with pytest.raises(ValidationError):
        AvailabilityRuleIn.model_validate(
            {"day_of_week": 7, "start_minute": "20:00", "end_minute": "23:00"}
        )


def test_override_schema_rejects_hours_on_a_blocked_date() -> None:
    with pytest.raises(ValidationError, match="must not carry hours"):
        DateOverrideIn.model_validate(
            {"date": "2026-12-25", "type": DateOverrideType.BLOCKED, "start_minute": "09:00"}
        )


def test_override_schema_requires_hours_for_custom_hours() -> None:
    with pytest.raises(ValidationError, match="requires both"):
        DateOverrideIn.model_validate({"date": "2026-06-01", "type": DateOverrideType.CUSTOM_HOURS})


def test_override_schema_accepts_a_valid_custom_hours_payload() -> None:
    override = DateOverrideIn.model_validate(
        {
            "date": "2026-06-01",
            "type": "custom_hours",
            "start_minute": "09:00",
            "end_minute": "17:00",
        }
    )

    assert override.start_minute == 540
    assert override.end_minute == 1020
