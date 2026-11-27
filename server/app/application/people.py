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


# actions /undo can take back. Others (bank, SePay, managers, owner, links) change things outside the
# database or decide who may act, so undo stops at them.
UNDOABLE = {"expense.add", "income.add", "payment.cash", "payment.assign", "payment.other_income", "entry.edit", "bill.charge",
            "bill.split", "bill.waive", "plan.save", "plan.window", "plan.delete", "plan.terms", "team.settings", "rules.edit", "charge.terms", "bill.due", "member.add",
            "member.update", "member.remove",
            "match.add", "match.update", "match.money", "match.fee", "match.delete", "match.cancel", "match.restore", "schedule.add", "schedule.update",
            "schedule.stop", "contact.save", "contact.update", "contact.delete"}


def audit(uow: UnitOfWork, clock: Clock, team_id, actor, action, target=None, before=None, after=None) -> None:
    """Log an action. An undoable one also keeps the rows it changed (call it before the commit)."""
    undo = None
    if action in UNDOABLE:
        uow.flush()
        undo = uow.changes()
    uow.audit.add(AuditEntry(team_id=team_id, actor_tg_id=actor, action=action, target=target,
                             before=before, after=after, created_at=clock.now(), undo=undo))
