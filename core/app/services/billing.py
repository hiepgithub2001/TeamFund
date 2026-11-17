"""Bills, payment intents, SePay matching and money reports."""
import math
import re
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import Bill, Expense, Member, Payment, PaymentIntent, Team, TeamAdmin, utcnow
from .crypto import CODE_ALPHABET, random_code
from .timeutil import current_period, month_bounds_utc, period_label, to_local_str

INTENT_TTL = timedelta(hours=24)
CODE_PREFIX = "TF"
# Lookahead so overlapping candidates are all tried, e.g. "GIFTF..." before the real code.
CODE_RE = re.compile(rf"(?=({CODE_PREFIX}[{CODE_ALPHABET}]{{8}}))")


def effective_fee(team: Team, member: Member) -> int:
    return member.fee_override if member.fee_override is not None else (team.fee or 0)


def ensure_bills(db: Session, team: Team, period: str | None = None, _retry: bool = True) -> int:
    """Create this month's fee bill for every active member who doesn't have one. Idempotent."""
    period = period or current_period()
    members = db.scalars(select(Member).where(Member.team_id == team.id, Member.status == "active")).all()
    billed = set(db.scalars(select(Bill.member_id).where(
        Bill.team_id == team.id, Bill.kind == "monthly", Bill.period == period)).all())
    created = 0
    for m in members:
        fee = effective_fee(team, m)
        if fee > 0 and m.id not in billed:
            db.add(Bill(team_id=team.id, member_id=m.id, kind="monthly", title="monthly",
                        period=period, amount=fee, monthly_key=f"{m.id}:{period}"))
            created += 1
    if created:
        try:
            db.commit()
        except IntegrityError:  # the scheduler and a request raced; reread and fill the gaps
            db.rollback()
            return ensure_bills(db, team, period, _retry=False) if _retry else 0
    return created


def reprice_current_month(db: Session, team: Team) -> None:
    """After a fee change, unpaid bills of this month follow the new fee."""
    rows = db.execute(select(Bill, Member).join(Member, Bill.member_id == Member.id).where(
        Bill.team_id == team.id, Bill.kind == "monthly", Bill.period == current_period(),
        Bill.status == "unpaid")).all()
    for bill, member in rows:
        fee = effective_fee(team, member)
        if fee > 0:
            bill.amount = fee
        else:
            bill.status = "waived"


def bill_dict(b: Bill) -> dict:
    return {"id": b.id, "kind": b.kind, "title": b.title, "period": b.period,
            "period_label": period_label(b.period), "amount": b.amount, "status": b.status}


def unpaid_by_member(db: Session, team: Team, member_ids: list[int] | None = None) -> list[dict]:
    q = (select(Bill, Member).join(Member, Bill.member_id == Member.id)
         .where(Bill.team_id == team.id, Bill.status == "unpaid", Member.status.in_(["active", "paused"])))
    if member_ids is not None:
        q = q.where(Member.id.in_(member_ids))
    groups: dict[int, dict] = {}
    for bill, member in db.execute(q.order_by(Bill.period, Bill.id)).all():
        g = groups.setdefault(member.id, {"member_id": member.id, "name": member.name, "total": 0, "bills": []})
        g["total"] += bill.amount
        g["bills"].append(bill_dict(bill))
    return sorted(groups.values(), key=lambda g: g["name"].casefold())


def qr_url(team: Team, amount: int, code: str) -> str:
    return ("https://qr.sepay.vn/img?acc={acc}&bank={bank}&amount={amount}&des={des}&template=compact"
            .format(acc=quote(team.bank_account or ""), bank=quote(team.bank_name or ""),
                    amount=amount, des=quote(code)))


def create_intent(db: Session, team: Team, member_ids: list[int], payer_tg_id: int | None) -> dict:
    if not team.bank_account or not team.bank_name:
        raise AppError(400, "bank_not_set")
    groups = unpaid_by_member(db, team, member_ids)
    bills = [b for g in groups for b in g["bills"]]
    if not bills:
        raise AppError(400, "nothing_to_pay")
    while True:
        code = CODE_PREFIX + random_code(8)
        if not db.scalar(select(PaymentIntent.id).where(PaymentIntent.code == code)):
            break
    amount = sum(b["amount"] for b in bills)
    intent = PaymentIntent(team_id=team.id, code=code, amount=amount, payer_tg_id=payer_tg_id,
                           bill_ids=[b["id"] for b in bills], member_names=[g["name"] for g in groups],
                           status="pending", expires_at=utcnow() + INTENT_TTL)
    db.add(intent)
    db.commit()
    return {"code": code, "amount": amount, "qr_url": qr_url(team, amount, code),
            "bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder,
            "members": intent.member_names, "expires_at": to_local_str(intent.expires_at)}


