"""Bills, payment intents and matching transfers to them. Shared by the money and job use cases."""
from datetime import timedelta

from ..domain.entities import Bill, IncomingTransfer, Member, Payment, PaymentIntent, Team
from ..domain.errors import DomainError
from ..domain.rules import effective_fee, find_payment_codes, new_payment_code, period_label, split_share
from .ports import Conflict, Deps, UnitOfWork

INTENT_TTL = timedelta(hours=24)


def ensure_bills(uow: UnitOfWork, d: Deps, team: Team, period: str | None = None, _retry: bool = True) -> int:
    """Create this month's fee bill for every active member who doesn't have one. Idempotent."""
    period = period or d.clock.current_period()
    billed = uow.bills.billed_member_ids(team.id, period)
    created = 0
    for m in uow.members.of_team(team.id, active_only=True):
        fee = effective_fee(team, m)
        if fee > 0 and m.id not in billed:
            uow.bills.add(Bill(team_id=team.id, member_id=m.id, kind="monthly", title="monthly", period=period,
                               amount=fee, monthly_key=f"{m.id}:{period}", created_at=d.clock.now()))
            created += 1
    if created:
        try:
            uow.commit()
        except Conflict:  # the scheduler and a request raced; reread and fill the gaps
            uow.rollback()
            return ensure_bills(uow, d, team, period, _retry=False) if _retry else 0
    return created


def reprice_current_month(uow: UnitOfWork, d: Deps, team: Team) -> None:
    """After a fee change, this month's unpaid bills follow the new fee."""
    for bill, member in uow.bills.monthly_with_members(team.id, d.clock.current_period(), unpaid_only=True):
        fee = effective_fee(team, member)
        if fee > 0:
            bill.amount = fee
        else:
            bill.status = "waived"


def bill_dict(b: Bill) -> dict:
    return {"id": b.id, "kind": b.kind, "title": b.title, "period": b.period,
            "period_label": period_label(b.period), "amount": b.amount, "status": b.status}


def unpaid_by_member(uow: UnitOfWork, team: Team, member_ids: list[int] | None = None) -> list[dict]:
    groups: dict[int, dict] = {}
    for bill, member in uow.bills.unpaid_with_members(team.id, member_ids):
        g = groups.setdefault(member.id, {"member_id": member.id, "name": member.name, "total": 0, "bills": []})
        g["total"] += bill.amount
        g["bills"].append(bill_dict(bill))
    return sorted(groups.values(), key=lambda g: g["name"].casefold())


def create_intent(uow: UnitOfWork, d: Deps, team: Team, member_ids: list[int], payer_tg_id: int | None) -> dict:
    if not team.has_bank():
        raise DomainError(400, "bank_not_set")
    groups = unpaid_by_member(uow, team, member_ids)
    bills = [b for g in groups for b in g["bills"]]
    if not bills:
        raise DomainError(400, "nothing_to_pay")
    code = new_payment_code()
    while uow.intents.code_exists(code):
        code = new_payment_code()
    amount = sum(b["amount"] for b in bills)
    now = d.clock.now()
    intent = PaymentIntent(team_id=team.id, code=code, amount=amount, payer_tg_id=payer_tg_id,
                           bill_ids=[b["id"] for b in bills], member_names=[g["name"] for g in groups],
                           created_at=now, expires_at=now + INTENT_TTL)
    uow.intents.add(intent)
    uow.commit()
    return {"code": code, "amount": amount, "qr_url": d.qr.url(team.bank_name, team.bank_account, amount, code),
            "bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder,
            "members": intent.member_names, "expires_at": d.clock.fmt(intent.expires_at)}


def _mark_paid(bills: list[Bill], payment: Payment, d: Deps) -> None:
    now = d.clock.now()
    for b in bills:
        b.mark_paid(payment.id, now)


def record_transfer(uow: UnitOfWork, d: Deps, team: Team, transfer: IncomingTransfer) -> dict | None:
    """Record money that arrived and match it to a payment intent by the code in its content.

    Returns the event to announce, or None (outgoing transfer, or one already recorded)."""
    if not transfer.incoming:
        return None
    tx_id = f"{team.id}:{transfer.tx_id}"
    if uow.payments.tx_exists(tx_id):
        return None
    codes = find_payment_codes(transfer.content)
    intent = uow.intents.find_by_codes(team.id, codes) if codes else None
    payment = Payment(team_id=team.id, source="sepay", amount=transfer.amount, sepay_tx_id=tx_id,
                      content=transfer.content[:500], created_at=d.clock.now())
    uow.payments.add(payment)
    try:
        uow.flush()
    except Conflict:  # the same webhook delivered twice at the same moment
        uow.rollback()
        return None

    needed, reason, names = 0, "no_code", []
    if intent:
        payment.intent_id, payment.payer_tg_id = intent.id, intent.payer_tg_id
        bills = uow.bills.get_many(intent.bill_ids, unpaid_only=True)
        needed = sum(b.amount for b in bills)
        if not bills:
            reason = "already_paid"
        elif transfer.amount < needed:
            reason = "amount_low"
        else:
            reason = None
            _mark_paid(bills, payment, d)
            names = sorted(uow.members.names({b.member_id for b in bills}), key=str.casefold)
            payment.status, payment.note = "matched", ", ".join(names)
            intent.status, intent.payment_id = "paid", payment.id
    payment.reason = reason
    uow.commit()

    if reason is None:
        return {"type": "payment_confirmed", "team_id": team.id, "payer_tg_id": intent.payer_tg_id,
                "members": names, "amount": transfer.amount, "group_chat_id": team.group_chat_id}
    return {"type": "unmatched_payment", "team_id": team.id, "payment_id": payment.id, "amount": transfer.amount,
            "content": transfer.content[:200], "reason": reason, "needed": needed,
            "at": d.clock.fmt(payment.created_at), "admins": [a.tg_id for a in uow.admins.of_team(team.id)]}


