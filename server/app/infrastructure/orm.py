"""Database tables, mapped onto the domain dataclasses (SQLAlchemy imperative mapping).

The domain stays free of SQLAlchemy: this module knows about the domain, never the other way round."""
from sqlalchemy import (JSON, BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, MetaData, String, Table,
                        Text, UniqueConstraint)
from sqlalchemy.orm import registry

from ..domain import entities as e

metadata = MetaData()
mapper_registry = registry(metadata=metadata)


def _id():
    return Column("id", Integer, primary_key=True)


users = Table(
    "tg_users", metadata,
    Column("tg_id", BigInteger, primary_key=True, autoincrement=False),
    Column("first_name", String(128)), Column("last_name", String(128)), Column("username", String(64)),
    Column("lang", String(2)), Column("updated_at", DateTime),
)

team_requests = Table(
    "team_requests", metadata, _id(),
    Column("tg_id", BigInteger, index=True, nullable=False), Column("team_name", String(100), nullable=False),
    Column("status", String(16), nullable=False), Column("decided_by", BigInteger),
    Column("created_at", DateTime, nullable=False), Column("decided_at", DateTime),
)

teams = Table(
    "teams", metadata, _id(),
    Column("name", String(100), nullable=False), Column("bot_id", BigInteger, unique=True, nullable=False),
    Column("bot_username", String(64), nullable=False), Column("bot_token_enc", Text, nullable=False),
    Column("webhook_secret", String(64), nullable=False), Column("owner_tg_id", BigInteger, index=True, nullable=False),
    Column("group_chat_id", BigInteger), Column("fee", Integer, nullable=False), Column("due_day", Integer, nullable=False),
    Column("lang", String(2), nullable=False), Column("bank_name", String(64)), Column("bank_account", String(32)),
    Column("bank_holder", String(128)), Column("sepay_key_hash", String(64)), Column("last_billed_period", String(7)),
    Column("sepay_token_enc", Text), Column("sepay_webhook_id", String(64)),
    Column("public_token", String(32), unique=True), Column("pay_account", String(32)),
    Column("notify_day", Integer), Column("due_days", Integer), Column("late_fee", Integer),
    Column("late_fee_since", Date),
    Column("active", Boolean, nullable=False), Column("created_at", DateTime, nullable=False),
)

team_admins = Table(
    "team_admins", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("tg_id", BigInteger, nullable=False),
    Column("role", String(16), nullable=False), Column("added_by", BigInteger), Column("added_at", DateTime),
    UniqueConstraint("team_id", "tg_id"),
)

manager_requests = Table(
    "manager_requests", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("tg_id", BigInteger, nullable=False),
    Column("status", String(16), nullable=False), Column("created_at", DateTime, nullable=False),
    Column("decided_at", DateTime),
)

members = Table(
    "members", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("name", String(100), nullable=False),
    Column("nickname", String(64)), Column("shirt_number", String(8)), Column("phone", String(32)),
    Column("note", String(255)), Column("fee_override", Integer), Column("status", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False), Column("plan_id", Integer),
)

fee_plans = Table(
    "fee_plans", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("amount", Integer, nullable=False),
    Column("months", Integer, nullable=False), Column("created_at", DateTime, nullable=False),
    Column("notify_day", Integer), Column("due_days", Integer), Column("late_fee", Integer),
    Column("late_fee_since", Date),
)

payments = Table(
    "payments", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("source", String(16), nullable=False),
    Column("amount", Integer, nullable=False), Column("sepay_tx_id", String(64), unique=True),
    Column("content", String(500)), Column("intent_id", Integer), Column("payer_tg_id", BigInteger),
    Column("recorded_by", BigInteger), Column("status", String(16), nullable=False), Column("reason", String(32)),
    Column("note", String(500)), Column("created_at", DateTime, nullable=False),
)

bills = Table(
    "bills", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False),
    Column("member_id", ForeignKey("members.id"), index=True, nullable=False),
    Column("kind", String(16), nullable=False), Column("title", String(100), nullable=False),
    Column("period", String(7), nullable=False), Column("amount", Integer, nullable=False),
    Column("status", String(16), nullable=False), Column("monthly_key", String(32), unique=True),
    Column("payment_id", ForeignKey("payments.id")), Column("created_at", DateTime, nullable=False),
    Column("paid_at", DateTime), Column("months", Integer), Column("plan_id", Integer),
)

payment_intents = Table(
    "payment_intents", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("code", String(16), unique=True),
    Column("amount", Integer, nullable=False), Column("payer_tg_id", BigInteger), Column("bill_ids", JSON),
    Column("member_names", JSON), Column("status", String(16), nullable=False), Column("payment_id", Integer),
    Column("created_at", DateTime, nullable=False), Column("expires_at", DateTime, nullable=False),
)

expenses = Table(
    "expenses", metadata, _id(),
    Column("team_id", ForeignKey("teams.id"), index=True, nullable=False), Column("amount", Integer, nullable=False),
    Column("title", String(200), nullable=False), Column("photo_file_id", String(255)),
    Column("created_by", BigInteger, nullable=False), Column("created_at", DateTime, nullable=False),
)

audit_log = Table(
    "audit_log", metadata, _id(),
    Column("team_id", Integer, index=True), Column("actor_tg_id", BigInteger), Column("action", String(64), nullable=False),
    Column("target", String(200)), Column("before", JSON), Column("after", JSON), Column("created_at", DateTime),
)

# Infrastructure-only secrets (e.g. the admin's Telegram login for @BotFather), encrypted. Not a domain thing,
# so not mapped onto an entity.
app_secrets = Table(
    "app_secrets", metadata,
    Column("name", String(64), primary_key=True), Column("value_enc", Text, nullable=False),
    Column("updated_at", DateTime),
)

job_runs = Table(
    "job_log", metadata,
    Column("key", String(100), primary_key=True), Column("created_at", DateTime),
)

_MAPPINGS = [
    (e.User, users), (e.TeamRequest, team_requests), (e.Team, teams), (e.TeamAdmin, team_admins),
    (e.ManagerRequest, manager_requests), (e.Member, members), (e.FeePlan, fee_plans), (e.Payment, payments), (e.Bill, bills),
    (e.PaymentIntent, payment_intents), (e.Expense, expenses), (e.AuditEntry, audit_log), (e.JobRun, job_runs),
]
_started = False


def start_mappers() -> None:
    """Map the domain dataclasses onto the tables. Safe to call more than once."""
    global _started
    if _started:
        return
    for cls, table in _MAPPINGS:
        mapper_registry.map_imperatively(cls, table)
    _started = True
