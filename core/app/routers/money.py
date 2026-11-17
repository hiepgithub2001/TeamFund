"""Unpaid list, payment intents, cash payments, charges, expenses, reports and export."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_actor, get_team, internal_auth, require_manager
from ..errors import AppError
from ..models import Bill, Expense, Payment, Team
from ..services import billing
from ..services.audit import audit, display_name
from ..services.export import export_team
from ..services.timeutil import period_label, to_local_str

router = APIRouter(prefix="/api/teams/{team_id}", dependencies=[Depends(internal_auth)])


@router.get("/unpaid")
def unpaid(team: Team = Depends(get_team), db: Session = Depends(get_db)):
    billing.ensure_bills(db, team)
    return billing.unpaid_by_member(db, team)


class IntentIn(BaseModel):
    member_ids: list[int] = Field(min_length=1, max_length=100)


@router.post("/intents")
def create_intent(body: IntentIn, team: Team = Depends(get_team), actor: Optional[int] = Depends(get_actor),
                  db: Session = Depends(get_db)):
    return billing.create_intent(db, team, body.member_ids, actor)


@router.get("/status")
def status(team: Team = Depends(get_team), db: Session = Depends(get_db)):
    return billing.team_status(db, team)


@router.get("/fund")
def fund(team: Team = Depends(get_team), db: Session = Depends(get_db)):
    return billing.fund(db, team)


@router.get("/history")
def history(limit: int = 10, team: Team = Depends(get_team), db: Session = Depends(get_db)):
    return billing.history(db, team, min(limit, 50))


class MembersSel(BaseModel):
    member_ids: list[int] = Field(min_length=1, max_length=100)


@router.post("/markpaid")
def markpaid(body: MembersSel, team: Team = Depends(get_team), actor: int = Depends(require_manager),
             db: Session = Depends(get_db)):
    result = billing.cash_payments(db, team, body.member_ids, actor)
    audit(db, team.id, actor, "payment.cash", target=", ".join(result["members"]), after={"amount": result["amount"]})
    db.commit()
    return result


class ChargeIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    amount: int = Field(gt=0, le=1_000_000_000)
    kind: Literal["charge", "split"] = "charge"
    member_ids: Optional[list[int]] = None  # None = every active member


@router.post("/charges")
def charges(body: ChargeIn, team: Team = Depends(get_team), actor: int = Depends(require_manager),
            db: Session = Depends(get_db)):
    result = billing.create_charges(db, team, body.title, body.amount, body.member_ids, body.kind)
    audit(db, team.id, actor, f"bill.{body.kind}", target=body.title, after=result)
    db.commit()
    return result


@router.post("/bills/{bill_id}/waive")
def waive(bill_id: int, team: Team = Depends(get_team), actor: int = Depends(require_manager),
          db: Session = Depends(get_db)):
    b = db.get(Bill, bill_id)
    if not b or b.team_id != team.id:
        raise AppError(404, "not_found")
    if b.status != "unpaid":
        raise AppError(409, "already_resolved")
    b.status = "waived"
    audit(db, team.id, actor, "bill.waive", target=f"{b.title} {period_label(b.period)}",
          after={"bill_id": b.id, "amount": b.amount})
    db.commit()
    return billing.bill_dict(b)


class ExpenseIn(BaseModel):
    amount: int = Field(gt=0, le=1_000_000_000)
    title: str = Field(min_length=1, max_length=200)
    photo_file_id: Optional[str] = None


@router.post("/expenses")
def add_expense(body: ExpenseIn, team: Team = Depends(get_team), actor: int = Depends(require_manager),
                db: Session = Depends(get_db)):
    e = Expense(team_id=team.id, amount=body.amount, title=body.title.strip(), photo_file_id=body.photo_file_id,
                created_by=actor)
    db.add(e)
    audit(db, team.id, actor, "expense.add", target=e.title, after={"amount": e.amount})
    db.commit()
    return {"id": e.id, "amount": e.amount, "title": e.title, "by": display_name(db, actor),
            "balance": billing.fund(db, team)["balance"], "group_chat_id": team.group_chat_id}


@router.get("/unmatched")
def unmatched(team: Team = Depends(get_team), _: int = Depends(require_manager), db: Session = Depends(get_db)):
    rows = db.scalars(select(Payment).where(Payment.team_id == team.id, Payment.status == "unmatched")
                      .order_by(Payment.id)).all()
    return [{"payment_id": p.id, "amount": p.amount, "content": p.content, "reason": p.reason,
             "at": to_local_str(p.created_at)} for p in rows]


@router.post("/payments/{payment_id}/assign")
def assign(payment_id: int, body: MembersSel, team: Team = Depends(get_team), actor: int = Depends(require_manager),
           db: Session = Depends(get_db)):
    result = billing.assign_payment(db, team, payment_id, body.member_ids, actor)
    audit(db, team.id, actor, "payment.assign", target=", ".join(result["members"]), after={"payment_id": payment_id})
    db.commit()
    return result


@router.post("/payments/{payment_id}/dismiss")
def dismiss(payment_id: int, team: Team = Depends(get_team), actor: int = Depends(require_manager),
            db: Session = Depends(get_db)):
    result = billing.dismiss_payment(db, team, payment_id, actor)
    audit(db, team.id, actor, "payment.other_income", after={"payment_id": payment_id})
    db.commit()
    return result


@router.get("/export")
def export(team: Team = Depends(get_team), _: int = Depends(require_manager), db: Session = Depends(get_db)):
    return Response(export_team(db, team),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
