"""Use cases: /undo. Pick any recent undoable action and take it back.

Each undoable action's log entry keeps the rows it inserted, changed or deleted. Undo puts them back, but only
if every one of them is still as the action left it: a bill paid by SePay after a /charge, for example, or a
later action that changed the same rows, stops that undo (it says so, and nothing changes). Actions that can't
be undone (bank, SePay, managers…) aren't offered, nor anything older than a week. A manager may only undo
their own actions; the owner may undo anyone's."""
from datetime import timedelta

from ..domain.entities import AuditEntry
from ..domain.errors import DomainError
from . import billing
from .access import manager_team, role_of
from .people import UNDOABLE, audit, display_name
from .ports import Conflict, Deps, UnitOfWork

UNDO_DAYS = 7
LIST_SIZE = 10
# actions that were announced to the team; their undo is announced too
ANNOUNCED = {"expense.add", "income.add", "payment.cash", "payment.assign", "bill.charge", "bill.split"}


class UndoUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _dict(self, uow: UnitOfWork, e: AuditEntry) -> dict:
        return {"id": e.id, "action": e.action, "target": e.target, "amount": (e.after or {}).get("amount"),
                "actor": display_name(uow, e.actor_tg_id), "at": self.d.clock.fmt(e.created_at)}

    def _allowed(self, uow: UnitOfWork, team_id: int, actor: int, e: AuditEntry) -> str | None:
        """None if the actor may undo e now, else why not."""
        if e.team_id != team_id or e.action not in UNDOABLE or e.undo is None:
            return "not_undoable"
        if e.undone_at is not None:
            return "gone"
        if e.created_at < self.d.clock.now() - timedelta(days=UNDO_DAYS):
            return "too_old"
        if e.actor_tg_id != actor and role_of(uow, team_id, actor) != "owner":
            return "not_yours"
        return None

    def recent(self, team_id: int, actor: int) -> list[dict]:
        """The actions this person can undo now, newest first."""
        with self.d.uow() as uow:
            manager_team(uow, team_id, actor)
            since = self.d.clock.now() - timedelta(days=UNDO_DAYS)
            return [self._dict(uow, e) for e in uow.audit.undoable_since(team_id, since)
                    if self._allowed(uow, team_id, actor, e) is None][:LIST_SIZE]

    def get(self, team_id: int, actor: int, entry_id: int) -> dict:
        with self.d.uow() as uow:
            manager_team(uow, team_id, actor)
            e = uow.audit.get(entry_id)
            reason = self._allowed(uow, team_id, actor, e) if e else "gone"
            if reason:
                raise DomainError(400, f"undo_{reason}")
            return self._dict(uow, e)

    def undo(self, team_id: int, actor: int, entry_id: int) -> dict:
        """Take back that action, if nothing has changed its rows since."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            e = uow.audit.get(entry_id)
            reason = self._allowed(uow, team.id, actor, e) if e else "gone"
            if reason:
                raise DomainError(400, f"undo_{reason}")
            v = self._dict(uow, e)
            try:
                uow.revert(e.undo)
            except Conflict as err:
                uow.rollback()
                raise DomainError(409, "undo_changed") from err
            e.undone_at, e.undone_by = self.d.clock.now(), actor
            billing.reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "undo", target=f"{e.action} {e.target or ''}".strip(),
                  after={"entry": e.id})
            uow.commit()
            billing.ensure_bills(uow, self.d, team)
            billing.update_late_fees(uow, self.d, team)  # fines follow the deadlines and charges put back
            return {**v, "announce": e.action in ANNOUNCED, "by": display_name(uow, actor)}
