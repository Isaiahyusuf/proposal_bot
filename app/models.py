from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    body: Mapped[str] = mapped_column(Text)
    customized: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserPreferences(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint("daily_limit >= 0 AND daily_limit <= 50"),
        CheckConstraint("min_interval_seconds >= 30"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    chains: Mapped[str] = mapped_column(String(255), default="")  # comma separated, empty = all
    min_liquidity_usd: Mapped[int] = mapped_column(Integer, default=0)
    max_age_hours: Mapped[int] = mapped_column(Integer, default=72)
    discovery_limit: Mapped[int] = mapped_column(Integer, default=20)
    auto_approve_drafts: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_limit: Mapped[int] = mapped_column(Integer, default=10)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    sending_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    configured: Mapped[bool] = mapped_column(Boolean, default=False)


class TelegramAccount(Base):
    """One Telegram connection per user. Secrets are encrypted at rest and never rendered."""
    __tablename__ = "telegram_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    api_id: Mapped[int] = mapped_column(Integer)
    api_hash_enc: Mapped[str] = mapped_column(Text)
    phone_enc: Mapped[str] = mapped_column(Text)
    session_enc: Mapped[Optional[str]] = mapped_column(Text)
    phone_code_hash_enc: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|connected
    flood_wait_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Per-user health state: unknown|checking|healthy|restricted|flood_wait|connection_error
    health_state: Mapped[str] = mapped_column(String(20), default="unknown", server_default="unknown")
    health_detail: Mapped[Optional[str]] = mapped_column(Text)  # explicit Telegram text/error, never invented
    health_source: Mapped[Optional[str]] = mapped_column(String(20))  # spambot|send_error|login
    health_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_spambot_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    restricted_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # only if Telegram states it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("user_id", "chain", "token_address"),
        UniqueConstraint("id", "user_id"),  # target for composite ownership FKs
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chain: Mapped[str] = mapped_column(String(50))
    token_address: Mapped[str] = mapped_column(String(128))
    project_name: Mapped[Optional[str]] = mapped_column(String(255))
    token_symbol: Mapped[Optional[str]] = mapped_column(String(50))
    website: Mapped[Optional[str]] = mapped_column(String(500))
    telegram_link: Mapped[Optional[str]] = mapped_column(String(500))
    dexscreener_url: Mapped[Optional[str]] = mapped_column(String(500))
    official_links: Mapped[Optional[str]] = mapped_column(Text)  # JSON list of links the project published
    # not_scanned | scanned | inaccessible
    scan_status: Mapped[str] = mapped_column(String(20), default="not_scanned", server_default="not_scanned")
    scan_note: Mapped[Optional[str]] = mapped_column(Text)
    scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        ForeignKeyConstraint(["lead_id", "user_id"], ["leads.id", "leads.user_id"], ondelete="CASCADE"),
        UniqueConstraint("lead_id", "username"),
        UniqueConstraint("id", "user_id"),
        CheckConstraint("category IN ('confirmed_creator','confirmed_developer','possible_owner',"
                        "'administrator','unverified_contact')", name="ck_candidate_category"),
        CheckConstraint("confidence IN ('high','medium','low')", name="ck_candidate_confidence"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(Integer)
    username: Mapped[str] = mapped_column(String(64))  # normalized, no '@', lowercase
    role: Mapped[str] = mapped_column(String(255), default="")  # observed role, as seen (not asserted)
    # confirmed_creator | confirmed_developer | possible_owner | administrator | unverified_contact
    category: Mapped[str] = mapped_column(String(30), default="unverified_contact", server_default="unverified_contact")
    evidence: Mapped[str] = mapped_column(Text, default="", server_default="")
    confidence: Mapped[str] = mapped_column(String(10), default="low", server_default="low")  # high|medium|low
    # verified (explicit Telegram/official-link proof) | unverified | user_supplied
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified", server_default="unverified")
    source: Mapped[str] = mapped_column(String(10), default="telegram", server_default="telegram")  # telegram|manual


class OutreachRecord(Base):
    __tablename__ = "outreach_records"
    __table_args__ = (
        ForeignKeyConstraint(["lead_id", "user_id"], ["leads.id", "leads.user_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["candidate_id", "user_id"], ["candidates.id", "candidates.user_id"],
                             ondelete="CASCADE"),
        UniqueConstraint("user_id", "recipient"),  # never message the same recipient twice per user
        UniqueConstraint("id", "user_id", name="uq_outreach_id_user"),
        CheckConstraint("status IN ('draft','approved','held','sending','sent','failed','delivery_failed',"
                        "'possible_block','skipped')", name="ck_outreach_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[int] = mapped_column(Integer)
    recipient: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    error: Mapped[Optional[str]] = mapped_column(Text)
    failure_kind: Mapped[Optional[str]] = mapped_column(String(30))  # temporary|privacy|invalid_recipient|possible_block|ambiguous
    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DoNotContact(Base):
    __tablename__ = "do_not_contact"
    __table_args__ = (UniqueConstraint("user_id", "username"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    username: Mapped[str] = mapped_column(String(64))
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    # manual | declined (recipient asked to stop) | blocked (user reports a block) | undeliverable (Telegram-confirmed)
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CleanupAction(Base):
    """Audit trail of user-confirmed message deletions. Outreach evidence rows are never removed."""
    __tablename__ = "cleanup_actions"
    __table_args__ = (
        ForeignKeyConstraint(["outreach_id", "user_id"], ["outreach_records.id", "outreach_records.user_id"],
                             ondelete="CASCADE"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    outreach_id: Mapped[int] = mapped_column(Integer)
    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    revoke: Mapped[bool] = mapped_column(Boolean, default=True)  # also delete for the recipient
    result: Mapped[str] = mapped_column(String(20))  # deleted | failed
    detail: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
