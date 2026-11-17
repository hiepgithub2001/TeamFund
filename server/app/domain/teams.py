"""Team settings, bank account, SePay key, group link, admins, manager requests and audit log."""
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AuditLog, ManagerRequest, Team, TeamAdmin, utcnow
from .access import get_team, is_super, manager_team, owner_team, role_of
from .audit import audit, display_name, user_info
from .billing import reprice_current_month
from .crypto import hash_key
from .errors import DomainError, invalid
from .timeutil import to_local_str

REQUEST_COOLDOWN = timedelta(hours=24)


def sepay_url(team: Team) -> str:
    return f"{settings.public_url}/sepay/{team.id}"


def team_info(db: Session, team_id: int, actor: int | None) -> dict:
    team = get_team(db, team_id)
    return {"team_id": team.id, "name": team.name, "bot_username": team.bot_username, "fee": team.fee,
            "due_day": team.due_day, "lang": team.lang, "bank_name": team.bank_name,
            "bank_account": team.bank_account, "bank_holder": team.bank_holder,
            "sepay_connected": bool(team.sepay_key_hash), "sepay_url": sepay_url(team),
            "group_chat_id": team.group_chat_id, "role": role_of(db, team.id, actor)}


def role(db: Session, team_id: int, actor: int | None) -> dict:
    get_team(db, team_id)
    return {"role": role_of(db, team_id, actor), "is_super": is_super(actor)}


def update_settings(db: Session, team_id: int, actor: int, fee: int | None = None, due_day: int | None = None,
                    lang: str | None = None) -> dict:
    team = manager_team(db, team_id, actor)
    if fee is not None and not 0 <= fee <= 100_000_000:
        raise invalid()
    if due_day is not None and not 1 <= due_day <= 28:
        raise invalid()
    if lang is not None and lang not in ("en", "vi"):
        raise invalid()
    before = {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}
    changes = {k: v for k, v in (("fee", fee), ("due_day", due_day), ("lang", lang)) if v is not None}
    for k, v in changes.items():
        setattr(team, k, v)
    if "fee" in changes:
        reprice_current_month(db, team)
    audit(db, team.id, actor, "team.settings", before=before, after=changes)
    db.commit()
    return {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}


def set_bank(db: Session, team_id: int, actor: int, bank_name: str, bank_account: str, bank_holder: str) -> dict:
    team = owner_team(db, team_id, actor)
    bank_name, bank_account, bank_holder = bank_name.strip(), bank_account.strip(), bank_holder.strip().upper()
    if not (2 <= len(bank_name) <= 64 and re.fullmatch(r"[0-9A-Za-z]{4,32}", bank_account)
            and 2 <= len(bank_holder) <= 128):
        raise invalid()
    before = {"bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder}
    team.bank_name, team.bank_account, team.bank_holder = bank_name, bank_account, bank_holder
    after = {"bank_name": bank_name, "bank_account": bank_account, "bank_holder": bank_holder}
    audit(db, team.id, actor, "team.bank", before=before, after=after)
    db.commit()
    return {**after, "by": display_name(db, actor)}


def sepay_info(db: Session, team_id: int, actor: int) -> dict:
    team = manager_team(db, team_id, actor)
    return {"webhook_url": sepay_url(team), "connected": bool(team.sepay_key_hash)}


def new_sepay_key(db: Session, team_id: int, actor: int) -> dict:
    team = owner_team(db, team_id, actor)
    key = secrets.token_urlsafe(24)
    team.sepay_key_hash = hash_key(key)
    audit(db, team.id, actor, "team.sepay_key")
    db.commit()
    return {"api_key": key, "webhook_url": sepay_url(team)}


def check_sepay_key(db: Session, team_id: int, key: str) -> Team:
    """The team whose SePay webhook this is, if the API key matches."""
    team = db.get(Team, team_id)
    if (not team or not team.active or not team.sepay_key_hash
            or not hmac.compare_digest(hash_key(key), team.sepay_key_hash)):
        raise DomainError(401, "unauthorized")
    return team


def link_group(db: Session, team_id: int, actor: int, chat_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    before = team.group_chat_id
    team.group_chat_id = chat_id
    audit(db, team.id, actor, "team.group", before={"chat_id": before}, after={"chat_id": chat_id})
    db.commit()
    return {"group_chat_id": chat_id}


# ---- admins ---------------------------------------------------------------------------------

def list_admins(db: Session, team_id: int) -> list[dict]:
    rows = db.scalars(select(TeamAdmin).where(TeamAdmin.team_id == team_id)
                      .order_by(TeamAdmin.role.desc(), TeamAdmin.id)).all()
    return [{**user_info(db, a.tg_id), "role": a.role} for a in rows]


def remove_admin(db: Session, team_id: int, actor: int, tg_id: int) -> dict:
    team = owner_team(db, team_id, actor)
    a = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == tg_id))
    if not a or a.role != "manager":
        raise DomainError(404, "not_a_manager")
    name = display_name(db, tg_id)
    db.delete(a)
    audit(db, team.id, actor, "admin.remove", target=name)
    db.commit()
    return {"tg_id": tg_id, "name": name}


