"""Entities: the things the business talks about. Plain dataclasses with no framework imports.

The infrastructure layer maps them onto database tables (SQLAlchemy imperative mapping), so the
domain never knows how it is stored. Timestamps are naive UTC and are set by the use cases from
the Clock port. `eq=False` keeps identity semantics, which the ORM relies on."""
from dataclasses import dataclass, field
from datetime import datetime

OWNER, MANAGER = "owner", "manager"


@dataclass(eq=False)
class User:
    tg_id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    lang: str | None = None
    updated_at: datetime | None = None

    def display_name(self) -> str:
        full = " ".join(x for x in (self.first_name, self.last_name) if x)
        return full or (f"@{self.username}" if self.username else str(self.tg_id))


@dataclass(eq=False)
class TeamRequest:
    """A request to register a new team through the master bot."""
    tg_id: int
    team_name: str
    status: str = "pending"  # pending|approved|rejected|used
    decided_by: int | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Team:
    name: str
    bot_id: int
    bot_username: str
    bot_token_enc: str
    webhook_secret: str
    owner_tg_id: int
    group_chat_id: int | None = None
    fee: int = 0
    due_day: int = 5
    lang: str = "vi"
    bank_name: str | None = None
    bank_account: str | None = None
    bank_holder: str | None = None
    sepay_key_hash: str | None = None
    sepay_token_enc: str | None = None     # owner's SePay API token, encrypted; lets us manage the webhook
    sepay_webhook_id: str | None = None    # the team's one webhook in that SePay account
    last_billed_period: str | None = None
    active: bool = True
    created_at: datetime | None = None
    id: int | None = None

    def has_bank(self) -> bool:
        return bool(self.bank_name and self.bank_account)


@dataclass(eq=False)
class TeamAdmin:
    team_id: int
    tg_id: int
    role: str  # owner|manager
    added_by: int | None = None
    added_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class ManagerRequest:
    team_id: int
    tg_id: int
    status: str = "pending"  # pending|approved|rejected|expired
    created_at: datetime | None = None
    decided_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Member:
    team_id: int
    name: str
    nickname: str | None = None
    shirt_number: str | None = None
    phone: str | None = None
    note: str | None = None
    fee_override: int | None = None
    status: str = "active"  # active|paused|left
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Bill:
    team_id: int
    member_id: int
    kind: str  # monthly|charge|split
    title: str
    period: str  # YYYY-MM
    amount: int
    status: str = "unpaid"  # unpaid|paid|waived
    # "<member_id>:<period>" for monthly bills only, so a member is never billed twice for a month
    monthly_key: str | None = None
    payment_id: int | None = None
    created_at: datetime | None = None
    paid_at: datetime | None = None
    id: int | None = None

    def mark_paid(self, payment_id: int, at: datetime) -> None:
        self.status, self.payment_id, self.paid_at = "paid", payment_id, at


@dataclass(eq=False)
class PaymentIntent:
    """Someone chose whose bills to pay; the code goes in the bank transfer content."""
    team_id: int
    code: str
    amount: int
    payer_tg_id: int | None = None
    bill_ids: list = field(default_factory=list)
    member_names: list = field(default_factory=list)
    status: str = "pending"  # pending|paid|expired
    payment_id: int | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Payment:
    team_id: int
    source: str  # sepay|cash
    amount: int
    sepay_tx_id: str | None = None
    content: str | None = None
    intent_id: int | None = None
    payer_tg_id: int | None = None
    recorded_by: int | None = None
    status: str = "unmatched"  # matched|unmatched|resolved
    reason: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Expense:
    team_id: int
    amount: int
    title: str
    created_by: int
    photo_file_id: str | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class AuditEntry:
    team_id: int | None
    actor_tg_id: int | None
    action: str
    target: str | None = None
    before: dict | None = None
    after: dict | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class JobRun:
    """Marks a scheduled job that already ran, e.g. one team's reminder on one date."""
    key: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class IncomingTransfer:
    """Money that arrived in a team's bank account, as reported by the payment provider."""
    tx_id: str
    amount: int
    content: str
    incoming: bool = True
