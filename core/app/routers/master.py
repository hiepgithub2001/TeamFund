"""Users, team registration requests and team creation (used by the master bot)."""
import secrets
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import internal_auth, is_super, require_actor, require_super
from ..errors import AppError
from ..models import Member, Team, TeamAdmin, TeamRequest, TgUser, utcnow
from ..services.audit import audit, display_name, user_info
from ..services.crypto import decrypt, encrypt
from ..services.timeutil import to_local_str

router = APIRouter(prefix="/api", dependencies=[Depends(internal_auth)])


class UserIn(BaseModel):
    tg_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None


@router.post("/users/upsert")
def upsert_user(body: UserIn, db: Session = Depends(get_db)):
    user = db.get(TgUser, body.tg_id)
    if not user:
        user = TgUser(tg_id=body.tg_id)
        db.add(user)
    user.first_name, user.last_name, user.username = body.first_name, body.last_name, body.username
    user.updated_at = utcnow()
    db.commit()
    return {"tg_id": user.tg_id, "lang": user.lang, "is_super": is_super(user.tg_id)}


@router.get("/users/{tg_id}")
def get_user(tg_id: int, db: Session = Depends(get_db)):
    user = db.get(TgUser, tg_id)
    return {"tg_id": tg_id, "lang": user.lang if user else None}


class LangIn(BaseModel):
    lang: Literal["en", "vi"]


@router.put("/users/{tg_id}/lang")
def set_user_lang(tg_id: int, body: LangIn, db: Session = Depends(get_db)):
    user = db.get(TgUser, tg_id) or TgUser(tg_id=tg_id)
    user.lang = body.lang
    db.add(user)
    db.commit()
    return {"tg_id": tg_id, "lang": user.lang}


# ---- team registration requests -------------------------------------------------------------

def _request_dict(db: Session, r: TeamRequest) -> dict:
    return {"id": r.id, "team_name": r.team_name, "status": r.status, **user_info(db, r.tg_id),
            "created_at": to_local_str(r.created_at)}


class TeamRequestIn(BaseModel):
    team_name: str = Field(min_length=1, max_length=100)


@router.post("/team-requests")
def create_team_request(body: TeamRequestIn, actor: int = Depends(require_actor), db: Session = Depends(get_db)):
    if db.scalar(select(TeamRequest.id).where(TeamRequest.tg_id == actor, TeamRequest.status == "pending")):
        raise AppError(409, "request_pending")
    r = TeamRequest(tg_id=actor, team_name=body.team_name.strip())
    db.add(r)
    db.commit()
    return {**_request_dict(db, r), "admin_ids": sorted(settings.master_admin_ids)}


@router.get("/team-requests")
def list_team_requests(_: int = Depends(require_super), db: Session = Depends(get_db)):
    rows = db.scalars(select(TeamRequest).where(TeamRequest.status == "pending").order_by(TeamRequest.id)).all()
    return [_request_dict(db, r) for r in rows]


class DecideIn(BaseModel):
    approve: bool


@router.post("/team-requests/{request_id}/decide")
def decide_team_request(request_id: int, body: DecideIn, actor: int = Depends(require_super),
                        db: Session = Depends(get_db)):
    r = db.get(TeamRequest, request_id)
    if not r:
        raise AppError(404, "not_found")
    if r.status != "pending":
        raise AppError(409, "already_decided", status=r.status)
    r.status = "approved" if body.approve else "rejected"
    r.decided_by, r.decided_at = actor, utcnow()
    audit(db, None, actor, "team_request." + r.status, target=r.team_name)
    db.commit()
    return _request_dict(db, r)


@router.get("/team-requests/approved")
def my_approved_request(actor: int = Depends(require_actor), db: Session = Depends(get_db)):
    r = db.scalar(select(TeamRequest).where(TeamRequest.tg_id == actor, TeamRequest.status == "approved")
                  .order_by(TeamRequest.id.desc()))
    return {"request": _request_dict(db, r) if r else None, "is_super": is_super(actor)}


# ---- teams -----------------------------------------------------------------------------------

def bot_payload(t: Team) -> dict:
    return {"team_id": t.id, "name": t.name, "bot_id": t.bot_id, "bot_username": t.bot_username,
            "token": decrypt(t.bot_token_enc), "webhook_secret": t.webhook_secret, "lang": t.lang,
            "group_chat_id": t.group_chat_id}


class TeamIn(BaseModel):
    token: str
    bot_id: int
    bot_username: str
    name: Optional[str] = None


@router.post("/teams")
def create_team(body: TeamIn, actor: int = Depends(require_actor), db: Session = Depends(get_db)):
    req = db.scalar(select(TeamRequest).where(TeamRequest.tg_id == actor, TeamRequest.status == "approved")
                    .order_by(TeamRequest.id.desc()))
    if not req and not is_super(actor):
        raise AppError(403, "not_approved")
    if db.scalar(select(Team.id).where(Team.bot_id == body.bot_id)):
        raise AppError(409, "bot_exists")
    name = (req.team_name if req else None) or body.name or body.bot_username
    team = Team(name=name, bot_id=body.bot_id, bot_username=body.bot_username, bot_token_enc=encrypt(body.token),
                webhook_secret=secrets.token_urlsafe(32), owner_tg_id=actor, lang=settings.default_lang)
    db.add(team)
    db.flush()
    db.add(TeamAdmin(team_id=team.id, tg_id=actor, role="owner", added_by=actor))
    if req:
        req.status = "used"
    audit(db, team.id, actor, "team.create", target=name)
    db.commit()
    return bot_payload(team)


@router.get("/teams/mine")
def my_teams(actor: int = Depends(require_actor), db: Session = Depends(get_db)):
    rows = db.execute(select(Team, TeamAdmin.role).join(TeamAdmin, TeamAdmin.team_id == Team.id)
                      .where(TeamAdmin.tg_id == actor, Team.active.is_(True)).order_by(Team.name)).all()
    return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username, "role": role} for t, role in rows]


@router.get("/teams")
def all_teams(_: int = Depends(require_super), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Member.team_id, func.count(Member.id))
                             .where(Member.status != "left").group_by(Member.team_id)).all())
    teams = db.scalars(select(Team).where(Team.active.is_(True)).order_by(Team.id)).all()
    return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username,
             "owner": display_name(db, t.owner_tg_id), "members": counts.get(t.id, 0)} for t in teams]


@router.get("/internal/bots")
def internal_bots(db: Session = Depends(get_db)):
    return [bot_payload(t) for t in db.scalars(select(Team).where(Team.active.is_(True))).all()]
