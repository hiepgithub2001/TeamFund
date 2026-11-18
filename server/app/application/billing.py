"""Bills, payment intents and matching transfers to them. Shared by the money and job use cases."""
from datetime import date, timedelta

from ..domain.entities import Bill, IncomingTransfer, Member, Payment, PaymentIntent, Team
from ..domain.errors import DomainError
from ..domain.rules import (DONOR_NAME_MAX, MAX_DONATION, donation_ok, CYCLES, add_months, covers, cycle_bill, cycle_start, due_date, find_payment_codes, late_days,
                            member_fee, new_payment_code, notify_date, on_plan, period_label, split_share, terms,
                            transfer_content)
from .ports import Conflict, Deps, UnitOfWork

INTENT_TTL = timedelta(hours=24)


class Plans(dict):
    """The team's plans by id, and each member's plan memberships (`ms`: member id -> memberships)."""

    def __init__(self, plans, memberships):
        super().__init__({p.id: p for p in plans})
        self.ms: dict[int, list] = {}
        for x in memberships:
            self.ms.setdefault(x.member_id, []).append(x)


def plans_of(uow: UnitOfWork, team: Team) -> Plans:
    return Plans(uow.plans.of_team(team.id), uow.memberships.of_team(team.id))


def membership_for(member: Member, plans: Plans, period: str):
    """The member's plan membership that applies in `period` (at most one: they never overlap)."""
    return next((x for x in plans.ms.get(member.id, []) if x.plan_id in plans and on_plan(x, period)), None)


def terms_of(team: Team, plans: dict, plan_id: int | None):
    """The terms a bill is under: its plan's (if the plan still exists), else the default package's."""
    return terms(team, plans.get(plan_id) if plan_id is not None else None)


def plan_for(member: Member, plans: dict, period: str):
    """The member's plan in `period`, if one of their memberships covers it; else None (the default package)."""
    x = membership_for(member, plans, period)
    return plans[x.plan_id] if x else None


def fee_of(team: Team, member: Member, plans: dict, period: str) -> tuple[int, int]:
    """(amount, months) the member is billed per cycle in `period`."""
    return member_fee(team, member, plan_for(member, plans, period))


def _shape(team: Team, m: Member, plans: dict, period: str, mine: set, today, keep_from: str | None = None):
    """The fee bill covering `period` for member m: (plan, first month, months, price, own due date), or None
    when there's nothing to pay. It starts at the standard period's start, or later: at the plan's first
    month, or past months other bills already cover (then it's priced for the months left). A bill that
    starts late because the member joined then gets its own due date, the deadline counted from today."""
    x = membership_for(m, plans, period)
    plan = plans[x.plan_id] if x else None
    amount, months = member_fee(team, m, plan)
    if amount <= 0:
        return None
    start = first = cycle_start(period, months)
    if x is not None and x.start and x.start > first:
        first = x.start
    while first < period and first in mine:
        first = add_months(first, 1)
    if keep_from is not None and months == 1:
        first = min(first, keep_from)
    k, price = cycle_bill(amount, months, first, x.until if x is not None else None)
    due_on = None
    if x is not None and first > start and first == x.start:
        tm = terms(team, plan)
        due_on = max(due_date(tm, first), today + timedelta(days=tm.due_days))
    return plan, first, k, price, due_on


def bill_due(team: Team, plans: dict, b: Bill):
    """When a fee bill is due: its own date, else its package's for its first month."""
    return b.due_on or due_date(terms_of(team, plans, b.plan_id), b.period)


def covering(uow: UnitOfWork, team: Team, period: str) -> dict[int, tuple[Bill, Member]]:
    """Each member's fee bill that covers `period` (a monthly bill for it, or a longer one started earlier)."""
    out: dict[int, tuple[Bill, Member]] = {}
    for b, m in uow.bills.fee_bills_since(team.id, add_months(period, 1 - max(CYCLES))):
        if covers(b.period, b.months, period):
            out[m.id] = (b, m)
    return out


def covered_months(uow: UnitOfWork, team: Team, since: str) -> dict[int, set[str]]:
    """For each member, the months from `since` on that a fee bill (any status) already covers."""
    out: dict[int, set[str]] = {}
    for b, m in uow.bills.fee_bills_since(team.id, add_months(since, 1 - max(CYCLES))):
        for i in range(b.months or 1):
            out.setdefault(m.id, set()).add(add_months(b.period, i))
    return out


