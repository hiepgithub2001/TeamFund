"""Who may do what. Every use case that changes something checks here first."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Team, TeamAdmin
from .errors import DomainError


def is_super(tg_id: int | None) -> bool:
    return tg_id is not None and tg_id in settings.master_admin_ids


def require_super(tg_id: int | None) -> None:
    if not is_super(tg_id):
        raise DomainError(403, "not_super")


def get_team(db: Session, team_id: int) -> Team:
    team = db.get(Team, team_id)
    if not team or not team.active:
        raise DomainError(404, "not_found")
    return team


def role_of(db: Session, team_id: int, tg_id: int | None) -> str | None:
    if tg_id is None:
        return None
    return db.scalar(select(TeamAdmin.role).where(TeamAdmin.team_id == team_id, TeamAdmin.tg_id == tg_id))


def manager_team(db: Session, team_id: int, actor: int | None) -> Team:
    """The team, if `actor` is its owner or a manager."""
    team = get_team(db, team_id)
    if role_of(db, team.id, actor) not in ("owner", "manager"):
        raise DomainError(403, "not_manager")
    return team


def owner_team(db: Session, team_id: int, actor: int | None) -> Team:
    """The team, if `actor` is its owner."""
    team = get_team(db, team_id)
    if role_of(db, team.id, actor) != "owner":
        raise DomainError(403, "not_owner")
    return team
