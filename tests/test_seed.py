"""Tests for the seed script.

Two properties matter and they pull in different directions:

  * running it on an empty database produces the intended starting state;
  * running it again changes nothing — **including not reverting edits the
    host has made since**, which is the failure mode an upserting seed script
    would have.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminUser, AvailabilityRule, Service, Settings
from app.models.settings import SETTINGS_ID
from app.schemas.intake import IntakeQuestion
from app.seed import (
    DEFAULT_RULE_DAY_OF_WEEK,
    DEFAULT_RULE_END_MINUTE,
    DEFAULT_RULE_START_MINUTE,
    SERVICES,
    seed,
)


async def test_seeding_an_empty_database_creates_everything(session: AsyncSession) -> None:
    report = await seed(session)

    services = (await session.scalars(select(Service).order_by(Service.sort_order))).all()
    assert [s.slug for s in services] == ["quick-question", "consultation", "deep-dive"]
    assert [s.duration_minutes for s in services] == [15, 30, 60]

    rules = (await session.scalars(select(AvailabilityRule))).all()
    assert len(rules) == 1
    assert rules[0].day_of_week == DEFAULT_RULE_DAY_OF_WEEK
    assert rules[0].start_minute == DEFAULT_RULE_START_MINUTE
    assert rules[0].end_minute == DEFAULT_RULE_END_MINUTE

    settings = await session.get(Settings, SETTINGS_ID)
    assert settings is not None

    assert report.changed is True
    assert len(report.created) == 5
    assert report.skipped == []


async def test_seeding_twice_creates_no_duplicates(session: AsyncSession) -> None:
    """The headline requirement: safe to run twice."""
    await seed(session)
    second = await seed(session)

    counts = {
        "services": await session.scalar(select(func.count()).select_from(Service)),
        "rules": await session.scalar(select(func.count()).select_from(AvailabilityRule)),
        "settings": await session.scalar(select(func.count()).select_from(Settings)),
    }

    assert counts == {"services": 3, "rules": 1, "settings": 1}
    assert second.changed is False
    assert second.created == []
    assert len(second.skipped) == 5


async def test_seeding_three_times_is_still_stable(session: AsyncSession) -> None:
    """Guards against a second run creating state that a third would duplicate."""
    await seed(session)
    await seed(session)
    await seed(session)

    assert await session.scalar(select(func.count()).select_from(Service)) == 3
    assert await session.scalar(select(func.count()).select_from(AvailabilityRule)) == 1


async def test_re_running_does_not_revert_host_edits(session: AsyncSession) -> None:
    """**The reason this inserts rather than upserts.**

    A host who has raised a price and disabled a service must not have that
    quietly undone by re-running the seed. An upserting script would look
    equally "idempotent" by row count while destroying real configuration.
    """
    await seed(session)
    consultation = await session.scalar(select(Service).where(Service.slug == "consultation"))
    assert consultation is not None
    consultation.price_pkr = Decimal("7500.00")
    consultation.is_active = False
    await session.flush()

    await seed(session)
    await session.refresh(consultation)

    assert consultation.price_pkr == Decimal("7500.00")
    assert consultation.is_active is False


async def test_re_running_does_not_revert_settings_edits(session: AsyncSession) -> None:
    await seed(session)
    settings = await session.get(Settings, SETTINGS_ID)
    assert settings is not None
    settings.min_notice_hours = 48
    settings.host_timezone = "America/New_York"
    await session.flush()

    await seed(session)
    await session.refresh(settings)

    assert settings.min_notice_hours == 48
    assert settings.host_timezone == "America/New_York"


async def test_a_deleted_service_is_restored(session: AsyncSession) -> None:
    """Insert-what-is-missing cuts both ways, and that is the useful half:
    the seed can repair a partially-set-up database."""
    await seed(session)
    await session.execute(text("DELETE FROM services WHERE slug = 'deep-dive'"))
    await session.flush()

    report = await seed(session)

    assert await session.scalar(select(func.count()).select_from(Service)) == 3
    assert report.created == ["service deep-dive"]


async def test_an_extra_host_created_rule_is_left_alone(session: AsyncSession) -> None:
    """Seeding must not touch availability the host added themselves."""
    await seed(session)
    session.add(AvailabilityRule(day_of_week=2, start_minute=9 * 60, end_minute=12 * 60))
    await session.flush()

    await seed(session)

    assert await session.scalar(select(func.count()).select_from(AvailabilityRule)) == 2


# ---------------------------------------------------------------------------
# No admin user — a deliberate absence
# ---------------------------------------------------------------------------


async def test_seed_creates_no_admin_user(session: AsyncSession) -> None:
    """Seeding an admin would mean committing a credential to a public repo,
    or relaxing password_hash to nullable — which would weaken the schema
    permanently to allow a temporary bootstrap state. Phase 5 creates the
    admin through a CLI command that prompts for a password."""
    await seed(session)

    assert await session.scalar(select(func.count()).select_from(AdminUser)) == 0


async def test_password_hash_is_still_not_nullable(session: AsyncSession) -> None:
    """The constraint that decision was made to protect.

    If this ever becomes nullable, the database will accept a credential-less
    admin forever, and it will have been done to serve a bootstrap convenience
    that no longer exists.
    """
    result = await session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'admin_users' AND column_name = 'password_hash'"
        )
    )

    assert result.scalar_one() == "NO"


# ---------------------------------------------------------------------------
# The seeded data must itself be valid
# ---------------------------------------------------------------------------


async def test_seeded_services_have_valid_intake_questions(session: AsyncSession) -> None:
    """Written through the validating column type, so they come back as models.

    A malformed question in SERVICES would fail the flush rather than land in
    the database, but asserting the shape here makes the failure legible.
    """
    await seed(session)

    services = (await session.scalars(select(Service))).all()
    all_questions = [q for service in services for q in service.intake_questions]

    assert all_questions
    assert all(isinstance(q, IntakeQuestion) for q in all_questions)
    # Every select question carries its options; the schema enforces it, and a
    # seed that violated it could never have been written.
    for question in all_questions:
        if question.type.endswith("select"):
            assert question.options


async def test_seeded_services_cover_the_specified_durations() -> None:
    """15/30/60, per the spec. Asserted on the constant so a typo is caught
    without a database."""
    assert sorted(int(s["duration_minutes"]) for s in SERVICES) == [15, 30, 60]  # type: ignore[call-overload]


async def test_seeded_services_are_priced_in_both_currencies() -> None:
    """Both providers work out of the box.

    A PKR-only or USD-only service is legal, but a *seeded* one that could not
    be booked by half the intended audience would be a poor default — and
    Phase 4 must not offer a currency a service is not priced in.
    """
    for spec in SERVICES:
        assert spec["price_pkr"] is not None, spec["slug"]
        assert spec["price_usd"] is not None, spec["slug"]


async def test_seeded_slugs_are_unique() -> None:
    slugs = [s["slug"] for s in SERVICES]

    assert len(slugs) == len(set(slugs))


async def test_seeded_settings_use_the_schema_defaults(session: AsyncSession) -> None:
    """The seed inserts id only, so defaults live in one place — the schema."""
    await seed(session)

    settings = await session.get(Settings, SETTINGS_ID)

    assert settings is not None
    assert settings.host_timezone == "Asia/Karachi"
    assert settings.min_notice_hours == 12
    assert settings.max_bookings_per_day == 5
    assert settings.slot_granularity_minutes == 15
    assert settings.cancellation_cutoff_hours == 24


async def test_seeded_deep_dive_has_buffers(session: AsyncSession) -> None:
    """So the seeded data exercises blocked_range rather than leaving every
    footprint equal to its meeting."""
    await seed(session)

    service = await session.scalar(select(Service).where(Service.slug == "deep-dive"))

    assert service is not None
    assert service.buffer_after_minutes == 15


async def test_report_renders_both_outcomes(session: AsyncSession) -> None:
    first = await seed(session)
    second = await seed(session)

    assert "created:" in first.render()
    assert "already seeded" in second.render()


@pytest.mark.parametrize("spec", SERVICES, ids=lambda s: str(s["slug"]))
async def test_each_seeded_service_satisfies_the_schema(
    session: AsyncSession, spec: dict[str, object]
) -> None:
    """Each service inserted alone, so a constraint violation names the service
    that caused it instead of failing the whole batch anonymously."""
    session.add(Service(**spec))

    await session.flush()
