"""Use cases: fee plans. Managers put members on a plan (amount + cycle, e.g. 300,000đ per quarter);
everyone else pays the team fee (or their own monthly fee) every month.

A member can be on several plans, each for whole periods (PlanMember: from the period they're put on it,
for good, unless a manager sets other periods), but never two at the same time: an overlap is refused with
the clashing plans named. Months no plan covers are paid on the default package. Bills follow standard periods (quarters Jan–Mar … Oct–Dec, half-years,
years): one bill covers the period. Months a member already paid aren't billed again; the rest of the period
is priced pro rata. Changing a plan reprices the unpaid bill covering this month (like the default fee)."""
from ..domain.entities import FeePlan, PlanMember, Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import (CYCLES, MAX_DUE_DAYS, MAX_FEE, MAX_LATE_FEE, cycle_start, is_period, on_plan, overlaps,
                            period_end, terms)
from . import billing
from .access import manager_team
from .people import audit
from .ports import Deps, UnitOfWork
from .teams import TeamUseCases

MAX_PLANS = 20


def _plan(uow: UnitOfWork, team: Team, plan_id: int) -> FeePlan:
    p = uow.plans.get(plan_id) if isinstance(plan_id, int) else None
    if not p or p.team_id != team.id:
        raise DomainError(404, "not_found")
    return p


def _label(p: FeePlan) -> str:
    return f"{p.amount}/{p.months}m"


def _clashes(uow: UnitOfWork, team: Team, member_ids: set, plan_id: int, start, until) -> list[dict]:
    """Memberships on other plans that share a month with start..until, for these members."""
    plans = {p.id: p for p in uow.plans.of_team(team.id)}
    names = {m.id: m.name for m in uow.members.of_team(team.id, include_left=True)}
    return [{"member_id": x.member_id, "member": names.get(x.member_id, "?"), "plan_id": x.plan_id,
             "amount": plans[x.plan_id].amount, "months": plans[x.plan_id].months, "from": x.start, "until": x.until}
            for x in uow.memberships.of_team(team.id)
            if x.member_id in member_ids and x.plan_id != plan_id and x.plan_id in plans
            and overlaps(x.start, x.until, start, until)]


def overlap_error(clashes: list[dict]) -> DomainError:
    return DomainError(409, "plan_overlap", names=", ".join(sorted({c["member"] for c in clashes})), clashes=clashes)


class PlanUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def list_plans(self, team_id: int, actor: int) -> dict:
        """The team fee and every plan with its members (those who haven't left)."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            return self._view(uow, team)

    def _view(self, uow: UnitOfWork, team: Team) -> dict:
        members = sorted(uow.members.of_team(team.id), key=lambda m: m.name.casefold())
        period = self.d.clock.current_period()
        plans = uow.plans.of_team(team.id)
        ids = {p.id for p in plans}
        ms = uow.memberships.of_team(team.id)
        by_id = {m.id: m for m in members}
        on = lambda pid: sorted(({"id": x.member_id, "name": by_id[x.member_id].name, "from": x.start,  # noqa: E731
                                  "until": x.until} for x in ms if x.plan_id == pid and x.member_id in by_id),
                                key=lambda r: r["name"].casefold())
        now_on = {x.member_id for x in ms if x.plan_id in ids and on_plan(x, period)}

        def tm(plan=None) -> dict:
            t = terms(team, plan)
            return {"notify_day": t.notify_day, "due_days": t.due_days, "late_fee": t.late_fee,
                    "own_schedule": plan is None or bool(plan.notify_day),
                    "own_late": plan is None or plan.late_fee is not None}
        return {"fee": team.fee, "fee_months": team.fee_months or 1, "default": tm(), "period": period,
                "default_members": [{"id": m.id, "name": m.name} for m in members if m.id not in now_on],
                "plans": [{"id": p.id, "amount": p.amount, "months": p.months, "members": on(p.id), "period": period,
                           **tm(p)}
                          for p in plans]}

    def get_plan(self, team_id: int, actor: int, plan_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            p = _plan(uow, team, plan_id)
            return next(x for x in self._view(uow, team)["plans"] if x["id"] == p.id)

    def save_plan(self, team_id: int, actor: int, amount: int, months: int, member_ids: list,
                  plan_id: int | None = None) -> dict:
        """Create a plan (or change one) and put exactly these members on it: a member who joins gets it from
        this period, for good, unless that overlaps another plan of theirs; those are left out and returned as
        `conflicts` (the bot asks for other periods). Members left out of an edited plan come off it. A new plan
        with the same amount and cycle as an existing one joins it. Plans nobody is on any more are deleted."""
        if (not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= MAX_FEE
                or months not in CYCLES or not isinstance(member_ids, list) or not 1 <= len(member_ids) <= 200
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in member_ids)):
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            plans = uow.plans.of_team(team.id)
            if plan_id is not None:
                plan = _plan(uow, team, plan_id)
            else:
                plan = next((p for p in plans if p.amount == amount and p.months == months), None)
                if plan is None:
                    if len(plans) >= MAX_PLANS:
                        raise DomainError(400, "too_many_plans")
                    plan = FeePlan(team_id=team.id, amount=amount, months=months, created_at=self.d.clock.now())
                    uow.plans.add(plan)
                    uow.flush()
            before = {"amount": plan.amount, "months": plan.months} if plan_id is not None else None
            plan.amount, plan.months = amount, months
            chosen = uow.members.of_team(team.id, ids=list(dict.fromkeys(member_ids)))
            if len(chosen) != len(set(member_ids)):
                raise DomainError(404, "not_found")
            picked = {m.id for m in chosen}
            start = cycle_start(self.d.clock.current_period(), months)
            on_it = {x.member_id: x for x in uow.memberships.of_plan(plan.id)}
            joining = picked - set(on_it)
            conflicts = _clashes(uow, team, joining, plan.id, start, None)
            blocked = {c["member_id"] for c in conflicts}
            for mid in joining - blocked:  # joins: from this period, for good
                uow.memberships.add(PlanMember(team_id=team.id, plan_id=plan.id, member_id=mid, start=start,
                                               created_at=self.d.clock.now()))
            if plan_id is not None:
                for mid, x in on_it.items():
                    if mid not in picked:
                        uow.memberships.remove(x)
            uow.flush()
            self._drop_empty(uow, team, keep=plan.id if conflicts else None)
            billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "plan.save", target=_label(plan), before=before,
                  after={"amount": amount, "months": months,
                         "members": sorted(m.name for m in chosen if m.id not in blocked)})
            uow.commit()
            billing.ensure_bills(uow, self.d, team)
            billing.update_late_fees(uow, self.d, team)
            return {**next(x for x in self._view(uow, team)["plans"] if x["id"] == plan.id), "conflicts": conflicts}

    def set_window(self, team_id: int, actor: int, plan_id: int, member_ids: list, start: str | None,
                   until: str | None) -> dict:
        """The whole periods a plan applies to these members: from the period starting `start` to the one
        ending `until` (YYYY-MM, both inclusive; until None: for good), e.g. 2027-01..2027-06 for 2027Q1–Q2.
        A member not on the plan yet joins it for those periods. Refused, with the clashing plans named, if it
        would overlap another plan of theirs. This month's unpaid bill follows at once."""
        if (not is_period(start) or (until is not None and (not is_period(until) or until < start))
                or not isinstance(member_ids, list) or not 1 <= len(member_ids) <= 200
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in member_ids)):
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            plan = _plan(uow, team, plan_id)
            if start != cycle_start(start, plan.months) or (until and until != period_end(until, plan.months)):
                raise invalid()  # whole periods only
            chosen = uow.members.of_team(team.id, ids=list(dict.fromkeys(member_ids)))
            if len(chosen) != len(set(member_ids)):
                raise DomainError(404, "not_found")
            clashes = _clashes(uow, team, {m.id for m in chosen}, plan.id, start, until)
            if clashes:
                raise overlap_error(clashes)
            on_it = {x.member_id: x for x in uow.memberships.of_plan(plan.id)}
            for m in chosen:
                if m.id in on_it:
                    on_it[m.id].start, on_it[m.id].until = start, until
                else:
                    uow.memberships.add(PlanMember(team_id=team.id, plan_id=plan.id, member_id=m.id, start=start,
                                                   until=until, created_at=self.d.clock.now()))
            uow.flush()
            billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "plan.window", target=_label(plan),
                  after={"members": sorted(m.name for m in chosen), "from": start, "until": until})
            uow.commit()
            billing.ensure_bills(uow, self.d, team)
            billing.update_late_fees(uow, self.d, team)
            return next(x for x in self._view(uow, team)["plans"] if x["id"] == plan.id)

    def set_terms(self, team_id: int, actor: int, plan_id: int | None, schedule: tuple[int, int] | None = None,
                  late_fee: int | None = None, inherit_schedule: bool = False, inherit_late: bool = False) -> dict:
        """A package's deadline (announcement day, days until due) and late fee per overdue day.
        plan_id None is the default package (the team's). A plan can go back to the default's with inherit_*."""
        if schedule is not None and not (isinstance(schedule, tuple) and len(schedule) == 2
                                         and 1 <= schedule[0] <= 28 and 0 <= schedule[1] <= MAX_DUE_DAYS):
            raise invalid()
        if late_fee is not None and not (isinstance(late_fee, int) and 0 <= late_fee <= MAX_LATE_FEE):
            raise invalid()
        if plan_id is None:
            if inherit_schedule or inherit_late:
                raise invalid()
            kw = {}
            if schedule:
                kw["notify_day"], kw["due_days"] = schedule
            if late_fee is not None:
                kw["late_fee"] = late_fee
            TeamUseCases(self.d).update_settings(team_id, actor, **kw)
            return self.list_plans(team_id, actor)["default"]
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            p = _plan(uow, team, plan_id)
            before = {"notify_day": p.notify_day, "due_days": p.due_days, "late_fee": p.late_fee}
            if inherit_schedule:
                p.notify_day = p.due_days = None
            elif schedule:
                p.notify_day, p.due_days = schedule
            if inherit_late:
                p.late_fee = p.late_fee_since = None
            elif late_fee is not None:
                prev, today = terms(team, p), self.d.clock.local_now().date()
                if late_fee and not prev.late_fee:
                    p.late_fee_since = today  # turned on now: fines count from today
                elif late_fee and p.late_fee is None:
                    p.late_fee_since = prev.late_since or today  # was following the default's: same start
                p.late_fee = late_fee
            audit(uow, self.d.clock, team.id, actor, "plan.terms", target=_label(p), before=before,
                  after={"notify_day": p.notify_day, "due_days": p.due_days, "late_fee": p.late_fee})
            uow.commit()
            billing.update_late_fees(uow, self.d, team)
            return next(x for x in self._view(uow, team)["plans"] if x["id"] == p.id)

    def delete_plan(self, team_id: int, actor: int, plan_id: int) -> dict:
        """Members on the plan go back to the team fee (or their own monthly fee)."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            plan = _plan(uow, team, plan_id)
            names = {m.id: m for m in uow.members.of_team(team.id, include_left=True)}
            moved = [names[x.member_id] for x in uow.memberships.of_plan(plan.id) if x.member_id in names]
            for x in uow.memberships.of_plan(plan.id):
                uow.memberships.remove(x)
            uow.flush()  # memberships go before the plan they point to
            label, info = _label(plan), {"amount": plan.amount, "months": plan.months}
            uow.plans.remove(plan)
            uow.flush()
            billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "plan.delete", target=label, before=info)
            uow.commit()
            billing.ensure_bills(uow, self.d, team)
            return {**info, "members": [m.name for m in moved if m.status != "left"]}

    def remove_members(self, team_id: int, actor: int, plan_id: int, member_ids: list) -> dict:
        """Take these members off the plan (their other plans stay). A plan nobody is on is deleted."""
        if not isinstance(member_ids, list) or not member_ids:
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            plan = _plan(uow, team, plan_id)
            gone = [x for x in uow.memberships.of_plan(plan.id) if x.member_id in set(member_ids)]
            if not gone:
                raise DomainError(404, "not_found")
            names = {m.id: m.name for m in uow.members.of_team(team.id, include_left=True)}
            for x in gone:
                uow.memberships.remove(x)
            uow.flush()
            label = _label(plan)
            self._drop_empty(uow, team)
            billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "plan.save", target=label,
                  after={"removed": sorted(names.get(x.member_id, "?") for x in gone)})
            uow.commit()
            billing.ensure_bills(uow, self.d, team)
            plan_now = uow.plans.get(plan.id)
            return next((x for x in self._view(uow, team)["plans"] if x["id"] == plan.id), None) if plan_now else None

    @staticmethod
    def _drop_empty(uow: UnitOfWork, team: Team, keep: int | None = None) -> None:
        """Delete plans nobody is on (except `keep`: a new plan whose members all still need periods)."""
        used = {x.plan_id for x in uow.memberships.of_team(team.id)}
        for p in uow.plans.of_team(team.id):
            if p.id not in used and p.id != keep:
                uow.plans.remove(p)
