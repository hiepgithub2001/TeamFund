"""Bills, payment intents and matching transfers to them. Shared by the money and job use cases."""
from datetime import timedelta

from ..domain.entities import Bill, IncomingTransfer, Member, Payment, PaymentIntent, Team
from ..domain.errors import DomainError
from ..domain.rules import (CYCLES, add_months, covers, due_date, find_payment_codes, late_days, member_fee,
                            new_payment_code, period_label, schedule, split_share, transfer_content)
from .ports import Conflict, Deps, UnitOfWork

INTENT_TTL = timedelta(hours=24)


def plans_of(uow: UnitOfWork, team: Team) -> dict:
    return {p.id: p for p in uow.plans.of_team(team.id)}


def fee_of(team: Team, member: Member, plans: dict) -> tuple[int, int]:
    """(amount, months) the member is billed per cycle."""
    return member_fee(team, member, plans.get(member.plan_id))


def covering(uow: UnitOfWork, team: Team, period: str) -> dict[int, tuple[Bill, Member]]:
    """Each member's fee bill that covers `period` (a monthly bill for it, or a longer one started earlier)."""
    out: dict[int, tuple[Bill, Member]] = {}
    for b, m in uow.bills.fee_bills_since(team.id, add_months(period, 1 - max(CYCLES))):
        if covers(b.period, b.months, period):
            out[m.id] = (b, m)
    return out


def ensure_bills(uow: UnitOfWork, d: Deps, team: Team, period: str | None = None, _retry: bool = True) -> int:
    """Bill every active member whose fee isn't covered for this month yet: one bill per cycle, so a
    quarterly member gets one bill for three months. Idempotent."""
    period = period or d.clock.current_period()
    covered, plans = covering(uow, team, period), plans_of(uow, team)
    created = 0
    for m in uow.members.of_team(team.id, active_only=True):
        amount, months = fee_of(team, m, plans)
        if amount > 0 and m.id not in covered:
            uow.bills.add(Bill(team_id=team.id, member_id=m.id, kind="monthly", title="monthly", period=period,
                               amount=amount, months=months if months > 1 else None,
                               monthly_key=f"{m.id}:{period}", created_at=d.clock.now()))
            created += 1
    if created:
        try:
            uow.commit()
        except Conflict:  # the scheduler and a request raced; reread and fill the gaps
            uow.rollback()
            return ensure_bills(uow, d, team, period, _retry=False) if _retry else 0
    update_late_fees(uow, d, team)
    return created


def update_late_fees(uow: UnitOfWork, d: Deps, team: Team) -> None:
    """Keep each overdue fee bill's late fee (its own "late" bill) at days overdue × the team's rate.
    Runs with ensure_bills, so amounts are current whenever someone looks or pays. A late bill stops
    growing once the fee is paid, and stays as it is for paused members; turning the fee off waives
    the late bills still unpaid."""
    rate, today = team.late_fee or 0, d.clock.local_now().date()
    late = {b.monthly_key: b for b in uow.bills.late_of_team(team.id)}
    changed = False
    for b, m in uow.bills.unpaid_with_members(team.id):
        if b.kind != "monthly" or m.status != "active":
            continue
        key = f"late:{b.id}"
        lb = late.get(key)
        if lb is not None and lb.status != "unpaid":
            continue
        days = late_days(due_date(team, b.period), today, team.late_fee_since) if rate else 0
        if days > 0:
            if lb is None:
                uow.bills.add(Bill(team_id=team.id, member_id=b.member_id, kind="late", title=f"late:{days}",
                                   period=b.period, months=b.months, amount=days * rate, monthly_key=key,
                                   created_at=d.clock.now()))
                changed = True
            elif (lb.amount, lb.title) != (days * rate, f"late:{days}"):
                lb.amount, lb.title, changed = days * rate, f"late:{days}", True
        elif lb is not None:
            lb.status, changed = "waived", True
    if changed:
        try:
            uow.commit()
        except Conflict:  # two requests added the same late bill; the other one's is as good
            uow.rollback()


def reprice_current_month(uow: UnitOfWork, d: Deps, team: Team) -> None:
    """After a fee or plan change, unpaid fee bills opened this month follow the member's new fee and cycle.
    Bills from earlier months (and paid ones) stay as they were; the new fee starts when they run out."""
    period, plans = d.clock.current_period(), plans_of(uow, team)
    for bill, member in uow.bills.fee_bills_since(team.id, period):
        if bill.period != period or bill.status != "unpaid":
            continue
        amount, months = fee_of(team, member, plans)
        if amount > 0:
            bill.amount, bill.months = amount, months if months > 1 else None
        else:
            bill.status = "waived"


def fee_groups(uow: UnitOfWork, team: Team) -> list[dict]:
    """The fees active members pay: [{amount, months, count}], monthly first. Empty when nobody pays a fee."""
    plans, groups = plans_of(uow, team), {}
    for m in uow.members.of_team(team.id, active_only=True):
        amount, months = fee_of(team, m, plans)
        if amount > 0:
            groups[(months, amount)] = groups.get((months, amount), 0) + 1
    return [{"amount": a, "months": n, "count": c} for (n, a), c in sorted(groups.items())]