def transfer_owner(db: Session, team_id: int, actor: int, tg_id: int) -> dict:
    team = owner_team(db, team_id, actor)
    target = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == tg_id))
    if not target or target.role != "manager":
        raise DomainError(404, "not_a_manager")
    me = db.scalar(select(TeamAdmin).where(TeamAdmin.team_id == team.id, TeamAdmin.tg_id == actor))
    me.role, target.role, team.owner_tg_id = "manager", "owner", tg_id
    name = display_name(db, tg_id)
    audit(db, team.id, actor, "team.transfer_owner", target=name)
    db.commit()
    return {"tg_id": tg_id, "name": name, "previous_owner": actor}


# ---- manager requests ----------------------------------------------------------------------

def _mreq_dict(db: Session, r: ManagerRequest, team: Team) -> dict:
    return {"id": r.id, "status": r.status, **user_info(db, r.tg_id), "owner_tg_id": team.owner_tg_id,
            "created_at": to_local_str(r.created_at)}


def request_manager(db: Session, team_id: int, actor: int) -> dict:
    team = get_team(db, team_id)
    if role_of(db, team.id, actor):
        raise DomainError(409, "already_manager")
    if db.scalar(select(ManagerRequest.id).where(ManagerRequest.team_id == team.id,
                                                 ManagerRequest.tg_id == actor, ManagerRequest.status == "pending")):
        raise DomainError(409, "request_pending")
    last_reject = db.scalar(select(ManagerRequest.decided_at).where(
        ManagerRequest.team_id == team.id, ManagerRequest.tg_id == actor, ManagerRequest.status == "rejected")
        .order_by(ManagerRequest.decided_at.desc()))
    if last_reject and utcnow() - last_reject < REQUEST_COOLDOWN:
        raise DomainError(429, "cooldown", until=to_local_str(last_reject + REQUEST_COOLDOWN))
    r = ManagerRequest(team_id=team.id, tg_id=actor)
    db.add(r)
    db.commit()
    return _mreq_dict(db, r, team)


def pending_manager_requests(db: Session, team_id: int, actor: int) -> list[dict]:
    team = owner_team(db, team_id, actor)
    rows = db.scalars(select(ManagerRequest).where(ManagerRequest.team_id == team.id,
                                                   ManagerRequest.status == "pending").order_by(ManagerRequest.id)).all()
    return [_mreq_dict(db, r, team) for r in rows]


def decide_manager_request(db: Session, team_id: int, actor: int, request_id: int, approve: bool) -> dict:
    team = owner_team(db, team_id, actor)
    r = db.get(ManagerRequest, request_id)
    if not r or r.team_id != team.id:
        raise DomainError(404, "not_found")
    if r.status != "pending":
        raise DomainError(409, "already_decided", status=r.status)
    r.status, r.decided_at = ("approved" if approve else "rejected"), utcnow()
    if approve and not role_of(db, team.id, r.tg_id):
        db.add(TeamAdmin(team_id=team.id, tg_id=r.tg_id, role="manager", added_by=actor))
    audit(db, team.id, actor, "manager_request." + r.status, target=display_name(db, r.tg_id))
    db.commit()
    return _mreq_dict(db, r, team)


def audit_log(db: Session, team_id: int, actor: int, limit: int = 20) -> list[dict]:
    team = owner_team(db, team_id, actor)
    rows = db.scalars(select(AuditLog).where(AuditLog.team_id == team.id)
                      .order_by(AuditLog.id.desc()).limit(min(limit, 100))).all()
    return [{"at": to_local_str(a.created_at), "actor": display_name(db, a.actor_tg_id), "action": a.action,
             "target": a.target, "before": a.before, "after": a.after} for a in rows]
