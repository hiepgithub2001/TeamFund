"""Use cases for the team's pay page: what someone without Telegram may do.

The page is reached by a secret link (/p/<public_token>/) that managers share. With it, anyone can see who
still owes and get a QR code to pay for chosen members, exactly like /start in the team bot. Nothing else:
no member details beyond names, no managing. Managers keep using Telegram."""
from ..domain.entities import Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import period_label
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
            return {"team": team.name, "lang": team.lang, "period_label": period_label(period),
                    "due_day": team.due_day, "fee": team.fee, "has_bank": team.has_bank(),
                    "bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder,
                    "bot_username": team.bot_username, "unpaid": unpaid,
                    "total_unpaid": sum(u["total"] for u in unpaid)}

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
