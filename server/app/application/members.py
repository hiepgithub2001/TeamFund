"""Use cases: the team roster. Only managers can see or change it (it holds phone numbers)."""
from ..domain.entities import Member, Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import MAX_FEE, MEMBER_FIELD_LIMITS, MEMBER_STATUSES
from . import billing
from .access import manager_team
from .billing import bill_dict
from .people import audit
from .ports import Deps, UnitOfWork


def _clean(field: str, value):
    if value is None:
        return None
    value = str(value).strip()
    if len(value) > MEMBER_FIELD_LIMITS[field]:
        raise invalid()
    return value or None


class MemberUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _dict(self, uow: UnitOfWork, team: Team, m: Member) -> dict:
        owes = sum(b.amount for b in uow.bills.unpaid_of_member(m.id))
        plans = billing.plans_of(uow, team)
        fee, months = billing.fee_of(team, m, plans, self.d.clock.current_period())
        on_plan = billing.plan_for(m, plans, self.d.clock.current_period()) is not None
        return {"id": m.id, "name": m.name, "nickname": m.nickname, "shirt_number": m.shirt_number,
                "phone": m.phone, "note": m.note, "fee_override": m.fee_override, "fee": fee, "months": months,
                "on_plan": on_plan, "status": m.status, "owes": owes}

    @staticmethod
    def _get(uow: UnitOfWork, team: Team, member_id: int) -> Member:
        m = uow.members.get(member_id)
        if not m or m.team_id != team.id:
            raise DomainError(404, "not_found")
        return m

    def list_members(self, team_id: int, actor: int, include_left: bool = False) -> list[dict]:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            members = sorted(uow.members.of_team(team.id, include_left=include_left), key=lambda m: m.name.casefold())
            return [self._dict(uow, team, m) for m in members]

    def add_members(self, team_id: int, actor: int, members: list[dict]) -> list[dict]:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            if not 1 <= len(members) <= 100:
                raise invalid()
            created = []
            for item in members:
                fields = {f: _clean(f, item.get(f)) for f in MEMBER_FIELD_LIMITS}
                if not fields["name"]:
                    raise invalid()
                m = Member(team_id=team.id, created_at=self.d.clock.now(), **fields)
                uow.members.add(m)
                created.append(m)
            uow.flush()
            audit(uow, self.d.clock, team.id, actor, "member.add", target=", ".join(m.name for m in created))
            uow.commit()
            return [self._dict(uow, team, m) for m in created]

    def get_member(self, team_id: int, actor: int, member_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            return self._dict(uow, team, self._get(uow, team, member_id))

    def update_member(self, team_id: int, actor: int, member_id: int, changes: dict) -> dict:
        """Change some fields; None clears a field (for fee_override: back to the team fee)."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            clean = self._validated(changes)
            m = self._get(uow, team, member_id)
            before = {k: getattr(m, k) for k in clean}
            for k, v in clean.items():
                setattr(m, k, v)
            if "fee_override" in clean:
                for x in uow.memberships.of_member(m.id):  # an own monthly fee takes the member off their plans
                    uow.memberships.remove(x)
                uow.flush()
                billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "member.update", target=m.name, before=before, after=clean)
            uow.commit()
            return self._dict(uow, team, m)

    @staticmethod
    def _validated(changes: dict) -> dict:
        clean = {}
        for k, v in changes.items():
            if k in MEMBER_FIELD_LIMITS:
                clean[k] = _clean(k, v)
            elif k == "fee_override":
                if v is not None and not (isinstance(v, int) and 0 <= v <= MAX_FEE):
                    raise invalid()
                clean[k] = v
            elif k == "status":
                if v not in MEMBER_STATUSES:
                    raise invalid()
                clean[k] = v
            else:
                raise invalid()
        if "name" in clean and not clean["name"]:
            raise invalid()
        return clean

    def remove_member(self, team_id: int, actor: int, member_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._get(uow, team, member_id)
            m.status = "left"
            audit(uow, self.d.clock, team.id, actor, "member.remove", target=m.name)
            uow.commit()
            return {"id": m.id, "name": m.name}

    def member_bills(self, team_id: int, actor: int, member_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._get(uow, team, member_id)
            return {"member": {"id": m.id, "name": m.name}, "bills": [bill_dict(b) for b in uow.bills.unpaid_of_member(m.id)]}
