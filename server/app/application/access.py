"""Who may do what. Every use case that changes something checks here first."""
from ..domain.entities import Team
from ..domain.errors import DomainError
from ..domain.rules import can_manage, is_owner
from .ports import AppConfig, UnitOfWork


def is_super(config: AppConfig, tg_id: int | None) -> bool:
    return tg_id is not None and tg_id in config.master_admin_ids


def require_super(config: AppConfig, tg_id: int | None) -> None:
    if not is_super(config, tg_id):
        raise DomainError(403, "not_super")


def get_team(uow: UnitOfWork, team_id: int) -> Team:
    team = uow.teams.get(team_id)
    if not team or not team.active:
        raise DomainError(404, "not_found")
    return team


def role_of(uow: UnitOfWork, team_id: int, tg_id: int | None) -> str | None:
    return None if tg_id is None else uow.admins.role_of(team_id, tg_id)


def manager_team(uow: UnitOfWork, team_id: int, actor: int | None) -> Team:
    """The team, if `actor` is its owner or a manager."""
    team = get_team(uow, team_id)
    if not can_manage(role_of(uow, team.id, actor)):
        raise DomainError(403, "not_manager")
    return team


def owner_team(uow: UnitOfWork, team_id: int, actor: int | None) -> Team:
    """The team, if `actor` is its owner."""
    team = get_team(uow, team_id)
    if not is_owner(role_of(uow, team.id, actor)):
        raise DomainError(403, "not_owner")
    return team
