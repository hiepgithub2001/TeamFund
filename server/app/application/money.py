"""Use cases: paying, cash, charges, expenses, transfers that need checking, reports, export."""
from datetime import date, timedelta

from ..domain.entities import Expense, IncomingTransfer, Payment
from ..domain.errors import DomainError, invalid
from ..domain.rules import MAX_AMOUNT, MAX_BACKDATE_DAYS, period_label
from . import billing
from .access import get_team, manager_team
from .people import audit, display_name
from .ports import Deps
from .teams import check_api_key


def _ids(member_ids) -> list[int]:
    if not member_ids or len(member_ids) > 100 or not all(isinstance(i, int) for i in member_ids):
        raise invalid()
    return list(member_ids)


class MoneyUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def unpaid(self, team_id: int) -> list[dict]:
        """Everyone with something to pay. Open to anyone who opens the bot."""
        with self.d.uow() as uow:
            team = get_team(uow, team_id)
            billing.ensure_bills(uow, self.d, team)
            return billing.unpaid_by_member(uow, team)

    def create_intent(self, team_id: int, actor: int | None, member_ids: list[int]) -> dict:
        with self.d.uow() as uow:
            return billing.create_intent(uow, self.d, get_team(uow, team_id), _ids(member_ids), actor)

    def donate(self, team_id: int, actor: int | None, amount, donor: str | None, reason: str | None = None) -> dict:
        """A donation QR. Anyone who opens the bot may make one; `reason` (optional) says what for."""
        with self.d.uow() as uow:
            return billing.create_donation(uow, self.d, get_team(uow, team_id), amount, donor, actor, reason)

    def status(self, team_id: int) -> dict:
        with self.d.uow() as uow:
            return billing.team_status(uow, self.d, get_team(uow, team_id))

    def fund(self, team_id: int) -> dict:
        with self.d.uow() as uow:
            return billing.fund(uow, get_team(uow, team_id))

    def history(self, team_id: int, limit: int = 10, period: str | None = None) -> list[dict]:
        with self.d.uow() as uow:
            return billing.history(uow, self.d, get_team(uow, team_id), min(limit, 50), period)

    def record_transfer(self, team_id: int, api_key: str, transfer: IncomingTransfer) -> str | None:
        """A payment provider reported money arriving. Checks the key, matches the transfer to bills
        and publishes what happened. Returns the event type, or None if nothing changed."""
        with self.d.uow() as uow:
            event = billing.record_transfer(uow, self.d, check_api_key(uow, team_id, api_key), transfer)
        if event:
            self.d.notifier.publish(event)
        return event["type"] if event else None

    def markpaid(self, team_id: int, actor: int, member_ids: list[int]) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            result = billing.cash_payments(uow, self.d, team, _ids(member_ids), actor)
            audit(uow, self.d.clock, team.id, actor, "payment.cash", target=", ".join(result["members"]),
                  after={"amount": result["amount"]})
            uow.commit()
            return result

    def markpaid_amount(self, team_id: int, actor: int, member_id: int, amount: int, settle: bool) -> dict:
        """One member paid in cash a sum other than what they owe: record what came in. Less: settle=True
        lets the rest of that bill go, settle=False keeps the rest as a new unpaid bill. More: all paid,
        the extra stays in the fund."""
        if not isinstance(member_id, int) or not isinstance(amount, int) or isinstance(amount, bool):
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            r = billing.partial_cash(uow, self.d, team, member_id, amount, settle, actor)
            audit(uow, self.d.clock, team.id, actor, "payment.cash", target=r["members"][0],
                  after={"amount": amount, "owed": r["owed"], "let_go": r["let_go"], "still_owed": r["still_owed"],
                         "extra": r["extra"]})
            uow.commit()
            return r

    def owed_by(self, team_id: int, actor: int, member_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            g = billing.unpaid_by_member(uow, team, [member_id])
            if not g:
                raise DomainError(400, "nothing_to_pay")
            return {"member_id": member_id, "name": g[0]["name"], "total": g[0]["total"]}

    def charges(self, team_id: int, actor: int, title: str, amount: int, kind: str = "charge",
                member_ids: list[int] | None = None) -> dict:
        title = (title or "").strip()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            if kind not in ("charge", "split") or not title or len(title) > 100 or not 0 < amount <= MAX_AMOUNT:
                raise invalid()
            result = billing.create_charges(uow, self.d, team, title, amount,
                                            None if member_ids is None else _ids(member_ids), kind)
            audit(uow, self.d.clock, team.id, actor, f"bill.{kind}", target=title, after=result)
            uow.commit()
            return result

    def waive(self, team_id: int, actor: int, bill_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            b = uow.bills.get(bill_id)
            if not b or b.team_id != team.id:
                raise DomainError(404, "not_found")
            if b.status != "unpaid":
                raise DomainError(409, "already_resolved")
            b.status = "waived"
            for lb in uow.bills.late_of_team(team.id):
                if lb.monthly_key == f"late:{b.id}" and lb.status == "unpaid":
                    lb.status = "waived"
            audit(uow, self.d.clock, team.id, actor, "bill.waive", target=f"{b.title} {period_label(b.period)}",
                  after={"bill_id": b.id, "amount": b.amount})
            uow.commit()
            return billing.bill_dict(b)

    def today(self) -> date:
        """The teams' local date, for reading a date without a year ("15/10")."""
        return self.d.clock.local_now().date()

    def _when(self, on: date | None):
        """When a record happened: now, or noon of a past day the manager gave (not in the future,
        at most about three years back)."""
        if on is None:
            return self.d.clock.now()
        today = self.d.clock.local_now().date()
        if on > today or on < today - timedelta(days=MAX_BACKDATE_DAYS):
            raise DomainError(400, "bad_date")
        return self.d.clock.now() if on == today else self.d.clock.at_local(on)

    @staticmethod
    def _check(amount: int, title: str, limit: int) -> str:
        title = (title or "").strip()
        if not title or len(title) > limit or not isinstance(amount, int) or not 0 < amount <= MAX_AMOUNT:
            raise invalid()
        return title

    def add_expense(self, team_id: int, actor: int, amount: int, title: str, photo_file_id: str | None = None,
                    on: date | None = None) -> dict:
        title = self._check(amount, title, 200)
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            at = self._when(on)
            uow.expenses.add(Expense(team_id=team.id, amount=amount, title=title, photo_file_id=photo_file_id,
                                     created_by=actor, created_at=at))
            audit(uow, self.d.clock, team.id, actor, "expense.add", target=title,
                  after={"amount": amount, **({"date": on.isoformat()} if on else {})})
            uow.commit()
            return {"amount": amount, "title": title, "by": display_name(uow, actor), "at": self.d.clock.fmt(at),
                    "backdated": on is not None, "balance": billing.fund(uow, team)["balance"]}

    def add_income(self, team_id: int, actor: int, amount: int, title: str, on: date | None = None) -> dict:
        """Money into the fund that isn't a member's fee: a prize, a sponsor, the balance from before the bot."""
        title = self._check(amount, title, 200)
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            at = self._when(on)
            uow.payments.add(Payment(team_id=team.id, source="other", amount=amount, status="matched",
                                     recorded_by=actor, note=title, created_at=at))
            audit(uow, self.d.clock, team.id, actor, "income.add", target=title,
                  after={"amount": amount, **({"date": on.isoformat()} if on else {})})
            uow.commit()
            return {"amount": amount, "title": title, "by": display_name(uow, actor), "at": self.d.clock.fmt(at),
                    "backdated": on is not None, "balance": billing.fund(uow, team)["balance"]}

    # ---- editing the text of a transaction ----
    # expenses, income (/income, transfers kept as income) and donations; payments of members' bills are named
    # after the bills they paid, so their text isn't edited here

    @staticmethod
    def _editable(p: Payment) -> bool:
        return p.source in ("other", "donation") or p.status == "resolved"

    def _entry(self, e: Expense | Payment) -> dict:
        out = isinstance(e, Expense)
        return {"kind": "e" if out else "p", "id": e.id, "type": "out" if out else "in", "amount": e.amount,
                "title": (e.title if out else e.note or e.content) or "",
                "source": "expense" if out else e.source, "at": self.d.clock.fmt(e.created_at)}

    def _find(self, uow, team, kind: str, entry_id: int) -> Expense | Payment:
        if kind == "e":
            e = uow.expenses.get(entry_id)
        elif kind == "p":
            e = uow.payments.get(entry_id)
            e = e if e and self._editable(e) else None
        else:
            raise invalid()
        if not e or e.team_id != team.id:
            raise DomainError(404, "not_found")
        return e

    def editable(self, team_id: int, actor: int, limit: int = 10) -> list[dict]:
        """The latest transactions whose text a manager can change, newest first."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            rows = [p for p in uow.payments.latest(team.id, 50) if self._editable(p)][:limit]
            rows += uow.expenses.latest(team.id, limit)
            rows.sort(key=lambda e: e.created_at, reverse=True)
            return [self._entry(e) for e in rows[:limit]]

    def entry(self, team_id: int, actor: int, kind: str, entry_id: int) -> dict:
        with self.d.uow() as uow:
            return self._entry(self._find(uow, manager_team(uow, team_id, actor), kind, entry_id))

    def edit_text(self, team_id: int, actor: int, kind: str, entry_id: int, text: str) -> dict:
        """Change what a transaction says in the history ("Ủng hộ: Hoài Sơn · 500k from FC23"). Undoable."""
        text = " ".join(text.split()) if isinstance(text, str) else ""
        if not text or len(text) > 200:
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            e = self._find(uow, team, kind, entry_id)
            old = self._entry(e)["title"]
            if isinstance(e, Expense):
                e.title = text
            else:
                e.note = text
            audit(uow, self.d.clock, team.id, actor, "entry.edit", target=f"{old} → {text}",
                  before={"title": old}, after={"amount": e.amount, "title": text})
            uow.commit()
            return {**self._entry(e), "old": old}

    def unmatched(self, team_id: int, actor: int) -> list[dict]:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            out = []
            for p in uow.payments.unmatched(team.id):
                item = {"payment_id": p.id, "amount": p.amount, "content": p.content, "reason": p.reason,
                        "at": self.d.clock.fmt(p.created_at)}
                intent = uow.intents.get(p.intent_id) if p.reason == "expired" and p.intent_id else None
                if intent:
                    item.update(billing.expired_details(uow, self.d, intent))
                out.append(item)
            return out

    def assign(self, team_id: int, actor: int, payment_id: int, member_ids: list[int]) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            result = billing.assign_payment(uow, self.d, team, payment_id, _ids(member_ids), actor)
            audit(uow, self.d.clock, team.id, actor, "payment.assign", target=", ".join(result["members"]),
                  after={"payment_id": payment_id})
            uow.commit()
            return result

    def dismiss(self, team_id: int, actor: int, payment_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            p = billing.unmatched_payment(uow, team, payment_id)
            p.status, p.recorded_by, p.note = "resolved", actor, "other income"
            audit(uow, self.d.clock, team.id, actor, "payment.other_income", after={"payment_id": payment_id})
            uow.commit()
            return {"amount": p.amount}

    def export(self, team_id: int, actor: int) -> bytes:
        """Members, bills, payments and expenses as a spreadsheet."""
        fmt = self.d.clock.fmt
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            members = sorted(uow.members.of_team(team.id, include_left=True), key=lambda m: m.name.casefold())
            names = {m.id: m.name for m in members}
            sheets = [
                ("Members", ["ID", "Name", "Nickname", "Shirt", "Phone", "Note", "Custom fee", "Status"],
                 [[m.id, m.name, m.nickname, m.shirt_number, m.phone, m.note, m.fee_override, m.status]
                  for m in members]),
                ("Bills", ["ID", "Member", "Type", "Title", "Month", "Amount", "Status", "Paid at"],
                 [[b.id, names.get(b.member_id), b.kind, b.title, period_label(b.period), b.amount, b.status,
                   fmt(b.paid_at)] for b in uow.bills.of_team(team.id)]),
                ("Payments", ["ID", "Time", "Source", "Amount", "Status", "For", "Bank content"],
                 [[p.id, fmt(p.created_at), p.source, p.amount, p.status, p.note, p.content]
                  for p in uow.payments.of_team(team.id)]),
                ("Expenses", ["ID", "Time", "Amount", "Title"],
                 [[e.id, fmt(e.created_at), e.amount, e.title] for e in uow.expenses.of_team(team.id)]),
            ]
        return self.d.spreadsheet.workbook(sheets)