def ensure_bills(uow: UnitOfWork, d: Deps, team: Team, period: str | None = None, _retry: bool = True) -> int:
    """Bill every active member whose fee isn't covered for this month yet. Bills follow standard periods:
    a quarterly bill covers Jan–Mar, Apr–Jun, Jul–Sep or Oct–Dec. It starts at the period's first month,
    or at the first month not already covered (then it's priced for the months left). Idempotent."""
    period = period or d.clock.current_period()
    plans, today = plans_of(uow, team), d.clock.local_now().date()
    covered = covered_months(uow, team, cycle_start(period, max(CYCLES)))
    created = 0
    for m in uow.members.of_team(team.id, active_only=True):
        mine = covered.get(m.id, set())
        shape = None if period in mine else _shape(team, m, plans, period, mine, today)
        if shape is None:
            continue
        plan, first, k, price, due_on = shape
        uow.bills.add(Bill(team_id=team.id, member_id=m.id, kind="monthly", title="monthly", period=first,
                           amount=price, months=k if k > 1 else None, plan_id=plan.id if plan else None,
                           due_on=due_on, monthly_key=f"{m.id}:{first}", created_at=d.clock.now()))
        created += 1
    if created:
        try:
            uow.commit()
        except Conflict:  # the scheduler and a request raced; reread and fill the gaps
            uow.rollback()
            return ensure_bills(uow, d, team, period, _retry=False) if _retry else 0
    return created


def update_late_fees(uow: UnitOfWork, d: Deps, team: Team) -> None:
    """Set each overdue fee bill's late fee (its own "late" bill) to days past its due date × its package's
    rate, counted from the bill's real due date (a quarter's: its first month's), whenever the fee was set.
    Runs once a day (the late_fees job, just after midnight) and right after a package's terms change.
    Runs with ensure_bills, so amounts are current whenever someone looks or pays. A late bill stops
    growing once the fee is paid, and stays as it is for paused members; turning the fee off waives
    the late bills still unpaid."""
    today, plans = d.clock.local_now().date(), plans_of(uow, team)
    late = {b.monthly_key: b for b in uow.bills.late_of_team(team.id)}
    changed = False
    for b, m in uow.bills.unpaid_with_members(team.id):
        if b.kind != "monthly" or m.status != "active":
            continue
        key = f"late:{b.id}"
        lb = late.get(key)
        # paid, or waived by a manager: leave it. One this job set to nothing ("late:0") may come back.
        if lb is not None and lb.status != "unpaid" and not (lb.status == "waived" and lb.title == "late:0"):
            continue
        rate = terms_of(team, plans, b.plan_id).late_fee
        days = late_days(bill_due(team, plans, b), today, None) if rate else 0  # always from the real due date
        if days > 0:
            if lb is None:
                uow.bills.add(Bill(team_id=team.id, member_id=b.member_id, kind="late", title=f"late:{days}",
                                   period=b.period, months=b.months, amount=days * rate, monthly_key=key,
                                   plan_id=b.plan_id, created_at=d.clock.now()))
                changed = True
            elif (lb.amount, lb.title, lb.status) != (days * rate, f"late:{days}", "unpaid"):
                lb.amount, lb.title, lb.status, changed = days * rate, f"late:{days}", "unpaid", True
        elif lb is not None and lb.status == "unpaid":  # not overdue any more (a later due date) or no fine
            lb.title, lb.status, changed = "late:0", "waived", True
    if changed:
        try:
            uow.commit()
        except Conflict:  # two requests added the same late bill; the other one's is as good
            uow.rollback()


def reprice_current_month(uow: UnitOfWork, d: Deps, team: Team) -> None:
    """After a fee or plan change, the unpaid fee bill covering this month follows the member's new fee and
    cycle (from the month it starts). Paid bills stay as they were; the new fee starts when they run out."""
    period, plans, today = d.clock.current_period(), plans_of(uow, team), d.clock.local_now().date()
    covered = covered_months(uow, team, cycle_start(period, max(CYCLES)))
    for bill, member in covering(uow, team, period).values():
        if bill.status != "unpaid":
            continue
        # other bills' months stay covered; this bill's are free to be redrawn
        others = covered.get(member.id, set()) - {add_months(bill.period, i) for i in range(bill.months or 1)}
        shape = _shape(team, member, plans, period, others, today, keep_from=bill.period)
        if shape is None or shape[1] > period:  # nothing owed for this month (any more)
            _void(uow, team, bill)
            continue
        plan, first, k, price, due_on = shape
        if (first, k, plan.id if plan else None) == (bill.period, bill.months or 1, bill.plan_id):
            due_on = bill.due_on if due_on is not None else None  # the same bill: keep the deadline it was given
        bill.period, bill.monthly_key = first, f"{member.id}:{first}"
        bill.amount, bill.months = price, k if k > 1 else None
        bill.plan_id, bill.due_on = (plan.id if plan else None), due_on


