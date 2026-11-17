from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_team, internal_auth, require_manager
from ..errors import AppError
from ..models import Bill, Member, Team
from ..services.audit import audit
from ..services.billing import bill_dict, effective_fee

router = APIRouter(prefix="/api/teams/{team_id}/members", dependencies=[Depends(internal_auth)])

EDITABLE = ("name", "nickname", "shirt_number", "phone", "note", "fee_override", "status")


def member_dict(db: Session, team: Team, m: Member) -> dict:
    owes = sum(db.scalars(select(Bill.amount).where(Bill.member_id == m.id, Bill.status == "unpaid")).all())
    return {"id": m.id, "name": m.name, "nickname": m.nickname, "shirt_number": m.shirt_number, "phone": m.phone,
            "note": m.note, "fee_override": m.fee_override, "fee": effective_fee(team, m), "status": m.status,
            "owes": owes}


def _get(db: Session, team: Team, member_id: int) -> Member:
    m = db.get(Member, member_id)
    if not m or m.team_id != team.id:
        raise AppError(404, "not_found")
    return m


@router.get("")
def list_members(include_left: bool = False, team: Team = Depends(get_team), db: Session = Depends(get_db)):
    q = select(Member).where(Member.team_id == team.id)
    if not include_left:
        q = q.where(Member.status != "left")
    members = sorted(db.scalars(q).all(), key=lambda m: m.name.casefold())
    return [member_dict(db, team, m) for m in members]


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    nickname: Optional[str] = Field(None, max_length=64)
    shirt_number: Optional[str] = Field(None, max_length=8)
    phone: Optional[str] = Field(None, max_length=32)
    note: Optional[str] = Field(None, max_length=255)


class MembersIn(BaseModel):
    members: list[MemberIn] = Field(min_length=1, max_length=100)


@router.post("")
def add_members(body: MembersIn, team: Team = Depends(get_team), actor: int = Depends(require_manager),
                db: Session = Depends(get_db)):
    created = []
    for item in body.members:
        m = Member(team_id=team.id, **{k: (v.strip() if isinstance(v, str) else v)
                                       for k, v in item.model_dump().items()})
        db.add(m)
        created.append(m)
    db.flush()
    audit(db, team.id, actor, "member.add", target=", ".join(m.name for m in created))
    db.commit()
    return [member_dict(db, team, m) for m in created]


@router.get("/{member_id}")
def get_member(member_id: int, team: Team = Depends(get_team), db: Session = Depends(get_db)):
    return member_dict(db, team, _get(db, team, member_id))


class MemberPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    nickname: Optional[str] = Field(None, max_length=64)
    shirt_number: Optional[str] = Field(None, max_length=8)
    phone: Optional[str] = Field(None, max_length=32)
    note: Optional[str] = Field(None, max_length=255)
    fee_override: Optional[int] = Field(None, ge=0, le=100_000_000)
    status: Optional[Literal["active", "paused", "left"]] = None


@router.patch("/{member_id}")
def update_member(member_id: int, body: MemberPatch, team: Team = Depends(get_team),
                  actor: int = Depends(require_manager), db: Session = Depends(get_db)):
    m = _get(db, team, member_id)
    changes = body.model_dump(exclude_unset=True)  # explicit null clears a field
    if "name" in changes and not changes["name"]:
        raise AppError(422, "invalid")
    before = {k: getattr(m, k) for k in changes}
    for k, v in changes.items():
        setattr(m, k, v.strip() if isinstance(v, str) else v)
    audit(db, team.id, actor, "member.update", target=m.name, before=before, after=changes)
    db.commit()
    return member_dict(db, team, m)


@router.delete("/{member_id}")
def remove_member(member_id: int, team: Team = Depends(get_team), actor: int = Depends(require_manager),
                  db: Session = Depends(get_db)):
    m = _get(db, team, member_id)
    m.status = "left"
    audit(db, team.id, actor, "member.remove", target=m.name)
    db.commit()
    return {"id": m.id, "name": m.name}


@router.get("/{member_id}/bills")
def member_bills(member_id: int, team: Team = Depends(get_team), db: Session = Depends(get_db)):
    m = _get(db, team, member_id)
    bills = db.scalars(select(Bill).where(Bill.member_id == m.id, Bill.status == "unpaid")
                       .order_by(Bill.period, Bill.id)).all()
    return {"member": {"id": m.id, "name": m.name}, "bills": [bill_dict(b) for b in bills]}
