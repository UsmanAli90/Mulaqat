"""Seed a database with the host's starting data.

    uv run python -m app.seed

Creates three services, one weekly availability rule, and the settings row.

**No admin user.** That is deliberate, not an omission. Seeding one would mean
either committing a password hash to a public repository, or relaxing
`admin_users.password_hash` to nullable so a credential-less row could exist
temporarily. The second is worse than it looks: dropping a NOT NULL to allow a
bootstrap state weakens the schema *permanently*, and the database would go on
accepting an admin with no credentials forever. The pattern throughout this
project is that the database enforces what must be true rather than trusting
the application to be careful, and this would be the one place that stopped
being so. Phase 5 adds a CLI command that prompts for a password and creates
the admin in one step, so there is never a moment when an admin exists without
credentials.

**Idempotency semantics: insert what is missing, never update what exists.**

Running twice is safe, and the second run changes nothing. Note what that
deliberately does *not* mean: it does not converge the database back to the
values below. If the host has raised a price or edited an availability window,
re-running leaves those edits alone. The alternative — upserting — would make
this script silently revert real configuration, which is a far worse failure
than it not being applied. Existing rows are matched on their natural key:
`slug` for services, the weekday plus window for the availability rule, and
the fixed primary key for settings.
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory, engine
from app.models import AvailabilityRule, Service, Settings
from app.models.settings import SETTINGS_ID

# Monday 20:00-23:00 host-local, matching the spec's worked example.
# Minutes from midnight; see app/core/wall_clock.py.
DEFAULT_RULE_DAY_OF_WEEK = 0
DEFAULT_RULE_START_MINUTE = 20 * 60
DEFAULT_RULE_END_MINUTE = 23 * 60

SERVICES: list[dict[str, object]] = [
    {
        "name": "Quick Question",
        "slug": "quick-question",
        "description": "A focused 15 minutes on one specific problem.",
        "duration_minutes": 15,
        "price_pkr": Decimal("2000.00"),
        "price_usd": Decimal("15.00"),
        "sort_order": 1,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
        "intake_questions": [
            {
                "key": "question",
                "label": "What is your question?",
                "type": "long_text",
                "required": True,
            }
        ],
    },
    {
        "name": "Consultation",
        "slug": "consultation",
        "description": "Half an hour to talk through a problem in context.",
        "duration_minutes": 30,
        "price_pkr": Decimal("5000.00"),
        "price_usd": Decimal("30.00"),
        "sort_order": 2,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 5,
        "intake_questions": [
            {
                "key": "topic",
                "label": "What would you like to cover?",
                "type": "long_text",
                "required": True,
            },
            {
                "key": "experience",
                "label": "How much experience do you have?",
                "type": "single_select",
                "options": ["Student", "0-2 years", "2-5 years", "5+ years"],
                "required": False,
            },
        ],
    },
    {
        "name": "Deep Dive",
        "slug": "deep-dive",
        "description": "A full hour for architecture review or career planning.",
        "duration_minutes": 60,
        "price_pkr": Decimal("9000.00"),
        "price_usd": Decimal("55.00"),
        "sort_order": 3,
        "buffer_before_minutes": 5,
        # A longer call earns a real gap afterwards. Also makes the seeded data
        # exercise blocked_range rather than leaving every footprint equal to
        # its meeting.
        "buffer_after_minutes": 15,
        "intake_questions": [
            {
                "key": "goal",
                "label": "What do you want to walk away with?",
                "type": "long_text",
                "required": True,
            },
            {
                "key": "context_link",
                "label": "Anything to read beforehand? (repo, doc, CV)",
                "type": "short_text",
                "required": False,
                "help_text": "Optional, but it makes the hour go further.",
            },
        ],
    },
]


@dataclass
class SeedReport:
    """What a run actually did, so the caller can print it or assert on it."""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created)

    def render(self) -> str:
        lines = [f"created: {item}" for item in self.created]
        lines += [f"exists:  {item}" for item in self.skipped]
        lines.append("")
        lines.append(
            f"{len(self.created)} created, {len(self.skipped)} already present."
            if self.created
            else "Nothing to do — the database is already seeded."
        )
        return "\n".join(lines)


async def seed(session: AsyncSession) -> SeedReport:
    """Insert missing seed data. Takes a session so tests can run it in a rollback.

    Does not commit: the caller decides, which lets the test suite run this
    inside its transaction and lets `main()` commit once at the end. Every
    write goes through the ORM, so the validated `intake_questions` column type
    and the model-level validators apply here exactly as they do in a request.
    """
    report = SeedReport()

    for spec in SERVICES:
        slug = str(spec["slug"])
        existing = await session.scalar(select(Service).where(Service.slug == slug))
        if existing is not None:
            report.skipped.append(f"service {slug}")
            continue
        session.add(Service(**spec))
        report.created.append(f"service {slug}")

    # Matched on the whole window, not just the weekday: a host who has added a
    # second Monday window should not stop this from restoring the default one,
    # and re-running must not duplicate it.
    rule = await session.scalar(
        select(AvailabilityRule).where(
            AvailabilityRule.day_of_week == DEFAULT_RULE_DAY_OF_WEEK,
            AvailabilityRule.start_minute == DEFAULT_RULE_START_MINUTE,
            AvailabilityRule.end_minute == DEFAULT_RULE_END_MINUTE,
        )
    )
    if rule is None:
        session.add(
            AvailabilityRule(
                day_of_week=DEFAULT_RULE_DAY_OF_WEEK,
                start_minute=DEFAULT_RULE_START_MINUTE,
                end_minute=DEFAULT_RULE_END_MINUTE,
                is_active=True,
            )
        )
        report.created.append("availability rule Monday 20:00-23:00")
    else:
        report.skipped.append("availability rule Monday 20:00-23:00")

    # The singleton. Every column has a server default, so an id-only insert
    # produces the intended row and keeps the defaults in one place — the
    # schema — rather than duplicating them here where they could drift.
    settings = await session.get(Settings, SETTINGS_ID)
    if settings is None:
        session.add(Settings(id=SETTINGS_ID))
        report.created.append("settings row")
    else:
        report.skipped.append("settings row")

    await session.flush()
    return report


async def _run() -> SeedReport:
    # The shared engine echoes SQL when DEBUG=true locally, which is useful in
    # a request log and useless here — it buries the report under sixty lines
    # of INSERT. Quietened for this process only; the web app is unaffected.
    engine.echo = False

    async with SessionFactory() as session:
        report = await seed(session)
        await session.commit()
    await engine.dispose()
    return report


def main() -> None:
    """Entry point for `python -m app.seed`."""
    report = asyncio.run(_run())
    print(report.render())


if __name__ == "__main__":
    main()