def cash_payments(uow: UnitOfWork, d: Deps, team: Team, member_ids: list[int], actor: int) -> dict:
    groups = unpaid_by_member(uow, team, member_ids)
    if not groups:
        raise DomainError(400, "nothing_to_pay")
    total = 0
    for g in groups:
        p = Payment(team_id=team.id, source="cash", amount=g["total"], status="matched", recorded_by=actor,
                    note=g["name"], created_at=d.clock.now())
        uow.payments.add(p)
        uow.flush()
        _mark_paid(uow.bills.get_many([b["id"] for b in g["bills"]]), p, d)
        total += g["total"]
    return {"members": [g["name"] for g in groups], "amount": total}


def unmatched_payment(uow: UnitOfWork, team: Team, payment_id: int) -> Payment:
    p = uow.payments.get(payment_id)
    if not p or p.team_id != team.id:
        raise DomainError(404, "not_found")
    if p.status != "unmatched":
        raise DomainError(409, "already_resolved")
    return p


def assign_payment(uow: UnitOfWork, d: Deps, team: Team, payment_id: int, member_ids: list[int], actor: int) -> dict:
    p = unmatched_payment(uow, team, payment_id)
    groups = unpaid_by_member(uow, team, member_ids)
    if not groups:
        raise DomainError(400, "nothing_to_pay")
    _mark_paid(uow.bills.get_many([b["id"] for g in groups for b in g["bills"]]), p, d)
    names = [g["name"] for g in groups]
    p.status, p.recorded_by, p.note = "resolved", actor, ", ".join(names)
    return {"members": names, "amount": p.amount, "billed": sum(g["total"] for g in groups)}


def create_charges(uow: UnitOfWork, d: Deps, team: Team, title: str, amount: int, member_ids: list[int] | None,
                   kind: str) -> dict:
    members: list[Member] = uow.members.of_team(team.id, active_only=True, ids=member_ids)
    if not members:
        raise DomainError(400, "no_members")
    each = amount if kind == "charge" else split_share(amount, len(members))
    period, now = d.clock.current_period(), d.clock.now()
    for m in members:
        uow.bills.add(Bill(team_id=team.id, member_id=m.id, kind=kind, title=title[:100], period=period,
                           amount=each, created_at=now))
    return {"count": len(members), "each": each, "title": title, "total": each * len(members)}


def team_status(uow: UnitOfWork, d: Deps, team: Team) -> dict:
    period = d.clock.current_period()
    ensure_bills(uow, d, team, period)
    rows = uow.bills.monthly_with_members(team.id, period)
    paid = sorted((m.name for b, m in rows if b.status == "paid"), key=str.casefold)
    unpaid = [{"name": g["name"], "total": g["total"]} for g in unpaid_by_member(uow, team)]
    return {"period": period, "period_label": period_label(period), "fee": team.fee, "due_day": team.due_day,
            "paid": paid, "unpaid": unpaid, "total_unpaid": sum(u["total"] for u in unpaid)}


def fund(uow: UnitOfWork, team: Team) -> dict:
    income, expense = uow.payments.total(team.id), uow.expenses.total(team.id)
    return {"income": income, "expense": expense, "balance": income - expense,
            "unmatched": uow.payments.count_unmatched(team.id)}


def month_totals(uow: UnitOfWork, d: Deps, team: Team, period: str) -> dict:
    start, end = d.clock.month_bounds(period)
    return {"collected": uow.payments.total(team.id, start, end), "spent": uow.expenses.total(team.id, start, end)}


def history(uow: UnitOfWork, d: Deps, team: Team, limit: int = 10) -> list[dict]:
    items = [{"type": "in", "amount": p.amount, "title": p.note or p.content or "", "source": p.source,
              "status": p.status, "ts": p.created_at} for p in uow.payments.latest(team.id, limit)]
    items += [{"type": "out", "amount": e.amount, "title": e.title, "source": "expense", "status": "done",
               "ts": e.created_at} for e in uow.expenses.latest(team.id, limit)]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return [{**{k: v for k, v in i.items() if k != "ts"}, "at": d.clock.fmt(i["ts"])} for i in items[:limit]]
