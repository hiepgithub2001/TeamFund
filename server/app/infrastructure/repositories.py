"""SQLAlchemy implementations of the repository ports."""
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..domain.entities import (AuditEntry, Bill, Contact, Expense, FeePlan, Follower, JobRun, ManagerRequest, Match,
                               MatchSchedule, Member, Payment, PaymentIntent, PlanMember,
                               Team, TeamAdmin, TeamRequest, User)


class _Repo:
    def __init__(self, s: Session):
        self.s = s

    def add(self, obj) -> None:
        self.s.add(obj)


class SqlUsers(_Repo):
    def get(self, tg_id: int) -> User | None:
        return self.s.get(User, tg_id)


class SqlTeamRequests(_Repo):
    def get(self, request_id: int) -> TeamRequest | None:
        return self.s.get(TeamRequest, request_id)

    def has_pending(self, tg_id: int) -> bool:
        return self.s.scalar(select(TeamRequest.id).where(TeamRequest.tg_id == tg_id,
                                                          TeamRequest.status == "pending")) is not None

    def pending(self) -> list[TeamRequest]:
        return list(self.s.scalars(select(TeamRequest).where(TeamRequest.status == "pending").order_by(TeamRequest.id)))

    def latest_approved(self, tg_id: int) -> TeamRequest | None:
        return self.s.scalar(select(TeamRequest).where(TeamRequest.tg_id == tg_id, TeamRequest.status == "approved")
                             .order_by(TeamRequest.id.desc()))


class SqlTeams(_Repo):
    def get(self, team_id: int) -> Team | None:
        return self.s.get(Team, team_id)

    def bot_exists(self, bot_id: int) -> bool:
        return self.s.scalar(select(Team.id).where(Team.bot_id == bot_id)) is not None

    def active(self) -> list[Team]:
        return list(self.s.scalars(select(Team).where(Team.active.is_(True)).order_by(Team.id)))

    def by_public_token(self, token: str) -> Team | None:
        return self.s.scalar(select(Team).where(Team.public_token == token, Team.active.is_(True)))

    def shares_account(self, team: Team) -> bool:
        """Another active team is paid into the same bank account (so each hears every transfer on it)."""
        if not team.bank_account:
            return False
        return self.s.scalar(select(Team.id).where(Team.id != team.id, Team.active.is_(True),
                                                   Team.bank_name == team.bank_name,
                                                   Team.bank_account == team.bank_account).limit(1)) is not None

    def for_admin(self, tg_id: int) -> list[tuple[Team, str]]:
        rows = self.s.execute(select(Team, TeamAdmin.role).join(TeamAdmin, TeamAdmin.team_id == Team.id)
                              .where(TeamAdmin.tg_id == tg_id, Team.active.is_(True)).order_by(Team.name))
        return [(t, role) for t, role in rows]


class SqlAdmins(_Repo):
    def get(self, team_id: int, tg_id: int) -> TeamAdmin | None:
        return self.s.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team_id, TeamAdmin.tg_id == tg_id))

    def role_of(self, team_id: int, tg_id: int) -> str | None:
        return self.s.scalar(select(TeamAdmin.role).where(TeamAdmin.team_id == team_id, TeamAdmin.tg_id == tg_id))

    def of_team(self, team_id: int) -> list[TeamAdmin]:
        return list(self.s.scalars(select(TeamAdmin).where(TeamAdmin.team_id == team_id)
                                   .order_by(TeamAdmin.role.desc(), TeamAdmin.id)))

    def remove(self, admin: TeamAdmin) -> None:
        self.s.delete(admin)