def _void(uow: UnitOfWork, team: Team, bill: Bill) -> None:
    """A fee bill a plan change made unnecessary: it stops counting as the member's fee for its months (so a
    later change can bill them again), and its unpaid fine goes with it. Unlike a manager's waive, which
    forgives those months for good."""
    bill.kind, bill.status, bill.monthly_key = "replaced", "waived", None
    for lb in uow.bills.late_of_team(team.id):
        if lb.monthly_key == f"late:{bill.id}" and lb.status == "unpaid":
            lb.status = "waived"


def _group(team: Team, plans: dict, plan_id, amount: int, months: int, count: int, period: str,
           due=None) -> dict:
    tm = terms_of(team, plans, plan_id)
    due = due or due_date(tm, period)
    return {"plan_id": plan_id, "amount": amount, "months": months, "count": count,
            "notify": notify_date(tm, period), "due_on": due, "due": due.strftime("%d/%m"),
            "late_fee": tm.late_fee}


def fee_groups(uow: UnitOfWork, d: Deps, team: Team) -> list[dict]:
    """What active members pay, per package: [{plan_id, amount, months, count, due, late_fee}], monthly first.
    `due` is the due date of the package's current period (a quarter's: its first month's)."""
    plans, groups, period = plans_of(uow, team), {}, d.clock.current_period()
    for m in uow.members.of_team(team.id, active_only=True):
        plan = plan_for(m, plans, period)
        amount, months = member_fee(team, m, plan)
        if amount > 0:
            key = (months, amount, plan.id if plan else None)
            groups[key] = groups.get(key, 0) + 1
    return [_group(team, plans, pid, a, n, c, cycle_start(period, n))
            for (n, a, pid), c in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0))]


def opened_groups(uow: UnitOfWork, d: Deps, team: Team, period: str) -> list[dict]:
    """Fee bills to announce in `period`, per package: those that start then, plus those made this month
    for a period that started earlier (a quarterly plan set up mid-quarter). Each group carries its bills'
    own start month, notify and due dates."""
    plans, groups = plans_of(uow, team), {}
    start, end = d.clock.month_bounds(period)
    for b, m in uow.bills.fee_bills_since(team.id, add_months(period, 1 - max(CYCLES))):
        if m.status == "left" or not covers(b.period, b.months, period):
            continue
        if b.period == period or (b.created_at and start <= b.created_at < end):
            key = (b.period, b.months or 1, b.amount, b.plan_id if b.plan_id in plans else None, b.due_on)
            groups[key] = groups.get(key, 0) + 1
    return [{**_group(team, plans, pid, a, n, c, p, due), "period": p} for (p, n, a, pid, due), c in sorted(
        groups.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][3] or 0, kv[0][0], kv[0][4] or date.min))]


def has_fees(uow: UnitOfWork, team: Team, period: str) -> bool:
    plans = plans_of(uow, team)
    return team.fee > 0 or any(fee_of(team, m, plans, period)[0] > 0
                               for m in uow.members.of_team(team.id, active_only=True))


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


