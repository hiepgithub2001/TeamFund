from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp; every datetime column stores UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TgUser(Base):
    __tablename__ = "tg_users"
    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    last_name: Mapped[Optional[str]] = mapped_column(String(128))
    username: Mapped[Optional[str]] = mapped_column(String(64))
    lang: Mapped[Optional[str]] = mapped_column(String(2))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TeamRequest(Base):
    """A request to register a new team through the master bot."""
    __tablename__ = "team_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    team_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected|used
    decided_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    bot_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    bot_username: Mapped[str] = mapped_column(String(64))
    bot_token_enc: Mapped[str] = mapped_column(Text)
    webhook_secret: Mapped[str] = mapped_column(String(64))
    owner_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    fee: Mapped[int] = mapped_column(Integer, default=0)
    due_day: Mapped[int] = mapped_column(Integer, default=5)
    lang: Mapped[str] = mapped_column(String(2), default="vi")
    bank_name: Mapped[Optional[str]] = mapped_column(String(64))
    bank_account: Mapped[Optional[str]] = mapped_column(String(32))
    bank_holder: Mapped[Optional[str]] = mapped_column(String(128))
    sepay_key_hash: Mapped[Optional[str]] = mapped_column(String(64))
    last_billed_period: Mapped[Optional[str]] = mapped_column(String(7))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TeamAdmin(Base):
    __tablename__ = "team_admins"
    __table_args__ = (UniqueConstraint("team_id", "tg_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(16))  # owner|manager
    added_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ManagerRequest(Base):
    __tablename__ = "manager_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected|expired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Member(Base):
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    nickname: Mapped[Optional[str]] = mapped_column(String(64))
    shirt_number: Mapped[Optional[str]] = mapped_column(String(8))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[Optional[str]] = mapped_column(String(255))
    fee_override: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|paused|left
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # monthly|charge|split
    title: Mapped[str] = mapped_column(String(100))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="unpaid")  # unpaid|paid|waived
    # "<member_id>:<period>" for monthly bills only, so a member can never be billed twice for a month
    monthly_key: Mapped[Optional[str]] = mapped_column(String(32), unique=True)
    payment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("payments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    payer_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    bill_ids: Mapped[list] = mapped_column(JSON)
    member_names: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|paid|expired
    payment_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # sepay|cash
    amount: Mapped[int] = mapped_column(Integer)
    sepay_tx_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    content: Mapped[Optional[str]] = mapped_column(String(500))
    intent_id: Mapped[Optional[int]] = mapped_column(Integer)
    payer_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    recorded_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))  # matched|unmatched|resolved
    reason: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    actor_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[Optional[str]] = mapped_column(String(200))
    before: Mapped[Optional[dict]] = mapped_column(JSON)
    after: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobLog(Base):
    """Marks scheduler jobs that already ran (e.g. a reminder for a team on a date)."""
    __tablename__ = "job_log"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
