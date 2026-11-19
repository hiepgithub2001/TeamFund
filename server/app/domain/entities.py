"""Entities: the things the business talks about. Plain dataclasses with no framework imports.

The infrastructure layer maps them onto database tables (SQLAlchemy imperative mapping), so the
domain never knows how it is stored. Timestamps are naive UTC and are set by the use cases from
the Clock port. `eq=False` keeps identity semantics, which the ORM relies on."""
from dataclasses import dataclass, field
from datetime import date, datetime

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
    due_day: int = 5                       # old setting (due day of the month); used until notify_day is set
    notify_day: int | None = None          # day of the month the fee is announced
    due_days: int | None = None            # days after the announcement until it's due
    late_fee: int | None = None            # đ per day a fee bill is overdue; 0/None: off
    late_fee_since: date | None = None     # the day the late fee was turned on: no fines for days before it
    fee_months: int | None = None          # the default package's cycle: 1 (None) monthly, 3, 6 or 12
    lang: str = "vi"
    bank_name: str | None = None
    bank_account: str | None = None
    bank_holder: str | None = None
    sepay_key_hash: str | None = None
    sepay_token_enc: str | None = None     # owner's SePay API token, encrypted; lets us manage the webhook
    sepay_webhook_id: str | None = None    # the team's one webhook in that SePay account
    public_token: str | None = None        # secret part of the team's pay page URL (/p/<token>/)
    pay_account: str | None = None         # VA to pay into, for banks that only report transfers to a VA
    sepay_quota: int | None = None         # transactions the owner's SePay plan allows per cycle (None: not told)
    sepay_cycle_day: int | None = None     # day of the month that plan's cycle restarts (None: the 1st)
    last_billed_period: str | None = None
    active: bool = True
    created_at: datetime | None = None
    id: int | None = None

    def has_bank(self) -> bool:
        return bool(self.bank_name and self.bank_account)

    def pay_to(self) -> str | None:
        """The account number payers transfer to: the VA when the bank needs one, else the account itself."""
        return self.pay_account or self.bank_account


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
class Follower:
    """Someone who opened the team bot: they get the team's announcements privately (unless muted)."""
    team_id: int
    tg_id: int
    muted: bool = False
    created_at: datetime | None = None
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
    # no longer used: plans are in PlanMember (a member can be on several, one at a time); kept for old rows
    plan_id: int | None = None
    plan_from: str | None = None
    plan_until: str | None = None
    status: str = "active"  # active|paused|left
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class PlanMember:
    """A member on a plan for whole periods: from `start` to `until` (YYYY-MM, both included; None: since
    always / for good). A member can be on several plans, never two at the same time."""
    team_id: int
    plan_id: int
    member_id: int
    start: str | None = None
    until: str | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class FeePlan:
    """A fee some members pay on a cycle instead of the team's monthly fee, e.g. 300,000đ every 3 months.
    Each member's cycle starts the first month they are billed on the plan."""
    team_id: int
    amount: int
    months: int = 1  # 1 monthly | 3 quarterly | 6 half-yearly | 12 yearly
    # the plan's own deadline and late fee; None: the same as the team's default package
    notify_day: int | None = None
    due_days: int | None = None
    late_fee: int | None = None
    late_fee_since: date | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Bill:
    team_id: int
    member_id: int
    kind: str  # monthly|late|charge|split; "replaced": a fee bill a plan change made unnecessary
    title: str
    period: str  # YYYY-MM
    amount: int
    status: str = "unpaid"  # unpaid|paid|waived
    # "<member_id>:<period>" for fee bills only, so a member is never billed twice for a month
    monthly_key: str | None = None
    months: int | None = None  # fee bills: how many months from `period` this bill covers (None = 1)
    plan_id: int | None = None  # fee and late bills: the plan billed under (None: the default package)
    due_on: date | None = None  # fee bills: an own due date (someone who joined mid-period), else the package's
    match_id: int | None = None  # a match fee: someone who played that match pays for it
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
    kind: str | None = None  # None: bills; "donation": money given to the fund (member_names: the donor)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Payment:
    team_id: int
    source: str  # sepay|cash|other (a prize, a sponsor, the opening balance)|donation (a donation QR; note: donor)
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
    undo: list | None = None          # the rows this action changed, to put back with /undo (undoable actions)
    undone_at: datetime | None = None
    undone_by: int | None = None
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


@dataclass(eq=False)
class Match:
    """A match on the team's calendar. Nothing to do with money: pitch costs go through /expense."""
    team_id: int
    day: date
    at: str | None = None        # "19:00"
    place: str | None = None
    opponent: str | None = None  # None: no opponent yet
    note: str | None = None
    status: str = "scheduled"    # scheduled|cancelled|deleted
    reason: str | None = None    # why it was cancelled
    schedule_id: int | None = None  # made by a repeating calendar
    paid: int | None = None      # what the team paid the opponent for it (recorded as an expense)
    received: int | None = None  # what the opponent paid the team (recorded as income)
    fee: int | None = None       # what each player charged for this match pays (set by a manager, per match)
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class MatchSchedule:
    """A repeating calendar: every week, every 2 weeks, or every month; it fills matches ahead of time."""
    team_id: int
    every: str                   # weekly|biweekly|monthly
    weekday: int | None = None   # 0 Monday … 6 Sunday
    nth: int | None = None       # monthly: 1–4, or -1 for the last such weekday
    mday: int | None = None      # monthly on a fixed day of the month
    anchor: date | None = None   # biweekly: a first date
    at: str | None = None
    place: str | None = None
    active: bool = True
    created_at: datetime | None = None
    id: int | None = None


@dataclass(eq=False)
class Contact:
    """An opposing team's contact: name, phone (10 digits) and a free note. Managers only."""
    team_id: int
    name: str
    phone: str | None = None
    note: str | None = None
    created_at: datetime | None = None
    id: int | None = None