def has_fees(uow: UnitOfWork, team: Team) -> bool:
    return team.fee > 0 or bool(fee_groups(uow, team))


def bill_dict(b: Bill) -> dict:
    out = {"id": b.id, "kind": b.kind, "title": b.title, "period": b.period, "months": b.months or 1,
           "period_label": period_label(b.period, b.months), "amount": b.amount, "status": b.status}
    if b.kind == "late":
        out["days"] = int(b.title.split(":")[1]) if ":" in b.title else 0
    return out


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
    content = transfer_content(team.bank_name, code)
    return {"code": code, "content": content, "amount": amount,
            "qr_url": d.qr.url(team.bank_name, team.pay_to(), amount, content),
            "bank_name": team.bank_name, "bank_account": team.pay_to(), "bank_holder": team.bank_holder,
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
        if bills and intent.amount <= transfer.amount < needed:
            # a late fee grew after the QR was made: the day they made it is the day that counts
            short = needed - transfer.amount
            for lb in sorted((b for b in bills if b.kind == "late"), key=lambda b: -b.id):
                cut = min(short, lb.amount)
                lb.amount, short = lb.amount - cut, short - cut
            if short == 0:
                bills = [b for b in bills if b.amount > 0]
                needed = transfer.amount
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


def partial_cash(uow: UnitOfWork, d: Deps, team: Team, member_id: int, amount: int, settle: bool,
                 actor: int) -> dict:
    """A member paid `amount` in cash, less than they owe. The money goes to their oldest bills first.
    The bill where it runs out is either settled at what was paid (settle=True: the rest is let go, e.g.
    someone who joined mid-quarter) or split into a paid part and a new unpaid bill for the rest.
    Bills the money doesn't reach stay unpaid. The fund grows by `amount` only."""
    groups = unpaid_by_member(uow, team, [member_id])
    if not groups:
        raise DomainError(400, "nothing_to_pay")
    g = groups[0]
    if not 0 < amount <= g["total"]:
        raise DomainError(400, "more_than_owed", owed=g["total"])
    p = Payment(team_id=team.id, source="cash", amount=amount, status="matched", recorded_by=actor,
                note=g["name"], created_at=d.clock.now())
    uow.payments.add(p)
    uow.flush()
    left, now, let_go = amount, d.clock.now(), 0
    for b in uow.bills.get_many([x["id"] for x in g["bills"]]):
        if left <= 0:
            break
        if b.amount > left:
            rest = b.amount - left
            if settle:
                let_go = rest
            else:
                uow.bills.add(Bill(team_id=team.id, member_id=b.member_id, kind=b.kind, title=b.title,
                                   period=b.period, amount=rest, months=b.months, created_at=now))
            b.amount = left
        left -= b.amount
        b.mark_paid(p.id, now)
    return {"members": [g["name"]], "amount": amount, "owed": g["total"], "let_go": let_go,
            "still_owed": g["total"] - amount - let_go}  # the split-off rest plus bills the money didn't reach


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
    # paid this month: their fee bill for it is paid, whether it's a monthly one or a quarter paid earlier
    paid = sorted((m.name for b, m in covering(uow, team, period).values()
                   if b.status == "paid" and m.status != "left"), key=str.casefold)
    unpaid = [{"name": g["name"], "total": g["total"]} for g in unpaid_by_member(uow, team)]
    notify, days = schedule(team)
    return {"period": period, "period_label": period_label(period), "fee": team.fee,
            "due": due_date(team, period).strftime("%d/%m"), "notify_day": notify, "due_days": days,
            "late_fee": team.late_fee or 0,
            "fees": fee_groups(uow, team), "has_fees": has_fees(uow, team),
            "paid": paid, "unpaid": unpaid, "total_unpaid": sum(u["total"] for u in unpaid)}


def fund(uow: UnitOfWork, team: Team) -> dict:
    income, expense = uow.payments.total(team.id), uow.expenses.total(team.id)
    return {"income": income, "other_income": uow.payments.total(team.id, source="other"), "expense": expense,
            "balance": income - expense,
            "unmatched": uow.payments.count_unmatched(team.id)}


def month_totals(uow: UnitOfWork, d: Deps, team: Team, period: str) -> dict:
    start, end = d.clock.month_bounds(period)
    return {"collected": uow.payments.total(team.id, start, end), "spent": uow.expenses.total(team.id, start, end)}


def history(uow: UnitOfWork, d: Deps, team: Team, limit: int = 10, period: str | None = None) -> list[dict]:
    """The latest transactions, or all of one month's ("YYYY-MM") when a period is given."""
    if period:
        start, end = d.clock.month_bounds(period)
        payments, expenses, limit = uow.payments.between(team.id, start, end), uow.expenses.between(team.id, start, end), 500
    else:
        payments, expenses = uow.payments.latest(team.id, limit), uow.expenses.latest(team.id, limit)
    items = [{"type": "in", "amount": p.amount, "title": p.note or p.content or "", "source": p.source,
              "status": p.status, "ts": p.created_at} for p in payments]
    items += [{"type": "out", "amount": e.amount, "title": e.title, "source": "expense", "status": "done",
               "ts": e.created_at} for e in expenses]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return [{**{k: v for k, v in i.items() if k != "ts"}, "at": d.clock.fmt(i["ts"])} for i in items[:limit]]