def _mark_paid(bills: list[Bill], payment: Payment) -> None:
    now = utcnow()
    for b in bills:
        b.status = "paid"
        b.payment_id = payment.id
        b.paid_at = now


def _names_for_bills(db: Session, bills: list[Bill]) -> list[str]:
    ids = {b.member_id for b in bills}
    names = db.scalars(select(Member.name).where(Member.id.in_(ids))).all() if ids else []
    return sorted(names, key=str.casefold)


def admin_ids(db: Session, team_id: int) -> list[int]:
    return list(db.scalars(select(TeamAdmin.tg_id).where(TeamAdmin.team_id == team_id)).all())


def apply_sepay(db: Session, team: Team, payload: dict) -> dict | None:
    """Record one SePay transaction and match it to a payment intent.

    Returns the event to send to the gateway, or None when there is nothing to announce
    (outgoing transfer, or a webhook SePay already delivered)."""
    if str(payload.get("transferType", "in")).lower() != "in":
        return None
    tx_id = f"{team.id}:{payload.get('id')}"
    if db.scalar(select(Payment.id).where(Payment.sepay_tx_id == tx_id)):
        return None
    amount = int(float(payload.get("transferAmount") or 0))
    content = " ".join(str(payload.get(k) or "") for k in ("code", "content", "description")).strip()
    normalized = re.sub(r"[^A-Z0-9]", "", content.upper())
    candidates = [m.group(1) for m in CODE_RE.finditer(normalized)]
    intent = None
    if candidates:
        intent = db.scalar(select(PaymentIntent).where(
            PaymentIntent.team_id == team.id, PaymentIntent.code.in_(candidates)))

    payment = Payment(team_id=team.id, source="sepay", amount=amount, sepay_tx_id=tx_id,
                      content=content[:500], status="unmatched")
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:  # same webhook delivered twice at the same moment
        db.rollback()
        return None

    needed = 0
    reason = "no_code"
    names: list[str] = []
    if intent:
        payment.intent_id = intent.id
        payment.payer_tg_id = intent.payer_tg_id
        bills = db.scalars(select(Bill).where(Bill.id.in_(intent.bill_ids), Bill.status == "unpaid")).all()
        needed = sum(b.amount for b in bills)
        if not bills:
            reason = "already_paid"
        elif amount < needed:
            reason = "amount_low"
        else:
            reason = None
            _mark_paid(bills, payment)
            names = _names_for_bills(db, bills)
            payment.status = "matched"
            payment.note = ", ".join(names)
            intent.status = "paid"
            intent.payment_id = payment.id
    payment.reason = reason
    db.commit()

    if reason is None:
        return {"type": "payment_confirmed", "team_id": team.id, "payer_tg_id": intent.payer_tg_id,
                "members": names, "amount": amount, "group_chat_id": team.group_chat_id}
    return {"type": "unmatched_payment", "team_id": team.id, "payment_id": payment.id, "amount": amount,
            "content": content[:200], "reason": reason, "needed": needed,
            "at": to_local_str(payment.created_at), "admins": admin_ids(db, team.id)}


def cash_payments(db: Session, team: Team, member_ids: list[int], actor: int) -> dict:
    groups = unpaid_by_member(db, team, member_ids)
    if not groups:
        raise AppError(400, "nothing_to_pay")
    total = 0
    for g in groups:
        p = Payment(team_id=team.id, source="cash", amount=g["total"], status="matched",
                    recorded_by=actor, note=g["name"])
        db.add(p)
        db.flush()
        bills = db.scalars(select(Bill).where(Bill.id.in_([b["id"] for b in g["bills"]]))).all()
        _mark_paid(bills, p)
        total += g["total"]
    db.commit()
    return {"members": [g["name"] for g in groups], "amount": total}


