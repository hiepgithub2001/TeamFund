"""Use cases for the team's pay page: what someone without Telegram may see and do.

The page is reached by a secret link (/p/<public_token>/) that managers share. With it, anyone can read what a
member can read in the team bot (/start, /status, /fund, /history, /bank) plus the roster, and get a QR code to
pay for chosen members. Everything else is read-only: managing stays in Telegram. The roster leaves out phone
numbers and notes, and payments are labelled by whom they paid for, never by the raw bank memo (which can carry
the sender's full name and bank references)."""
from ..domain.entities import Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import effective_fee, period_label
from . import billing
from .ports import Deps, UnitOfWork

MAX_MEMBERS_PER_PAYMENT = 50


def _team(uow: UnitOfWork, token: str) -> Team:
    team = uow.teams.by_public_token(token) if token and len(token) <= 64 else None
    if not team:
        raise DomainError(404, "not_found")
    return team


class WebUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def page(self, token: str) -> dict:
        """Everything the page shows: the team, its bank account, and who owes what."""
        with self.d.uow() as uow:
            team = _team(uow, token)
            billing.ensure_bills(uow, self.d, team)
            unpaid = [{"member_id": g["member_id"], "name": g["name"], "total": g["total"],
                       "bills": [{"kind": b["kind"], "title": b["title"], "period_label": b["period_label"],
                                  "amount": b["amount"]} for b in g["bills"]]}
                      for g in billing.unpaid_by_member(uow, team)]
            period = self.d.clock.current_period()
            fund = billing.fund(uow, team)
            return {"team": team.name, "lang": team.lang, "period_label": period_label(period),
                    "due_day": team.due_day, "fee": team.fee, "has_bank": team.has_bank(),
                    "bank_name": team.bank_name, "bank_account": team.pay_to(), "bank_holder": team.bank_holder,
                    "bot_username": team.bot_username, "unpaid": unpaid,
                    "total_unpaid": sum(u["total"] for u in unpaid),
                    "paid": billing.team_status(uow, self.d, team)["paid"],
                    "fund": {k: fund[k] for k in ("income", "expense", "balance")},
                    "months": self._months(uow, team, period, 6), "history": self._history(uow, team, 30),
                    "members": self._members(uow, team)}

    def _months(self, uow: UnitOfWork, team: Team, period: str, n: int) -> list[dict]:
        """Money in and out for the last n months, newest first."""
        y, m = map(int, period.split("-"))
        out = []
        for _ in range(n):
            p = f"{y:04d}-{m:02d}"
            out.append({"period_label": period_label(p), **billing.month_totals(uow, self.d, team, p)})
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return out

    def _history(self, uow: UnitOfWork, team: Team, limit: int) -> list[dict]:
        payments = uow.payments.latest(team.id, limit)
        names = uow.intents.members_of([p.intent_id for p in payments if p.intent_id])
        items = []
        for p in payments:
            who = names.get(p.intent_id) if p.intent_id else None
            label = ", ".join(who) if who else (p.note if p.status != "unmatched" else None)
            items.append({"type": "in", "amount": p.amount, "kind": "unassigned" if label is None else p.source,
                          "title": label or "", "ts": p.created_at})
        items += [{"type": "out", "amount": e.amount, "kind": "expense", "title": e.title, "ts": e.created_at}
                  for e in uow.expenses.latest(team.id, limit)]
        items.sort(key=lambda i: i["ts"], reverse=True)
        return [{k: v for k, v in i.items() if k != "ts"} | {"at": self.d.clock.fmt(i["ts"])} for i in items[:limit]]

    def _members(self, uow: UnitOfWork, team: Team) -> list[dict]:
        """The roster (members who haven't left), without phone numbers or notes."""
        members = sorted(uow.members.of_team(team.id), key=lambda m: m.name.casefold())
        return [{"name": m.name, "nickname": m.nickname, "shirt_number": m.shirt_number, "status": m.status,
                 "fee": effective_fee(team, m), "owes": sum(b.amount for b in uow.bills.unpaid_of_member(m.id))}
                for m in members]

    def create_intent(self, token: str, member_ids: list) -> dict:
        """A QR code for the chosen members' unpaid bills. No Telegram payer, so no private receipt;
        the team group still gets the "paid" post."""
        if (not isinstance(member_ids, list) or not 1 <= len(member_ids) <= MAX_MEMBERS_PER_PAYMENT
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in member_ids)):
            raise invalid()
        with self.d.uow() as uow:
            team = _team(uow, token)
            r = billing.create_intent(uow, self.d, team, list(dict.fromkeys(member_ids)), None)
            return {k: r[k] for k in ("code", "content", "amount", "qr_url", "bank_name", "bank_account",
                                      "bank_holder", "members", "expires_at")}

    def intent_status(self, token: str, code: str) -> dict:
        """pending | paid | expired, for the page to poll while the payer transfers."""
        with self.d.uow() as uow:
            team = _team(uow, token)
            intent = uow.intents.find_by_codes(team.id, [code]) if code and len(code) <= 16 else None
            if not intent:
                raise DomainError(404, "not_found")
            status = intent.status
            if status == "pending" and intent.expires_at and intent.expires_at < self.d.clock.now():
                status = "expired"
            return {"code": intent.code, "status": status, "amount": intent.amount, "members": intent.member_names}
