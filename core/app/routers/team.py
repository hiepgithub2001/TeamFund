"""Team settings, roles, manager requests and audit log."""
import secrets
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_actor, get_team, internal_auth, is_super, require_actor, require_manager, require_owner, role_of
from ..errors import AppError
from ..models import AuditLog, ManagerRequest, Team, TeamAdmin, utcnow
from ..services.audit import audit, display_name, user_info
from ..services.billing import reprice_current_month
from ..services.crypto import hash_key
from ..services.timeutil import to_local_str

router = APIRouter(prefix="/api/teams/{team_id}", dependencies=[Depends(internal_auth)])

REQUEST_COOLDOWN = timedelta(hours=24)


def _sepay_url(team: Team) -> str:
    return f"{settings.public_url}/sepay/{team.id}"


@router.get("")
def team_info(team: Team = Depends(get_team), actor: Optional[int] = Depends(get_actor),
              db: Session = Depends(get_db)):
    return {"team_id": team.id, "name": team.name, "bot_username": team.bot_username, "fee": team.fee,
            "due_day": team.due_day, "lang": team.lang, "bank_name": team.bank_name,
            "bank_account": team.bank_account, "bank_holder": team.bank_holder,
            "sepay_connected": bool(team.sepay_key_hash), "sepay_url": _sepay_url(team),
            "group_chat_id": team.group_chat_id, "role": role_of(db, team.id, actor)}


@router.get("/role")
def my_role(team: Team = Depends(get_team), actor: Optional[int] = Depends(get_actor),
            db: Session = Depends(get_db)):
    return {"role": role_of(db, team.id, actor), "is_super": is_super(actor)}


class SettingsIn(BaseModel):
    fee: Optional[int] = Field(None, ge=0, le=100_000_000)
    due_day: Optional[int] = Field(None, ge=1, le=28)
    lang: Optional[Literal["en", "vi"]] = None


@router.patch("/settings")
def update_settings(body: SettingsIn, team: Team = Depends(get_team), actor: int = Depends(require_manager),
                    db: Session = Depends(get_db)):
    before = {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(team, k, v)
    if "fee" in changes:
        reprice_current_month(db, team)
    audit(db, team.id, actor, "team.settings", before=before, after=changes)
    db.commit()
    return {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}


class BankIn(BaseModel):
    bank_name: str = Field(min_length=2, max_length=64)
    bank_account: str = Field(min_length=4, max_length=32, pattern=r"^[0-9A-Za-z]+$")
    bank_holder: str = Field(min_length=2, max_length=128)


@router.put("/bank")
def set_bank(body: BankIn, team: Team = Depends(get_team), actor: int = Depends(require_owner),
             db: Session = Depends(get_db)):
    before = {"bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder}
    team.bank_name, team.bank_account = body.bank_name.strip(), body.bank_account.strip()
    team.bank_holder = body.bank_holder.strip().upper()
    after = {"bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder}
    audit(db, team.id, actor, "team.bank", before=before, after=after)
    db.commit()
    return {**after, "by": display_name(db, actor), "group_chat_id": team.group_chat_id}


@router.get("/sepay")
def sepay_info(team: Team = Depends(get_team), _: int = Depends(require_manager)):
    return {"webhook_url": _sepay_url(team), "connected": bool(team.sepay_key_hash)}


@router.post("/sepay-key")
def new_sepay_key(team: Team = Depends(get_team), actor: int = Depends(require_owner),
                  db: Session = Depends(get_db)):
    key = secrets.token_urlsafe(24)
    team.sepay_key_hash = hash_key(key)
    audit(db, team.id, actor, "team.sepay_key")
    db.commit()
    return {"api_key": key, "webhook_url": _sepay_url(team)}


class GroupIn(BaseModel):
    chat_id: int


@router.put("/group")
def link_group(body: GroupIn, team: Team = Depends(get_team), actor: int = Depends(require_manager),
               db: Session = Depends(get_db)):
    before = team.group_chat_id
    team.group_chat_id = body.chat_id
    audit(db, team.id, actor, "team.group", before={"chat_id": before}, after={"chat_id": body.chat_id})
    db.commit()
    return {"group_chat_id": team.group_chat_id}


# ---- admins ---------------------------------------------------------------------------------

@router.get("/admins")
def list_admins(team: Team = Depends(get_team), db: Session = Depends(get_db)):
    rows = db.scalars(select(TeamAdmin).where(TeamAdmin.team_id == team.id)
                      .order_by(TeamAdmin.role.desc(), TeamAdmin.id)).all()
    return [{**user_info(db, a.tg_id), "role": a.role} for a in rows]


@router.delete("/admins/{tg_id}")
def remove_admin(tg_id: int, team: Team = Depends(get_team), actor: int = Depends(require_owner),
                 db: Session = Depends(get_db)):
    a = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == tg_id))
    if not a or a.role != "manager":
        raise AppError(404, "not_a_manager")
    name = display_name(db, tg_id)
    db.delete(a)
    audit(db, team.id, actor, "admin.remove", target=name)
    db.commit()
    return {"tg_id": tg_id, "name": name}


class TransferIn(BaseModel):
    tg_id: int


@router.post("/transfer-owner")
def transfer_owner(body: TransferIn, team: Team = Depends(get_team), actor: int = Depends(require_owner),
                   db: Session = Depends(get_db)):
    target = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == body.tg_id))
    if not target or target.role != "manager":
        raise AppError(404, "not_a_manager")
    me = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == actor))
    me.role, target.role, team.owner_tg_id = "manager", "owner", body.tg_id
    name = display_name(db, body.tg_id)
    audit(db, team.id, actor, "team.transfer_owner", target=name)
    db.commit()
    return {"tg_id": body.tg_id, "name": name, "previous_owner": actor}


