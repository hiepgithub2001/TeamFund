"""Money use cases: paying, cash, charges, expenses, unmatched transfers, reports, export.

Each function checks who may do it, calls the billing rules, records the audit log and commits."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Bill, Expense, Payment
from . import billing
from .access import get_team, manager_team
from .audit import audit, display_name
from .errors import DomainError, invalid
from .export import export_team
from .timeutil import period_label, to_local_str

MAX_AMOUNT = 1_000_000_000


def _ids(member_ids) -> list[int]:
    if not member_ids or len(member_ids) > 100 or not all(isinstance(i, int) for i in member_ids):
        raise invalid()
    return list(member_ids)


def unpaid(db: Session, team_id: int) -> list[dict]:
    """Everyone with something to pay. Open to anyone who opens the bot."""
    team = get_team(db, team_id)
    billing.ensure_bills(db, team)
    return billing.unpaid_by_member(db, team)


def create_intent(db: Session, team_id: int, actor: int | None, member_ids: list[int]) -> dict:
    return billing.create_intent(db, get_team(db, team_id), _ids(member_ids), actor)


def status(db: Session, team_id: int) -> dict:
    return billing.team_status(db, get_team(db, team_id))


def fund(db: Session, team_id: int) -> dict:
    return billing.fund(db, get_team(db, team_id))


def history(db: Session, team_id: int, limit: int = 10) -> list[dict]:
    return billing.history(db, get_team(db, team_id), min(limit, 50))


def markpaid(db: Session, team_id: int, actor: int, member_ids: list[int]) -> dict:
    team = manager_team(db, team_id, actor)
    result = billing.cash_payments(db, team, _ids(member_ids), actor)
    audit(db, team.id, actor, "payment.cash", target=", ".join(result["members"]), after={"amount": result["amount"]})
    db.commit()
    return result


def charges(db: Session, team_id: int, actor: int, title: str, amount: int, kind: str = "charge",
            member_ids: list[int] | None = None) -> dict:
    team = manager_team(db, team_id, actor)
    title = (title or "").strip()
    if kind not in ("charge", "split") or not title or len(title) > 100 or not 0 < amount <= MAX_AMOUNT:
        raise invalid()
    result = billing.create_charges(db, team, title, amount, None if member_ids is None else _ids(member_ids), kind)
    audit(db, team.id, actor, f"bill.{kind}", target=title, after=result)
    db.commit()
    return result


def waive(db: Session, team_id: int, actor: int, bill_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    b = db.get(Bill, bill_id)
    if not b or b.team_id != team.id:
        raise DomainError(404, "not_found")
    if b.status != "unpaid":
        raise DomainError(409, "already_resolved")
    b.status = "waived"
    audit(db, team.id, actor, "bill.waive", target=f"{b.title} {period_label(b.period)}",
          after={"bill_id": b.id, "amount": b.amount})
    db.commit()
    return billing.bill_dict(b)


def add_expense(db: Session, team_id: int, actor: int, amount: int, title: str,
                photo_file_id: str | None = None) -> dict:
    team = manager_team(db, team_id, actor)
    title = (title or "").strip()
    if not title or len(title) > 200 or not 0 < amount <= MAX_AMOUNT:
        raise invalid()
    e = Expense(team_id=team.id, amount=amount, title=title, photo_file_id=photo_file_id, created_by=actor)
    db.add(e)
    audit(db, team.id, actor, "expense.add", target=title, after={"amount": amount})
    db.commit()
    return {"id": e.id, "amount": e.amount, "title": e.title, "by": display_name(db, actor),
            "balance": billing.fund(db, team)["balance"]}


def unmatched(db: Session, team_id: int, actor: int) -> list[dict]:
    team = manager_team(db, team_id, actor)
    rows = db.scalars(select(Payment).where(Payment.team_id == team.id, Payment.status == "unmatched")
                      .order_by(Payment.id)).all()
    return [{"payment_id": p.id, "amount": p.amount, "content": p.content, "reason": p.reason,
             "at": to_local_str(p.created_at)} for p in rows]


def assign(db: Session, team_id: int, actor: int, payment_id: int, member_ids: list[int]) -> dict:
    team = manager_team(db, team_id, actor)
    result = billing.assign_payment(db, team, payment_id, _ids(member_ids), actor)
    audit(db, team.id, actor, "payment.assign", target=", ".join(result["members"]), after={"payment_id": payment_id})
    db.commit()
    return result


def dismiss(db: Session, team_id: int, actor: int, payment_id: int) -> dict:
    team = manager_team(db, team_id, actor)
    result = billing.dismiss_payment(db, team, payment_id, actor)
    audit(db, team.id, actor, "payment.other_income", after={"payment_id": payment_id})
    db.commit()
    return result


def record_sepay(db: Session, team_id: int, api_key: str, payload: dict) -> dict | None:
    """A SePay webhook: check the team's API key, then record and match the transfer.

    Returns the event to announce, or None (outgoing transfer, or one already recorded)."""
    from .teams import check_sepay_key
    return billing.apply_sepay(db, check_sepay_key(db, team_id, api_key), payload)


def export(db: Session, team_id: int, actor: int) -> bytes:
    return export_team(db, manager_team(db, team_id, actor))
