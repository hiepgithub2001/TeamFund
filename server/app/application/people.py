"""Names for display, and the audit log."""
from ..domain.entities import AuditEntry
from .ports import Clock, UnitOfWork


def display_name(uow: UnitOfWork, tg_id: int | None) -> str:
    if tg_id is None:
        return "—"
    user = uow.users.get(tg_id)
    return user.display_name() if user else str(tg_id)


def user_info(uow: UnitOfWork, tg_id: int) -> dict:
    user = uow.users.get(tg_id)
    return {"tg_id": tg_id, "name": display_name(uow, tg_id), "username": user.username if user else None}


def audit(uow: UnitOfWork, clock: Clock, team_id, actor, action, target=None, before=None, after=None) -> None:
    uow.audit.add(AuditEntry(team_id=team_id, actor_tg_id=actor, action=action, target=target,
                             before=before, after=after, created_at=clock.now()))
