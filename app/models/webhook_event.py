"""The webhook idempotency ledger."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProcessedWebhookEvent(Base):
    """One webhook delivery we have already handled.

    Payment gateways retry aggressively and will send the same event several
    times — after a timeout, after a 500, sometimes just because. Confirming a
    booking twice, or refunding twice, is the failure this table prevents:
    Phase 5 verifies the signature, then inserts here, and a duplicate
    `(provider, provider_event_id)` means the event was already processed and
    the handler returns 200 without doing anything.

    The unique constraint is what makes that safe under concurrency. Two
    simultaneous deliveries of the same event both pass an application-level
    "have we seen this?" check; only one survives the INSERT.

    No TimestampMixin here. `received_at` is the meaningful time and an
    `updated_at` would be misleading — rows in this table are written once and
    never modified.
    """

    __tablename__ = "processed_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # A plain string rather than the PaymentProvider enum, on purpose. This is
    # a ledger of what actually arrived, and it must be able to record a
    # delivery from a source we do not recognise — that is precisely the
    # situation worth having a record of. A native enum would reject the row
    # and leave no trace.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # The gateway's own event id, the thing that makes a retry identifiable.
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # ------------------------------------------------------------------
    # The raw body, exactly as received. BYTEA, not TEXT, not JSONB.
    #
    # Signature verification is an HMAC over the exact bytes on the wire. Any
    # re-serialisation changes them: JSON key order is not preserved, whitespace
    # is normalised, unicode escaping differs between encoders, and a trailing
    # newline disappears. A payload stored as JSONB and dumped back out will
    # very often produce a *different* signature from the one the provider
    # sent — so when a signature mismatch is the bug you are chasing, a parsed
    # copy is worse than useless: it actively misleads.
    #
    # BYTEA also means a body that is not valid UTF-8, or not JSON at all, is
    # still recorded rather than throwing during logging.
    # ------------------------------------------------------------------
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # The parsed form, stored *alongside* the raw bytes and never instead of
    # them. This is the convenient one to query in the admin panel; raw_body is
    # the authoritative one. Nullable because a body that fails to parse must
    # still be recorded.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Extracted for querying, e.g. "checkout.completed". Nullable for the same
    # reason as payload.
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    # Set when handling finished. A row with received_at but no processed_at is
    # a delivery that arrived and then crashed mid-handling — exactly what you
    # want to find after an incident.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_processed_webhook_events_provider_provider_event_id",
        ),
        Index("ix_processed_webhook_events_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<ProcessedWebhookEvent {self.provider}:{self.provider_event_id} "
            f"type={self.event_type!r}>"
        )