def _team_payment(db: Session, team: Team, payment_id: int) -> Payment:
    p = db.get(Payment, payment_id)
    if not p or p.team_id != team.id:
        raise AppError(404, "not_found")
    if p.status != "unmatched":
        raise AppError(409, "already_resolved")
    return p


def assign_payment(db: Session, team: Team, payment_id: int, member_ids: list[int], actor: int) -> dict:
    p = _team_payment(db, team, payment_id)
    groups = unpaid_by_member(db, team, member_ids)
    if not groups:
        raise AppError(400, "nothing_to_pay")
    bills = db.scalars(select(Bill).where(Bill.id.in_([b["id"] for g in groups for b in g["bills"]]))).all()
    _mark_paid(bills, p)
    names = [g["name"] for g in groups]
    p.status, p.recorded_by, p.note = "resolved", actor, ", ".join(names)
    db.commit()
    return {"members": names, "amount": p.amount, "billed": sum(g["total"] for g in groups)}


def dismiss_payment(db: Session, team: Team, payment_id: int, actor: int) -> dict:
    p = _team_payment(db, team, payment_id)
    p.status, p.recorded_by, p.note = "resolved", actor, "other income"
    db.commit()
    return {"amount": p.amount}


def create_charges(db: Session, team: Team, title: str, amount: int, member_ids: list[int] | None,
                   kind: str) -> dict:
    q = select(Member).where(Member.team_id == team.id, Member.status == "active")
    if member_ids is not None:
        q = q.where(Member.id.in_(member_ids))
    members = db.scalars(q).all()
    if not members:
        raise AppError(400, "no_members")
    each = amount if kind == "charge" else int(math.ceil(amount / len(members) / 1000) * 1000)
    period = current_period()
    for m in members:
        db.add(Bill(team_id=team.id, member_id=m.id, kind=kind, title=title[:100], period=period, amount=each))
    db.commit()
    return {"count": len(members), "each": each, "title": title, "total": each * len(members)}


def team_status(db: Session, team: Team) -> dict:
    period = current_period()
    ensure_bills(db, team, period)
    rows = db.execute(select(Bill.status, Member.name).join(Member, Bill.member_id == Member.id).where(
        Bill.team_id == team.id, Bill.kind == "monthly", Bill.period == period)).all()
    paid = sorted((name for status, name in rows if status == "paid"), key=str.casefold)
    unpaid = [{"name": g["name"], "total": g["total"]} for g in unpaid_by_member(db, team)]
    return {"period": period, "period_label": period_label(period), "fee": team.fee, "due_day": team.due_day,
            "paid": paid, "unpaid": unpaid, "total_unpaid": sum(u["total"] for u in unpaid)}


def fund(db: Session, team: Team) -> dict:
    income = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.team_id == team.id))
    expense = db.scalar(select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.team_id == team.id))
    unmatched = db.scalar(select(func.count(Payment.id)).where(
        Payment.team_id == team.id, Payment.status == "unmatched"))
    return {"income": int(income), "expense": int(expense), "balance": int(income) - int(expense),
            "unmatched": int(unmatched)}


def month_totals(db: Session, team: Team, period: str) -> dict:
    start, end = month_bounds_utc(period)
    collected = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.team_id == team.id, Payment.created_at >= start, Payment.created_at < end))
    spent = db.scalar(select(func.coalesce(func.sum(Expense.amount), 0)).where(
        Expense.team_id == team.id, Expense.created_at >= start, Expense.created_at < end))
    return {"collected": int(collected), "spent": int(spent)}


def history(db: Session, team: Team, limit: int = 10) -> list[dict]:
    pays = db.scalars(select(Payment).where(Payment.team_id == team.id)
                      .order_by(Payment.created_at.desc()).limit(limit)).all()
    exps = db.scalars(select(Expense).where(Expense.team_id == team.id)
                      .order_by(Expense.created_at.desc()).limit(limit)).all()
    items = [{"type": "in", "amount": p.amount, "title": p.note or p.content or "", "source": p.source,
              "status": p.status, "ts": p.created_at} for p in pays]
    items += [{"type": "out", "amount": e.amount, "title": e.title, "source": "expense",
               "status": "done", "ts": e.created_at} for e in exps]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return [{**{k: v for k, v in i.items() if k != "ts"}, "at": to_local_str(i["ts"])} for i in items[:limit]]
