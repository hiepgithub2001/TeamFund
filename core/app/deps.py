import hmac
from typing import Optional

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .errors import AppError
from .models import Team, TeamAdmin


def internal_auth(x_internal_key: str = Header("")):
    if not hmac.compare_digest(x_internal_key, settings.internal_key):
        raise AppError(401, "unauthorized")


def get_actor(x_actor_id: Optional[int] = Header(None)) -> Optional[int]:
    return x_actor_id


def require_actor(actor: Optional[int] = Depends(get_actor)) -> int:
    if actor is None:
        raise AppError(400, "actor_required")
    return actor


def is_super(tg_id: Optional[int]) -> bool:
    return tg_id is not None and tg_id in settings.master_admin_ids


def require_super(actor: int = Depends(require_actor)) -> int:
    if not is_super(actor):
        raise AppError(403, "not_super")
    return actor


def get_team(team_id: int, db: Session = Depends(get_db)) -> Team:
    team = db.get(Team, team_id)
    if not team or not team.active:
        raise AppError(404, "not_found")
    return team


def role_of(db: Session, team_id: int, tg_id: Optional[int]) -> Optional[str]:
    if tg_id is None:
        return None
    return db.scalar(select(TeamAdmin.role).where(TeamAdmin.team_id == team_id, TeamAdmin.tg_id == tg_id))


def require_manager(team: Team = Depends(get_team), actor: int = Depends(require_actor),
                    db: Session = Depends(get_db)) -> int:
    if role_of(db, team.id, actor) not in ("owner", "manager"):
        raise AppError(403, "not_manager")
    return actor


def require_owner(team: Team = Depends(get_team), actor: int = Depends(require_actor),
                  db: Session = Depends(get_db)) -> int:
    if role_of(db, team.id, actor) != "owner":
        raise AppError(403, "not_owner")
    return actor