class SqlManagerRequests(_Repo):
    def get(self, request_id: int) -> ManagerRequest | None:
        return self.s.get(ManagerRequest, request_id)

    def has_pending(self, team_id: int, tg_id: int) -> bool:
        return self.s.scalar(select(ManagerRequest.id).where(
            ManagerRequest.team_id == team_id, ManagerRequest.tg_id == tg_id,
            ManagerRequest.status == "pending")) is not None

    def last_rejected_at(self, team_id: int, tg_id: int) -> datetime | None:
        return self.s.scalar(select(ManagerRequest.decided_at).where(
            ManagerRequest.team_id == team_id, ManagerRequest.tg_id == tg_id, ManagerRequest.status == "rejected")
            .order_by(ManagerRequest.decided_at.desc()))

    def pending(self, team_id: int) -> list[ManagerRequest]:
        return list(self.s.scalars(select(ManagerRequest).where(
            ManagerRequest.team_id == team_id, ManagerRequest.status == "pending").order_by(ManagerRequest.id)))

    def expire_created_before(self, cutoff: datetime) -> None:
        self.s.execute(update(ManagerRequest).where(ManagerRequest.status == "pending",
                                                    ManagerRequest.created_at < cutoff).values(status="expired"))


class SqlFollowers(_Repo):
    def get(self, team_id: int, tg_id: int) -> Follower | None:
        return self.s.scalar(select(Follower).where(Follower.team_id == team_id, Follower.tg_id == tg_id))

    def active_ids(self, team_id: int) -> list[int]:
        return list(self.s.scalars(select(Follower.tg_id).where(Follower.team_id == team_id,
                                                                Follower.muted.is_(False)).order_by(Follower.id)))

    def of_team(self, team_id: int) -> list[Follower]:
        return list(self.s.scalars(select(Follower).where(Follower.team_id == team_id).order_by(Follower.id)))


class SqlMembers(_Repo):
    def get(self, member_id: int) -> Member | None:
        return self.s.get(Member, member_id)

    def of_team(self, team_id: int, include_left: bool = False, active_only: bool = False,
                ids: list[int] | None = None) -> list[Member]:
        q = select(Member).where(Member.team_id == team_id)
        if active_only:
            q = q.where(Member.status == "active")
        elif not include_left:
            q = q.where(Member.status != "left")
        if ids is not None:
            q = q.where(Member.id.in_(ids))
        return list(self.s.scalars(q.order_by(Member.id)))

    def names(self, member_ids: set[int]) -> list[str]:
        if not member_ids:
            return []
        return list(self.s.scalars(select(Member.name).where(Member.id.in_(member_ids))))

    def count_by_team(self) -> dict[int, int]:
        rows = self.s.execute(select(Member.team_id, func.count(Member.id)).where(Member.status != "left")
                              .group_by(Member.team_id))
        return {team_id: n for team_id, n in rows}


class SqlBills(_Repo):
    def get(self, bill_id: int) -> Bill | None:
        return self.s.get(Bill, bill_id)

    def of_match(self, match_id: int) -> list[Bill]:
        return list(self.s.scalars(select(Bill).where(Bill.match_id == match_id).order_by(Bill.id)))

    def get_many(self, bill_ids: list[int], unpaid_only: bool = False, lock: bool = False) -> list[Bill]:
        """lock=True: hold these rows until commit, so two payments can't both pay one bill."""
        if not bill_ids:
            return []
        q = select(Bill).where(Bill.id.in_(bill_ids))
        if unpaid_only:
            q = q.where(Bill.status == "unpaid")
        if lock:
            q = _locked(q.order_by(Bill.id))
        return list(self.s.scalars(q))

    def unpaid_with_members(self, team_id: int, member_ids: list[int] | None = None,
                            lock: bool = False) -> list[tuple[Bill, Member]]:
        q = (select(Bill, Member).join(Member, Bill.member_id == Member.id)
             .where(Bill.team_id == team_id, Bill.status == "unpaid", Member.status.in_(["active", "paused"])))
        if member_ids is not None:
            q = q.where(Member.id.in_(member_ids))
        q = q.order_by(Bill.period, Bill.id)
        if lock:
            q = _locked(q, of=Bill)
        return [(b, m) for b, m in self.s.execute(q)]

    def fee_bills_since(self, team_id: int, since: str) -> list[tuple[Bill, Member]]:
        q = (select(Bill, Member).join(Member, Bill.member_id == Member.id)
             .where(Bill.team_id == team_id, Bill.kind == "monthly", Bill.period >= since))
        return [(b, m) for b, m in self.s.execute(q.order_by(Bill.period, Bill.id))]

    def late_of_team(self, team_id: int) -> list[Bill]:
        return list(self.s.scalars(select(Bill).where(Bill.team_id == team_id, Bill.kind == "late")))

    def unpaid_of_member(self, member_id: int) -> list[Bill]:
        return list(self.s.scalars(select(Bill).where(Bill.member_id == member_id, Bill.status == "unpaid")
                                   .order_by(Bill.period, Bill.id)))

    def of_team(self, team_id: int) -> list[Bill]:
        return list(self.s.scalars(select(Bill).where(Bill.team_id == team_id).order_by(Bill.period, Bill.id)))


