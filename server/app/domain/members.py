"""The team roster. Only managers can see or change it (it holds phone numbers)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bill, Member, Team
from .access import manager_team
from .audit import audit
from .billing import bill_dict, effective_fee
from .errors import DomainError, invalid

LIMITS = {"name": 100, "nickname": 64, "shirt_number": 8, "phone": 32, "note": 255}
STATUSES = ("active", "paused", "left")


def member_dict(db: Session, team: Team, m: Member) -> dict:
    owes = sum(db.scalars(select(Bill.amount).where(Bill.member_id == m.id, Bill.status == "unpaid")).all())
    return {"id": m.id, "name": m.name, "nickname": m.nickname, "shirt_number": m.shirt_number, "phone": m.phone,
            "note": m.note, "fee_override": m.fee_override, "fee": effective_fee(team, m), "status": m.status,
            "owes": owes}


def _get(db: Session, team: Team, member_id: int) -> Member:
    m = db.get(Member, member_id)
    if not m or m.team_id != team.id:
        raise DomainError(404, "not_found")
    return m


def _clean(field: str, value):
    if value is None:
        return None
    value = str(value).strip()
    if len(value) > LIMITS[field]:
        raise invalid()
    return value or None


def list_members(db: Session, team_id: int, actor: int, include_left: bool = False) -> list[dict]:
    team = manager_team(db, team_id, actor)
    q = select(Member).where(Member.team_id == team.id)
    if not include_left:
        q = q.where(Member.status != "left")
    return [member_dict(db, team, m) for m in sorted(db.scalars(q).all(), key=lambda m: m.name.casefold())]


def add_members(db: Session, team_id: int, actor: int, members: list[dict]) -> list[dict]:
    team = manager_team(db, team_id, actor)
    if not 1 <= len(members) <= 100:
        raise invalid()
    created = []
    for item in members:
        fields = {f: _clean(f, item.get(f)) for f in LIMITS}
        if not fields["name"]:
            raise invalid()
        m = Member(team_id=team.id, **fields)
        db.add(m)
        created.append(m)
    db.flush()
    audit(db, team.id, actor, "member.add", target=", ".join(m.name for m in created))
    db.commit()
    return [member_dict(db, team, m) for m in created]


def get_member(db: Session, team_id: int, actor: int, member_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    return member_dict(db, team, _get(db, team, member_id))


def update_member(db: Session, team_id: int, actor: int, member_id: int, changes: dict) -> dict:
    """Change some fields; a None value clears that field (or, for fee_override, returns to the team fee)."""
    team = manager_team(db, team_id, actor)
    m = _get(db, team, member_id)
    clean = {}
    for k, v in changes.items():
        if k in LIMITS:
            clean[k] = _clean(k, v)
        elif k == "fee_override":
            if v is not None and not (isinstance(v, int) and 0 <= v <= 100_000_000):
                raise invalid()
            clean[k] = v
        elif k == "status":
            if v not in STATUSES:
                raise invalid()
            clean[k] = v
        else:
            raise invalid()
    if "name" in clean and not clean["name"]:
        raise invalid()
    before = {k: getattr(m, k) for k in clean}
    for k, v in clean.items():
        setattr(m, k, v)
    audit(db, team.id, actor, "member.update", target=m.name, before=before, after=clean)
    db.commit()
    return member_dict(db, team, m)


def remove_member(db: Session, team_id: int, actor: int, member_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    m = _get(db, team, member_id)
    m.status = "left"
    audit(db, team.id, actor, "member.remove", target=m.name)
    db.commit()
    return {"id": m.id, "name": m.name}


def member_bills(db: Session, team_id: int, actor: int, member_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    m = _get(db, team, member_id)
    bills = db.scalars(select(Bill).where(Bill.member_id == m.id, Bill.status == "unpaid")
                       .order_by(Bill.period, Bill.id)).all()
    return {"member": {"id": m.id, "name": m.name}, "bills": [bill_dict(b) for b in bills]}
