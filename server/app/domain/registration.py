"""Registering teams through the master bot: requests, approval by a system admin, creation."""
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Member, Team, TeamAdmin, TeamRequest, utcnow
from .access import is_super, require_super
from .audit import audit, display_name, user_info
from .crypto import decrypt, encrypt
from .errors import DomainError, invalid
from .timeutil import to_local_str


def _request_dict(db: Session, r: TeamRequest) -> dict:
    return {"id": r.id, "team_name": r.team_name, "status": r.status, **user_info(db, r.tg_id),
            "created_at": to_local_str(r.created_at)}


def bot_payload(t: Team) -> dict:
    """What the bot layer needs to run a team's bot."""
    return {"team_id": t.id, "name": t.name, "bot_id": t.bot_id, "bot_username": t.bot_username,
            "token": decrypt(t.bot_token_enc), "webhook_secret": t.webhook_secret, "lang": t.lang,
            "group_chat_id": t.group_chat_id}


def request_team(db: Session, actor: int, team_name: str) -> dict:
    name = (team_name or "").strip()[:100]
    if not name:
        raise invalid()
    if db.scalar(select(TeamRequest.id).where(TeamRequest.tg_id == actor, TeamRequest.status == "pending")):
        raise DomainError(409, "request_pending")
    r = TeamRequest(tg_id=actor, team_name=name)
    db.add(r)
    db.commit()
    return {**_request_dict(db, r), "admin_ids": sorted(settings.master_admin_ids)}


def pending_team_requests(db: Session, actor: int) -> list[dict]:
    require_super(actor)
    rows = db.scalars(select(TeamRequest).where(TeamRequest.status == "pending").order_by(TeamRequest.id)).all()
    return [_request_dict(db, r) for r in rows]


def decide_team_request(db: Session, actor: int, request_id: int, approve: bool) -> dict:
    require_super(actor)
    r = db.get(TeamRequest, request_id)
    if not r:
        raise DomainError(404, "not_found")
    if r.status != "pending":
        raise DomainError(409, "already_decided", status=r.status)
    r.status = "approved" if approve else "rejected"
    r.decided_by, r.decided_at = actor, utcnow()
    audit(db, None, actor, "team_request." + r.status, target=r.team_name)
    db.commit()
    return _request_dict(db, r)


def _approved(db: Session, actor: int) -> TeamRequest | None:
    return db.scalar(select(TeamRequest).where(TeamRequest.tg_id == actor, TeamRequest.status == "approved")
                     .order_by(TeamRequest.id.desc()))


def approved_request(db: Session, actor: int) -> dict:
    r = _approved(db, actor)
    return {"request": _request_dict(db, r) if r else None, "is_super": is_super(actor)}


def create_team(db: Session, actor: int, token: str, bot_id: int, bot_username: str, name: str | None) -> dict:
    req = _approved(db, actor)
    if not req and not is_super(actor):
        raise DomainError(403, "not_approved")
    if db.scalar(select(Team.id).where(Team.bot_id == bot_id)):
        raise DomainError(409, "bot_exists")
    team_name = (req.team_name if req else None) or name or bot_username
    team = Team(name=team_name, bot_id=bot_id, bot_username=bot_username, bot_token_enc=encrypt(token),
                webhook_secret=secrets.token_urlsafe(32), owner_tg_id=actor, lang=settings.default_lang)
    db.add(team)
    db.flush()
    db.add(TeamAdmin(team_id=team.id, tg_id=actor, role="owner", added_by=actor))
    if req:
        req.status = "used"
    audit(db, team.id, actor, "team.create", target=team_name)
    db.commit()
    return bot_payload(team)


def my_teams(db: Session, actor: int) -> list[dict]:
    rows = db.execute(select(Team, TeamAdmin.role).join(TeamAdmin, TeamAdmin.team_id == Team.id)
                      .where(TeamAdmin.tg_id == actor, Team.active.is_(True)).order_by(Team.name)).all()
    return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username, "role": role} for t, role in rows]


def all_teams(db: Session, actor: int) -> list[dict]:
    require_super(actor)
    counts = dict(db.execute(select(Member.team_id, func.count(Member.id))
                             .where(Member.status != "left").group_by(Member.team_id)).all())
    teams = db.scalars(select(Team).where(Team.active.is_(True)).order_by(Team.id)).all()
    return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username,
             "owner": display_name(db, t.owner_tg_id), "members": counts.get(t.id, 0)} for t in teams]


def active_bots(db: Session) -> list[dict]:
    return [bot_payload(t) for t in db.scalars(select(Team).where(Team.active.is_(True))).all()]