# ---- manager requests ---------------------------------------------------------------------

def _mreq_dict(db: Session, r: ManagerRequest, team: Team) -> dict:
    return {"id": r.id, "status": r.status, **user_info(db, r.tg_id), "owner_tg_id": team.owner_tg_id,
            "created_at": to_local_str(r.created_at)}


@router.post("/manager-requests")
def request_manager(team: Team = Depends(get_team), actor: int = Depends(require_actor),
                    db: Session = Depends(get_db)):
    if role_of(db, team.id, actor):
        raise AppError(409, "already_manager")
    if db.scalar(select(ManagerRequest.id).where(ManagerRequest.team_id == team.id,
                                                 ManagerRequest.tg_id == actor, ManagerRequest.status == "pending")):
        raise AppError(409, "request_pending")
    last_reject = db.scalar(select(ManagerRequest.decided_at).where(
        ManagerRequest.team_id == team.id, ManagerRequest.tg_id == actor, ManagerRequest.status == "rejected")
        .order_by(ManagerRequest.decided_at.desc()))
    if last_reject and utcnow() - last_reject < REQUEST_COOLDOWN:
        raise AppError(429, "cooldown", until=to_local_str(last_reject + REQUEST_COOLDOWN))
    r = ManagerRequest(team_id=team.id, tg_id=actor)
    db.add(r)
    db.commit()
    return _mreq_dict(db, r, team)


@router.get("/manager-requests")
def pending_manager_requests(team: Team = Depends(get_team), _: int = Depends(require_owner),
                             db: Session = Depends(get_db)):
    rows = db.scalars(select(ManagerRequest).where(ManagerRequest.team_id == team.id,
                                                   ManagerRequest.status == "pending").order_by(ManagerRequest.id)).all()
    return [_mreq_dict(db, r, team) for r in rows]


class DecideIn(BaseModel):
    approve: bool


@router.post("/manager-requests/{request_id}/decide")
def decide_manager_request(request_id: int, body: DecideIn, team: Team = Depends(get_team),
                           actor: int = Depends(require_owner), db: Session = Depends(get_db)):
    r = db.get(ManagerRequest, request_id)
    if not r or r.team_id != team.id:
        raise AppError(404, "not_found")
    if r.status != "pending":
        raise AppError(409, "already_decided", status=r.status)
    r.status, r.decided_at = ("approved" if body.approve else "rejected"), utcnow()
    if body.approve and not role_of(db, team.id, r.tg_id):
        db.add(TeamAdmin(team_id=team.id, tg_id=r.tg_id, role="manager", added_by=actor))
    audit(db, team.id, actor, "manager_request." + r.status, target=display_name(db, r.tg_id))
    db.commit()
    return _mreq_dict(db, r, team)


@router.get("/audit")
def audit_log(limit: int = 20, team: Team = Depends(get_team), _: int = Depends(require_owner),
              db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.team_id == team.id)
                      .order_by(AuditLog.id.desc()).limit(min(limit, 100))).all()
    return [{"at": to_local_str(a.created_at), "actor": display_name(db, a.actor_tg_id), "action": a.action,
             "target": a.target, "before": a.before, "after": a.after} for a in rows]
