"""Tests for admin_users, processed_webhook_events and notification_log."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import encryption
from app.core.config import EXAMPLE_TOTP_KEY, Settings, get_settings
from app.models import (
    AdminUser,
    NotificationLog,
    NotificationStatus,
    NotificationType,
    ProcessedWebhookEvent,
)
from tests.factories import (
    build_admin_user,
    build_booking,
    build_notification_log,
    build_service,
    build_webhook_event,
)

# A realistic base32 TOTP seed. Not a real enrolment.
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aVeryDistinctiveDigestValue"


@asynccontextmanager
async def expect_violation(session: AsyncSession, constraint: str) -> AsyncIterator[None]:
    """SAVEPOINT, assert IntegrityError names `constraint`. As elsewhere."""
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            yield
    finally:
        await savepoint.rollback()
    message = str(exc_info.value.orig)
    assert constraint in message, f"expected {constraint!r} to be violated, got: {message}"


# ---------------------------------------------------------------------------
# Secrets must never leave the object
#
# This is the section that matters most. Encryption at rest defends against a
# stolen database; these tests defend against the way secrets actually escape,
# which is a log line.
# ---------------------------------------------------------------------------


def test_repr_never_contains_either_secret() -> None:
    """The single most important test in this file.

    Any f-string, any exception rendering its arguments, any structured logger
    capturing an object, any debugger dump goes through __repr__. If either
    secret appeared there it would end up in a log aggregator that is far less
    protected than the database the encryption is guarding.
    """
    user = build_admin_user(password_hash=PASSWORD_HASH, totp_secret=TOTP_SECRET)

    rendered = repr(user)

    assert PASSWORD_HASH not in rendered
    assert TOTP_SECRET not in rendered
    # Not even a fragment: a truncated hash is still a leaked hash.
    assert "argon2" not in rendered
    assert TOTP_SECRET[:8] not in rendered


def test_repr_still_identifies_the_row() -> None:
    """Excluding secrets must not make the repr useless for debugging."""
    user = build_admin_user(email="host@example.com")

    rendered = repr(user)

    assert "host@example.com" in rendered
    assert "AdminUser" in rendered


def test_str_and_format_also_hide_the_secrets() -> None:
    """str() and f-strings fall back to __repr__, but assert it rather than
    assume it — a future __str__ would silently reopen the hole."""
    user = build_admin_user(password_hash=PASSWORD_HASH, totp_secret=TOTP_SECRET)

    for rendered in (str(user), f"{user}", "{}".format(user)):  # noqa: UP032
        assert PASSWORD_HASH not in rendered
        assert TOTP_SECRET not in rendered


def test_secrets_absent_from_repr_of_a_containing_structure() -> None:
    """Logging a list or dict of users renders each via repr."""
    user = build_admin_user(password_hash=PASSWORD_HASH, totp_secret=TOTP_SECRET)

    rendered = repr({"users": [user]})

    assert PASSWORD_HASH not in rendered
    assert TOTP_SECRET not in rendered


async def test_secrets_absent_from_repr_after_loading_from_the_database(
    session: AsyncSession,
) -> None:
    """A persisted, freshly-loaded instance behaves the same.

    Worth asserting separately: the object under test above was never through
    the ORM round trip, and it is the loaded instance that gets logged in
    production.
    """
    session.add(build_admin_user(password_hash=PASSWORD_HASH, totp_secret=TOTP_SECRET))
    await session.flush()
    session.expunge_all()

    loaded = (await session.execute(text("SELECT id FROM admin_users"))).scalar_one()
    user = await session.get(AdminUser, loaded)
    assert user is not None

    assert PASSWORD_HASH not in repr(user)
    assert TOTP_SECRET not in repr(user)


def test_has_totp_enrolled_reports_without_exposing(session: AsyncSession) -> None:
    """Callers asking "is 2FA on?" should never pull the secret into a local."""
    assert build_admin_user(totp_secret=TOTP_SECRET).has_totp_enrolled is True
    assert build_admin_user(totp_secret=None).has_totp_enrolled is False


# ---------------------------------------------------------------------------
# TOTP secret encryption at rest
# ---------------------------------------------------------------------------


async def test_totp_secret_is_ciphertext_in_the_database(session: AsyncSession) -> None:
    """The actual claim: a database dump does not contain the seed."""
    user = build_admin_user(totp_secret=TOTP_SECRET)
    session.add(user)
    await session.flush()

    stored = await session.execute(
        text("SELECT totp_secret FROM admin_users WHERE id = :id"), {"id": user.id}
    )
    raw = stored.scalar_one()

    assert raw != TOTP_SECRET
    assert TOTP_SECRET not in raw
    # Fernet tokens are versioned and start with 0x80, base64'd as "gAAAAA".
    assert raw.startswith("gAAAAA")


async def test_totp_secret_round_trips_transparently(session: AsyncSession) -> None:
    """Plaintext in Python. It must, or verifying a code is impossible."""
    user = build_admin_user(totp_secret=TOTP_SECRET)
    session.add(user)
    await session.flush()
    user_id = user.id
    session.expunge_all()

    loaded = await session.get(AdminUser, user_id)

    assert loaded is not None
    assert loaded.totp_secret == TOTP_SECRET


async def test_null_totp_secret_stays_null(session: AsyncSession) -> None:
    """An admin exists before enrolling an authenticator."""
    user = build_admin_user(totp_secret=None)
    session.add(user)
    await session.flush()
    await session.refresh(user)

    assert user.totp_secret is None


def test_encryption_is_non_deterministic() -> None:
    """Same plaintext, different ciphertext — each token carries a random IV.

    Documents why this column can never be searched by value or usefully
    indexed, which is a property worth knowing before someone tries.
    """
    first = encryption.encrypt(TOTP_SECRET)
    second = encryption.encrypt(TOTP_SECRET)

    assert first != second
    assert encryption.decrypt(first) == encryption.decrypt(second) == TOTP_SECRET


def test_tampered_ciphertext_is_rejected_not_silently_garbled() -> None:
    """Fernet is authenticated, so a modified token fails loudly."""
    token = encryption.encrypt(TOTP_SECRET)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    with pytest.raises(InvalidToken):
        encryption.decrypt(tampered)


def test_a_different_key_cannot_decrypt() -> None:
    """What a lost or rotated key looks like: unrecoverable.

    This is the failure mode `.env.example` warns about — losing the key means
    losing admin access, because no other copy of the seed exists.
    """
    token = encryption.encrypt(TOTP_SECRET)
    other = Fernet(Fernet.generate_key())

    with pytest.raises(InvalidToken):
        other.decrypt(token.encode())


def test_generated_keys_are_valid_fernet_keys() -> None:
    Fernet(encryption.generate_key().encode())


# ---------------------------------------------------------------------------
# The encryption key as configuration
# ---------------------------------------------------------------------------


def test_settings_rejects_a_malformed_key() -> None:
    """Fails at startup rather than at first admin login."""
    with pytest.raises(ValueError, match="valid Fernet key"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
            totp_encryption_key="not-a-key",
        )


def test_settings_rejects_the_example_key_in_production() -> None:
    """That key is published in this repository, so anything encrypted under
    it is effectively plaintext. A stray `cp .env.example .env` on a server
    would otherwise be silent and total."""
    with pytest.raises(ValueError, match="example key"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
            totp_encryption_key=EXAMPLE_TOTP_KEY,
            environment="production",
        )


def test_settings_allows_the_example_key_outside_production() -> None:
    """Local development and CI use it deliberately."""
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",  # type: ignore[arg-type]
        totp_encryption_key=EXAMPLE_TOTP_KEY,
        environment="local",
    )

    assert settings.totp_encryption_key == EXAMPLE_TOTP_KEY


def test_the_running_test_suite_uses_a_valid_key() -> None:
    """Guards the fixture wiring itself."""
    Fernet(get_settings().totp_encryption_key.encode())


async def test_admin_email_is_unique(session: AsyncSession) -> None:
    session.add(build_admin_user(email="host@example.com"))
    await session.flush()

    async with expect_violation(session, "ix_admin_users_email"):
        session.add(build_admin_user(email="host@example.com"))
        await session.flush()


# ---------------------------------------------------------------------------
# processed_webhook_events
# ---------------------------------------------------------------------------


async def test_duplicate_event_is_rejected(session: AsyncSession) -> None:
    """The idempotency guarantee.

    Gateways retry the same event aggressively. Under concurrency two
    deliveries can both pass an application-level "have we seen this?" check;
    the unique constraint is what makes sure only one is recorded, and
    therefore only one booking confirmation or refund happens.
    """
    session.add(build_webhook_event(provider_event_id="evt_123"))
    await session.flush()

    async with expect_violation(session, "uq_processed_webhook_events_provider_provider_event_id"):
        session.add(build_webhook_event(provider_event_id="evt_123"))
        await session.flush()


async def test_same_event_id_from_a_different_provider_is_allowed(
    session: AsyncSession,
) -> None:
    """Uniqueness is per provider — two gateways may both use "evt_1"."""
    session.add(build_webhook_event(provider="external_checkout", provider_event_id="evt_1"))
    session.add(build_webhook_event(provider="some_other_gateway", provider_event_id="evt_1"))
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM processed_webhook_events"))
    assert count.scalar_one() == 2


async def test_raw_body_is_stored_byte_for_byte(session: AsyncSession) -> None:
    """**The reason raw_body is BYTEA rather than JSONB.**

    Signature verification is an HMAC over the exact bytes on the wire. This
    body has non-alphabetical key order, irregular whitespace and a trailing
    newline — all of which a JSON round trip would normalise away, producing a
    different signature from the one the provider sent. Storing the parsed
    form only would make a signature-mismatch investigation impossible.
    """
    body = b'{"zebra":1,   "alpha":  2,\n  "nested":{"b":1,"a":2}}\n'

    event = build_webhook_event(raw_body=body)
    session.add(event)
    await session.flush()
    session.expunge_all()

    stored = await session.execute(text("SELECT raw_body FROM processed_webhook_events"))
    returned = stored.scalar_one()

    assert returned == body
    # Byte-for-byte, not merely JSON-equivalent: re-dumping would reorder the
    # keys, drop the extra spaces and lose the trailing newline.
    assert json.dumps(json.loads(body)).encode() != returned


async def test_raw_body_accepts_a_payload_that_is_not_valid_utf8(
    session: AsyncSession,
) -> None:
    """A malformed or binary body must still be recorded, not raise.

    Losing the record of a delivery is worse than storing something unreadable,
    because the delivery is exactly what needs investigating.
    """
    body = b"\xff\xfe not utf-8 at all \x00\x01"

    event = build_webhook_event(raw_body=body, payload=None, event_type=None)
    session.add(event)
    await session.flush()
    await session.refresh(event)

    assert event.raw_body == body
    assert event.payload is None


async def test_structured_payload_is_stored_alongside_the_raw_bytes(
    session: AsyncSession,
) -> None:
    """Both, never one instead of the other."""
    body = b'{"type":"checkout.completed","amount":3000}'
    event = build_webhook_event(
        raw_body=body,
        payload={"type": "checkout.completed", "amount": 3000},
        event_type="checkout.completed",
    )
    session.add(event)
    await session.flush()
    await session.refresh(event)

    assert event.raw_body == body
    assert event.payload == {"type": "checkout.completed", "amount": 3000}
    assert event.event_type == "checkout.completed"


async def test_received_at_defaults_and_processed_at_starts_null(
    session: AsyncSession,
) -> None:
    """A row with received_at but no processed_at is a delivery that arrived
    and then crashed mid-handling — the thing you look for after an incident."""
    event = build_webhook_event()
    session.add(event)
    await session.flush()
    await session.refresh(event)

    assert event.received_at is not None
    assert event.received_at.tzinfo is not None
    assert event.processed_at is None


# ---------------------------------------------------------------------------
# notification_log
# ---------------------------------------------------------------------------


async def test_notification_defaults(session: AsyncSession) -> None:
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    entry = build_notification_log(booking)
    session.add(entry)
    await session.flush()
    await session.refresh(entry)

    assert entry.status is NotificationStatus.QUEUED
    assert entry.retry_count == 0
    assert entry.sent_at is None
    assert entry.last_attempted_at is None


async def test_retry_count_distinguishes_transient_from_permanent_failure(
    session: AsyncSession,
) -> None:
    """**The reason retry_count and last_attempted_at exist.**

    Both rows below are `failed` with an error message. With only `status` and
    `sent_at` they would be indistinguishable, and "failed once, ARQ will try
    again" would look exactly like "gave up hours ago, nobody knows". The
    retry count is what separates them, and last_attempted_at says how stale
    the silence is.
    """
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    attempted_at = datetime.now(UTC)
    will_retry = build_notification_log(
        booking,
        type=NotificationType.REMINDER_24H,
        status=NotificationStatus.FAILED,
        retry_count=1,
        last_attempted_at=attempted_at,
        error="smtp timeout",
    )
    gave_up = build_notification_log(
        booking,
        type=NotificationType.REMINDER_1H,
        status=NotificationStatus.FAILED,
        retry_count=5,
        last_attempted_at=attempted_at - timedelta(hours=6),
        error="smtp timeout",
    )
    session.add_all([will_retry, gave_up])
    await session.flush()

    max_retries = 5
    stalled = await session.execute(
        text(
            "SELECT count(*) FROM notification_log "
            "WHERE status = 'failed' AND retry_count >= :limit"
        ),
        {"limit": max_retries},
    )

    assert stalled.scalar_one() == 1


async def test_sent_at_must_accompany_sent_status(session: AsyncSession) -> None:
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    async with expect_violation(session, "ck_notification_log_sent_at_matches_status"):
        session.add(build_notification_log(booking, status=NotificationStatus.SENT, sent_at=None))
        await session.flush()


async def test_unsent_notification_may_not_claim_a_sent_at(session: AsyncSession) -> None:
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    async with expect_violation(session, "ck_notification_log_sent_at_matches_status"):
        session.add(
            build_notification_log(
                booking, status=NotificationStatus.FAILED, sent_at=datetime.now(UTC)
            )
        )
        await session.flush()


async def test_retry_count_cannot_be_negative(session: AsyncSession) -> None:
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    # last_attempted_at is supplied so the retried-rows constraint is satisfied
    # and the non-negative check is the only one that can fire — otherwise this
    # would pass on the wrong violation.
    async with expect_violation(session, "ck_notification_log_retry_count_non_negative"):
        session.add(
            build_notification_log(booking, retry_count=-1, last_attempted_at=datetime.now(UTC))
        )
        await session.flush()


async def test_a_retried_row_must_record_when_it_was_attempted(
    session: AsyncSession,
) -> None:
    """Guards a worker bumping retry_count without setting the timestamp,
    which would make the "gave up long ago" query silently wrong."""
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    async with expect_violation(session, "ck_notification_log_retried_rows_have_an_attempt_time"):
        session.add(
            build_notification_log(
                booking,
                status=NotificationStatus.FAILED,
                retry_count=2,
                last_attempted_at=None,
            )
        )
        await session.flush()


async def test_a_successful_send_records_both_timestamps(session: AsyncSession) -> None:
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    now = datetime.now(UTC)
    entry = build_notification_log(
        booking,
        status=NotificationStatus.SENT,
        sent_at=now,
        last_attempted_at=now,
        retry_count=2,
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry)

    assert entry.status is NotificationStatus.SENT
    assert entry.sent_at is not None
    # Succeeded on the third attempt: the history survives the success.
    assert entry.retry_count == 2


async def test_notification_types_are_stored_as_lowercase_values(
    session: AsyncSession,
) -> None:
    """Same values_callable trap as the other native enums."""
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()

    entry = build_notification_log(booking, type=NotificationType.MANUAL_PAYMENT_REJECTED)
    session.add(entry)
    await session.flush()

    stored = await session.execute(
        text("SELECT type::text FROM notification_log WHERE id = :id"), {"id": entry.id}
    )

    assert stored.scalar_one() == "manual_payment_rejected"


async def test_notification_log_survives_a_booking_delete_attempt(
    session: AsyncSession,
) -> None:
    """RESTRICT: an audit record of what was sent to a real person should
    outlive any attempt to tidy up the booking."""
    service = build_service()
    session.add(service)
    await session.flush()
    booking = build_booking(service)
    session.add(booking)
    await session.flush()
    session.add(build_notification_log(booking))
    await session.flush()

    async with expect_violation(session, "fk_notification_log_booking_id_bookings"):
        await session.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": booking.id})


def test_notification_log_repr_is_useful() -> None:
    entry = NotificationLog(
        booking_id=1,
        type=NotificationType.REMINDER_1H,
        status=NotificationStatus.FAILED,
        retry_count=3,
    )

    assert "reminder_1h" in repr(entry)
    assert "retries=3" in repr(entry)


def test_webhook_event_repr_is_useful() -> None:
    event = ProcessedWebhookEvent(
        provider="external_checkout",
        provider_event_id="evt_9",
        raw_body=b"{}",
        event_type="checkout.completed",
    )

    assert "evt_9" in repr(event)