class SqlFeePlans(_Repo):
    def get(self, plan_id: int) -> FeePlan | None:
        return self.s.get(FeePlan, plan_id)

    def of_team(self, team_id: int) -> list[FeePlan]:
        return list(self.s.scalars(select(FeePlan).where(FeePlan.team_id == team_id)
                                   .order_by(FeePlan.months, FeePlan.amount, FeePlan.id)))

    def remove(self, p: FeePlan) -> None:
        self.s.delete(p)


class SqlPlanMembers(_Repo):
    def of_team(self, team_id: int) -> list[PlanMember]:
        q = select(PlanMember).where(PlanMember.team_id == team_id)
        return list(self.s.scalars(q.order_by(PlanMember.member_id, PlanMember.start, PlanMember.id)))

    def of_plan(self, plan_id: int) -> list[PlanMember]:
        return list(self.s.scalars(select(PlanMember).where(PlanMember.plan_id == plan_id).order_by(PlanMember.id)))

    def of_member(self, member_id: int) -> list[PlanMember]:
        return list(self.s.scalars(select(PlanMember).where(PlanMember.member_id == member_id)
                                   .order_by(PlanMember.start, PlanMember.id)))

    def remove(self, x: PlanMember) -> None:
        self.s.delete(x)


class SqlIntents(_Repo):
    def get(self, intent_id: int) -> PaymentIntent | None:
        return self.s.get(PaymentIntent, intent_id)

    def code_exists(self, code: str) -> bool:
        return self.s.scalar(select(PaymentIntent.id).where(PaymentIntent.code == code)) is not None

    def find_by_codes(self, team_id: int, codes: list[str]) -> PaymentIntent | None:
        return self.s.scalar(select(PaymentIntent).where(PaymentIntent.team_id == team_id,
                                                         PaymentIntent.code.in_(codes)))

    def other_team_has(self, team_id: int, codes: list[str]) -> bool:
        """One of these codes is another team's (two teams sharing one bank account both hear each transfer)."""
        return self.s.scalar(select(PaymentIntent.id).where(PaymentIntent.team_id != team_id,
                                                            PaymentIntent.code.in_(codes)).limit(1)) is not None

    def members_of(self, intent_ids: list[int]) -> dict[int, list[str]]:
        if not intent_ids:
            return {}
        rows = self.s.execute(select(PaymentIntent.id, PaymentIntent.member_names).where(PaymentIntent.id.in_(intent_ids)))
        return {i: list(names or []) for i, names in rows}

    def since(self, team_id: int, since: datetime) -> list[PaymentIntent]:
        return list(self.s.scalars(select(PaymentIntent).where(PaymentIntent.team_id == team_id,
                                                               PaymentIntent.created_at >= since)))

    def expire_pending_before(self, now: datetime) -> None:
        self.s.execute(update(PaymentIntent).where(PaymentIntent.status == "pending", PaymentIntent.expires_at < now)
                       .values(status="expired"))