def create_donation(uow: UnitOfWork, d: Deps, team: Team, amount, donor: str | None,
                    payer_tg_id: int | None) -> dict:
    """A QR code to give `amount` (a multiple of 1.000đ) to the fund. Anyone may; no bills involved."""
    if not team.has_bank():
        raise DomainError(400, "bank_not_set")
    if not donation_ok(amount):
        raise DomainError(400, "donation_amount")
    donor = " ".join(str(donor or "").split())[:DONOR_NAME_MAX] or ("Anonymous" if team.lang == "en" else "Ẩn danh")
    code = new_payment_code()
    while uow.intents.code_exists(code):
        code = new_payment_code()
    now = d.clock.now()
    intent = PaymentIntent(team_id=team.id, code=code, amount=amount, payer_tg_id=payer_tg_id, bill_ids=[],
                           member_names=[donor], kind="donation", created_at=now, expires_at=now + INTENT_TTL)
    uow.intents.add(intent)
    uow.commit()
    content = transfer_content(team.bank_name, code)
    return {"code": code, "content": content, "amount": amount, "donation": True,
            "qr_url": d.qr.url(team.bank_name, team.pay_to(), amount, content),
            "bank_name": team.bank_name, "bank_account": team.pay_to(), "bank_holder": team.bank_holder,
            "members": [donor], "expires_at": d.clock.fmt(intent.expires_at)}


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
    if intent is None and codes and uow.intents.other_team_has(team.id, codes):
        return None  # another team's QR paid into a bank account both teams use: that team records it
    if intent is None and uow.teams.shares_account(team):
        return None  # a shared account: only transfers with this team's QR code are this team's; the rest is ignored
    payment = Payment(team_id=team.id, source="sepay", amount=transfer.amount, sepay_tx_id=tx_id,
                      content=transfer.content[:500], created_at=d.clock.now())
    uow.payments.add(payment)
    try:
        uow.flush()
    except Conflict:  # the same webhook delivered twice at the same moment
        uow.rollback()
        return None

    if intent and intent.kind == "donation":  # whatever arrived is given to the fund
        donor = (intent.member_names or [""])[0]
        payment.intent_id, payment.payer_tg_id = intent.id, intent.payer_tg_id
        payment.source, payment.status, payment.note = "donation", "matched", donor
        intent.status, intent.payment_id = "paid", payment.id
        uow.commit()
        return {"type": "donation_received", "team_id": team.id, "payer_tg_id": intent.payer_tg_id, "donor": donor,
                "amount": transfer.amount, "to": uow.followers.active_ids(team.id)}

    needed, reason, names = 0, "no_code", []
    if intent:
        payment.intent_id, payment.payer_tg_id = intent.id, intent.payer_tg_id
        bills = uow.bills.get_many(intent.bill_ids, unpaid_only=True)
        needed = sum(b.amount for b in bills)
        if bills and intent_expired(intent, d):
            reason = "expired"  # its bills may have changed since: a manager says who this was for
        elif bills and intent.amount <= transfer.amount < needed:
            # a late fee grew after the QR was made: the day they made it is the day that counts
            short = needed - transfer.amount
            for lb in sorted((b for b in bills if b.kind == "late"), key=lambda b: -b.id):
                cut = min(short, lb.amount)
                lb.amount, short = lb.amount - cut, short - cut
            if short == 0:
                bills = [b for b in bills if b.amount > 0]
                needed = transfer.amount
        if reason == "expired":
            pass
        elif not bills:
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
                "members": names, "amount": transfer.amount, "to": uow.followers.active_ids(team.id)}
    ev = {"type": "unmatched_payment", "team_id": team.id, "payment_id": payment.id, "amount": transfer.amount,
          "content": transfer.content[:200], "reason": reason, "needed": needed,
          "at": d.clock.fmt(payment.created_at), "admins": [a.tg_id for a in uow.admins.of_team(team.id)]}
    if reason == "expired":
        ev.update(expired_details(uow, d, intent))
        ev["payer_tg_id"] = intent.payer_tg_id
    return ev


def intent_expired(intent: PaymentIntent, d: Deps) -> bool:
    return intent.status == "expired" or (intent.expires_at is not None and intent.expires_at < d.clock.now())


def expired_details(uow: UnitOfWork, d: Deps, intent: PaymentIntent) -> dict:
    """Who an expired QR was made for, and which of them still owe: the manager's starting point."""
    owing = sorted({b.member_id for b in uow.bills.get_many(intent.bill_ids, unpaid_only=True)})
    return {"qr_for": list(intent.member_names or []), "qr_made": d.clock.fmt(intent.created_at),
            "qr_amount": intent.amount, "member_ids": owing}


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
    """A member paid `amount` in cash. The money goes to their oldest bills first.
    Less than they owe: the bill where it runs out is either settled at what was paid (settle=True: the rest
    is let go, e.g. someone who joined mid-quarter) or split into a paid part and a new unpaid bill for the
    rest; bills the money doesn't reach stay unpaid. More than they owe: every bill is paid and the extra
    stays in the fund as part of the same payment. The fund grows by `amount` either way."""
    groups = unpaid_by_member(uow, team, [member_id])
    if not groups:
        raise DomainError(400, "nothing_to_pay")
    g = groups[0]
    if not 0 < amount <= g["total"] + MAX_DONATION:
        raise DomainError(400, "cash_amount")
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
    extra = max(0, amount - g["total"])
    if extra:
        p.note = f"{g['name']} (+{extra})"
    return {"members": [g["name"]], "amount": amount, "owed": g["total"], "let_go": let_go, "extra": extra,
            "still_owed": max(0, g["total"] - amount - let_go)}  # the split-off rest plus bills the money didn't reach


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
    return {"period": period, "period_label": period_label(period), "fee": team.fee,
            "fees": [{k: v for k, v in g.items() if k not in ("notify", "due_on")} for g in fee_groups(uow, d, team)],
            "has_fees": has_fees(uow, team, d.clock.current_period()),
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
