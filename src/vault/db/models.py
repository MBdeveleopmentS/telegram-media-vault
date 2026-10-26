"""Relational schema.

Conventions:
- All Telegram identifiers are 64-bit (BigInteger).
- All timestamps are timezone-aware UTC.
- No plaintext session material, phone numbers, login codes or media bytes are stored.
- Every tenant-owned row carries ``user_id`` so queries can always be scoped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# SQLite only autoincrements INTEGER PRIMARY KEY; Postgres gets BIGINT identity.
PK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: UTCDateTime()}


def _enum(e: type[StrEnum], name: str) -> Enum:
    return Enum(e, name=name, native_enum=False, length=32, validate_strings=True)


# --------------------------------------------------------------------- enums
class Tier(StrEnum):
    FREE = "FREE"
    PREMIUM = "PREMIUM"


class AccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"  # Telegram revoked/expired the authorization
    DISCONNECTED = "DISCONNECTED"  # user disconnected


class ChatStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"  # paused by user
    PAUSED_TIER = "PAUSED_TIER"  # paused because of plan limits (e.g. Premium expired)
    PAUSED_ACCOUNT = "PAUSED_ACCOUNT"  # account disconnected/expired
    INACCESSIBLE = "INACCESSIBLE"  # account lost access to the chat


class ChatType(StrEnum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    SUPERGROUP = "SUPERGROUP"
    CHANNEL = "CHANNEL"


class JobState(StrEnum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    UPLOADING = "UPLOADING"
    DELIVERED = "DELIVERED"
    RETRY_WAIT = "RETRY_WAIT"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class MediaKind(StrEnum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    DOCUMENT_IMAGE = "DOCUMENT_IMAGE"
    DOCUMENT_VIDEO = "DOCUMENT_VIDEO"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"


class FlowState(StrEnum):
    CREATED = "CREATED"  # link sent to the user via bot
    OPENED = "OPENED"  # link exchanged for a browser-bound cookie (token now consumed)
    CODE_SENT = "CODE_SENT"
    PASSWORD_NEEDED = "PASSWORD_NEEDED"  # noqa: S105 - state name, not a secret
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


# --------------------------------------------------------------------- tables
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="he", nullable=False)
    # Destination = the user's private chat with the bot (chat id == telegram_user_id).
    destination_ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    privacy_ack_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_issue_digest_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    account: Mapped[TelegramAccount | None] = relationship(back_populates="user", uselist=False)
    subscription: Mapped[Subscription | None] = relationship(back_populates="user", uselist=False)


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # MTProto user id; MUST equal users.telegram_user_id (identity binding invariant).
    tg_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[AccountStatus] = mapped_column(_enum(AccountStatus, "account_status"), nullable=False)
    shard: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    disconnected_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="account")

    __table_args__ = (
        CheckConstraint("shard >= 0", name="ck_accounts_shard"),
        Index("ix_accounts_shard_status", "shard", "status"),
    )


class EncryptedSession(Base):
    """AES-256-GCM encrypted Telethon StringSession.

    The key never lives in the database. AAD binds the ciphertext to
    (account_id, tg_user_id) so swapping ciphertexts between rows fails decryption.
    """

    __tablename__ = "encrypted_sessions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    key_id: Mapped[str] = mapped_column(String(32), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class MonitoredChat(Base):
    __tablename__ = "monitored_chats"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    # Telethon "marked" peer id (users > 0, basic groups < 0, channels -100...).
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Account-specific access hash needed to address the peer after restarts.
    access_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_type: Mapped[ChatType] = mapped_column(_enum(ChatType, "chat_type"), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ChatStatus] = mapped_column(_enum(ChatStatus, "chat_status"), nullable=False)
    keep_on_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_at: Mapped[datetime] = mapped_column(nullable=False)
    last_seen_message_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", name="uq_monitored_user_chat"),
        Index("ix_monitored_account_status", "account_id", "status"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    tier: Mapped[Tier] = mapped_column(_enum(Tier, "tier"), default=Tier.FREE, nullable=False)
    premium_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Reminder days already sent for the current expiry (e.g. [7, 3]).
    reminders_sent: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="subscription")

    __table_args__ = (Index("ix_subscriptions_tier_expiry", "tier", "premium_expires_at"),)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    invoice_payload: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    product: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus, "payment_status"), nullable=False)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    entitlement_from: Mapped[datetime | None] = mapped_column(nullable=True)
    entitlement_until: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount"),
        CheckConstraint("period_days > 0", name="ck_payments_period"),
        Index("ix_payments_user", "user_id", "status"),
    )


class MediaJob(Base):
    __tablename__ = "media_jobs"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    monitored_chat_id: Mapped[int] = mapped_column(
        ForeignKey("monitored_chats.id", ondelete="CASCADE"), nullable=False
    )
    shard: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_key: Mapped[str] = mapped_column(String(64), nullable=False)
    media_kind: Mapped[MediaKind] = mapped_column(_enum(MediaKind, "media_kind"), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(127), nullable=True)
    declared_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_date: Mapped[datetime | None] = mapped_column(nullable=True)
    priority: Mapped[Tier] = mapped_column(_enum(Tier, "job_priority"), nullable=False)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[JobState] = mapped_column(_enum(JobState, "job_state"), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    upload_started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivery_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # Idempotency key: one job per media item per message per account.
        UniqueConstraint(
            "account_id", "source_chat_id", "source_message_id", "media_key", name="uq_media_jobs_idem"
        ),
        Index("ix_media_jobs_state_next", "state", "next_attempt_at"),
        Index("ix_media_jobs_shard_state", "shard", "state"),
        Index("ix_media_jobs_user_state", "user_id", "state"),
        Index("ix_media_jobs_lease", "state", "lease_expires_at"),
    )


class MediaDelivery(Base):
    __tablename__ = "media_deliveries"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("media_jobs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    destination_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_unique_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_as: Mapped[str] = mapped_column(String(16), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    __table_args__ = (Index("ix_deliveries_user", "user_id", "delivered_at"),)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # Sanitized, non-secret details only (enforced by security.events.record).
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    __table_args__ = (Index("ix_security_events_created", "created_at"),)


class AuthenticationFlow(Base):
    __tablename__ = "authentication_flows"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # HMAC-SHA256 of the one-time link token; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # HMAC of the browser-binding cookie set when the link is first opened.
    browser_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    csrf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[FlowState] = mapped_column(_enum(FlowState, "flow_state"), nullable=False)
    consent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    password_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (Index("ix_auth_flows_user_created", "user_id", "created_at"),)