def _locked(q, of=None):
    """SELECT ... FOR UPDATE (ignored by SQLite), with already-loaded rows refreshed from what it read."""
    return q.with_for_update(of=of).execution_options(populate_existing=True)


def _between(q, col, start, end):
    if start is not None:
        q = q.where(col >= start)
    if end is not None:
        q = q.where(col < end)
    return q


class SqlPayments(_Repo):
    def get(self, payment_id: int, lock: bool = False) -> Payment | None:
        if lock:
            return self.s.scalar(_locked(select(Payment).where(Payment.id == payment_id)))
        return self.s.get(Payment, payment_id)

    def tx_exists(self, tx_id: str) -> bool:
        return self.s.scalar(select(Payment.id).where(Payment.sepay_tx_id == tx_id)) is not None

    def unmatched(self, team_id: int) -> list[Payment]:
        return list(self.s.scalars(select(Payment).where(Payment.team_id == team_id, Payment.status == "unmatched")
                                   .order_by(Payment.id)))

    def total(self, team_id: int, start=None, end=None, source: str | None = None) -> int:
        q = select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.team_id == team_id)
        if source is not None:
            q = q.where(Payment.source == source)
        return int(self.s.scalar(_between(q, Payment.created_at, start, end)))

    def last_transfer_at(self, team_id: int):
        """When SePay last reported a bank transfer for this team (None: never)."""
        return self.s.scalar(select(func.max(Payment.created_at)).where(Payment.team_id == team_id,
                                                                        Payment.sepay_tx_id.is_not(None)))

    def amounts_between(self, team_id: int, start, end) -> list[tuple]:
        """(created_at, amount) of every payment in [start, end): one query for many months' totals."""
        q = select(Payment.created_at, Payment.amount).where(Payment.team_id == team_id)
        return [tuple(r) for r in self.s.execute(_between(q, Payment.created_at, start, end))]

    def count_unmatched(self, team_id: int) -> int:
        return int(self.s.scalar(select(func.count(Payment.id)).where(
            Payment.team_id == team_id, Payment.status == "unmatched")))

    def latest(self, team_id: int, limit: int) -> list[Payment]:
        return list(self.s.scalars(select(Payment).where(Payment.team_id == team_id)
                                   .order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit)))

    def first_at(self, team_id: int):
        return self.s.scalar(select(func.min(Payment.created_at)).where(Payment.team_id == team_id))

    def between(self, team_id: int, start, end) -> list[Payment]:
        q = _between(select(Payment).where(Payment.team_id == team_id), Payment.created_at, start, end)
        return list(self.s.scalars(q.order_by(Payment.created_at.desc(), Payment.id.desc())))

    def of_team(self, team_id: int) -> list[Payment]:
        return list(self.s.scalars(select(Payment).where(Payment.team_id == team_id).order_by(Payment.created_at)))


class SqlExpenses(_Repo):
    def total(self, team_id: int, start=None, end=None) -> int:
        q = select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.team_id == team_id)
        return int(self.s.scalar(_between(q, Expense.created_at, start, end)))

    def latest(self, team_id: int, limit: int) -> list[Expense]:
        return list(self.s.scalars(select(Expense).where(Expense.team_id == team_id)
                                   .order_by(Expense.created_at.desc(), Expense.id.desc()).limit(limit)))

    def first_at(self, team_id: int):
        return self.s.scalar(select(func.min(Expense.created_at)).where(Expense.team_id == team_id))

    def amounts_between(self, team_id: int, start, end) -> list[tuple]:
        q = select(Expense.created_at, Expense.amount).where(Expense.team_id == team_id)
        return [tuple(r) for r in self.s.execute(_between(q, Expense.created_at, start, end))]

    def between(self, team_id: int, start, end) -> list[Expense]:
        q = _between(select(Expense).where(Expense.team_id == team_id), Expense.created_at, start, end)
        return list(self.s.scalars(q.order_by(Expense.created_at.desc(), Expense.id.desc())))

    def of_team(self, team_id: int) -> list[Expense]:
        return list(self.s.scalars(select(Expense).where(Expense.team_id == team_id).order_by(Expense.created_at)))


class SqlAudit(_Repo):
    def get(self, entry_id: int) -> AuditEntry | None:
        return self.s.get(AuditEntry, entry_id)

    def undoable_since(self, team_id: int, since: datetime) -> list[AuditEntry]:
        return list(self.s.scalars(select(AuditEntry).where(
            AuditEntry.team_id == team_id, AuditEntry.undo.is_not(None), AuditEntry.undone_at.is_(None),
            AuditEntry.created_at >= since).order_by(AuditEntry.id.desc()).limit(100)))

    def latest(self, team_id: int, limit: int) -> list[AuditEntry]:
        return list(self.s.scalars(select(AuditEntry).where(AuditEntry.team_id == team_id)
                                   .order_by(AuditEntry.id.desc()).limit(limit)))


class SqlJobRuns(_Repo):
    def exists(self, key: str) -> bool:
        return self.s.get(JobRun, key) is not None


class SqlMatches(_Repo):
    def get(self, match_id: int) -> Match | None:
        return self.s.get(Match, match_id)

    def lock(self, match_id: int) -> None:
        """Hold the match row until commit: one manager charges its players at a time."""
        self.s.execute(_locked(select(Match.id).where(Match.id == match_id)))

    def between(self, team_id: int, start, end, statuses: tuple = ("scheduled", "cancelled")) -> list[Match]:
        q = select(Match).where(Match.team_id == team_id, Match.day >= start, Match.day <= end,
                                Match.status.in_(statuses))
        return list(self.s.scalars(q.order_by(Match.day, Match.at, Match.id)))

    def from_day(self, team_id: int, start, limit: int) -> list[Match]:
        q = select(Match).where(Match.team_id == team_id, Match.day >= start, Match.status != "deleted")
        return list(self.s.scalars(q.order_by(Match.day, Match.at, Match.id).limit(limit)))

    def before(self, team_id: int, day, limit: int) -> list[Match]:
        q = select(Match).where(Match.team_id == team_id, Match.day < day, Match.status != "deleted")
        return list(self.s.scalars(q.order_by(Match.day.desc(), Match.at.desc(), Match.id.desc()).limit(limit)))

    def of_schedule(self, schedule_id: int) -> list[Match]:
        return list(self.s.scalars(select(Match).where(Match.schedule_id == schedule_id)))

    def all_of_team(self, team_id: int) -> list[Match]:
        q = select(Match).where(Match.team_id == team_id, Match.status != "deleted")
        return list(self.s.scalars(q.order_by(Match.day)))

    def remove(self, m: Match) -> None:
        self.s.delete(m)


class SqlMatchSchedules(_Repo):
    def get(self, schedule_id: int) -> MatchSchedule | None:
        return self.s.get(MatchSchedule, schedule_id)

    def of_team(self, team_id: int, active_only: bool = True) -> list[MatchSchedule]:
        q = select(MatchSchedule).where(MatchSchedule.team_id == team_id)
        if active_only:
            q = q.where(MatchSchedule.active.is_(True))
        return list(self.s.scalars(q.order_by(MatchSchedule.id)))


class SqlContacts(_Repo):
    def get(self, contact_id: int) -> Contact | None:
        return self.s.get(Contact, contact_id)

    def of_team(self, team_id: int) -> list[Contact]:
        return list(self.s.scalars(select(Contact).where(Contact.team_id == team_id).order_by(Contact.id)))

    def remove(self, c: Contact) -> None:
        self.s.delete(c)
