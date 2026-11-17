"""Use cases: team settings, bank account, SePay key, group link, managers, audit log."""
import hmac
import re
import secrets
from datetime import timedelta

from ..domain.entities import MANAGER, OWNER, ManagerRequest, Team, TeamAdmin
from ..domain.errors import DomainError, invalid
from ..domain.rules import LANGS, MAX_FEE, hash_secret, new_public_token, valid_bank
from .access import get_team, is_super, manager_team, owner_team, role_of
from .billing import reprice_current_month
from .people import audit, display_name, user_info
from .ports import Deps, SepayError, SepayWebhookSpec, UnitOfWork

REQUEST_COOLDOWN = timedelta(hours=24)
SEPAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _sepay_failed(e: SepayError) -> DomainError:
    """SePay's answer, in the words the bot shows the owner."""
    if e.kind == "unauthorized":
        return DomainError(400, "sepay_token_invalid")
    if e.kind == "invalid":
        return DomainError(502, "sepay_rejected", message=e.message[:200] or "-")
    return DomainError(502, "sepay_unavailable")


def check_api_key(uow: UnitOfWork, team_id: int, key: str) -> Team:
    """The team whose payment webhook this is, if the API key matches."""
    team = uow.teams.get(team_id)
    if (not team or not team.active or not team.sepay_key_hash
            or not hmac.compare_digest(hash_secret(key), team.sepay_key_hash)):
        raise DomainError(401, "unauthorized")
    return team


class TeamUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _webhook_url(self, team: Team) -> str:
        return f"{self.d.config.public_url}/sepay/{team.id}"

    def team_info(self, team_id: int, actor: int | None) -> dict:
        with self.d.uow() as uow:
            t = get_team(uow, team_id)
            return {"team_id": t.id, "name": t.name, "bot_username": t.bot_username, "fee": t.fee,
                    "due_day": t.due_day, "lang": t.lang, "bank_name": t.bank_name, "bank_account": t.bank_account,
                    "bank_holder": t.bank_holder, "sepay_connected": bool(t.sepay_key_hash),
                    "sepay_url": self._webhook_url(t), "group_chat_id": t.group_chat_id,
                    "role": role_of(uow, t.id, actor)}

    def role(self, team_id: int, actor: int | None) -> dict:
        with self.d.uow() as uow:
            get_team(uow, team_id)
            return {"role": role_of(uow, team_id, actor), "is_super": is_super(self.d.config, actor)}

    def update_settings(self, team_id: int, actor: int, fee: int | None = None, due_day: int | None = None,
                        lang: str | None = None) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            if (fee is not None and not 0 <= fee <= MAX_FEE) or (due_day is not None and not 1 <= due_day <= 28) \
                    or (lang is not None and lang not in LANGS):
                raise invalid()
            before = {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}
            changes = {k: v for k, v in (("fee", fee), ("due_day", due_day), ("lang", lang)) if v is not None}
            for k, v in changes.items():
                setattr(team, k, v)
            if "fee" in changes:
                reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "team.settings", before=before, after=changes)
            uow.commit()
            return {"fee": team.fee, "due_day": team.due_day, "lang": team.lang}

    def set_bank(self, team_id: int, actor: int, bank_name: str, bank_account: str, bank_holder: str) -> dict:
        bank_name, bank_account, bank_holder = bank_name.strip(), bank_account.strip(), bank_holder.strip().upper()
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            if not valid_bank(bank_name, bank_account, bank_holder):
                raise invalid()
            before = {"bank_name": team.bank_name, "bank_account": team.bank_account, "bank_holder": team.bank_holder}
            team.bank_name, team.bank_account, team.bank_holder = bank_name, bank_account, bank_holder
            after = {"bank_name": bank_name, "bank_account": bank_account, "bank_holder": bank_holder}
            audit(uow, self.d.clock, team.id, actor, "team.bank", before=before, after=after)
            uow.commit()
            return {**after, "by": display_name(uow, actor)}

    def sepay_info(self, team_id: int, actor: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            return {"webhook_url": self._webhook_url(team), "connected": bool(team.sepay_key_hash),
                    "token_set": bool(team.sepay_token_enc), "webhook_id": team.sepay_webhook_id,
                    "has_bank": team.has_bank()}

    def set_sepay_token(self, team_id: int, actor: int, token: str) -> dict:
        """Keep the owner's SePay API token (encrypted) after checking that SePay accepts it."""
        token = token.strip()
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            if not SEPAY_TOKEN_RE.match(token):
                raise invalid()
            try:
                accounts = self.d.sepay.bank_accounts(token)
            except SepayError as e:
                raise _sepay_failed(e) from e
            team.sepay_token_enc = self.d.cipher.encrypt(token)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_token")
            uow.commit()
            return {"bank_accounts": len(accounts)}

    def sync_sepay(self, team_id: int, actor: int) -> dict:
        """Make the team's one SePay webhook match the team's bank account, creating it or overwriting it.

        Errors: bank_not_set, sepay_token_missing, sepay_bank_missing (the account isn't linked in SePay),
        sepay_token_invalid, sepay_rejected, sepay_unavailable."""
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            if not team.has_bank():
                raise DomainError(400, "bank_not_set")
            if not team.sepay_token_enc:
                raise DomainError(400, "sepay_token_missing")
            token = self.d.cipher.decrypt(team.sepay_token_enc)
            api, url = self.d.sepay, self._webhook_url(team)
            try:
                accounts = api.bank_accounts(token)
                account = next((a for a in accounts if a.active and _digits(a.account_number) == _digits(team.bank_account)),
                               None)
                if not account:
                    raise DomainError(404, "sepay_bank_missing", bank=team.bank_name, account=team.bank_account)
                key = secrets.token_urlsafe(24)
                spec = SepayWebhookSpec(name=f"Team Fund · {team.name}"[:100], url=url,
                                        bank_account_xid=account.xid, api_key=key)
                webhook_id, created = team.sepay_webhook_id, False
                if not webhook_id:  # adopt a webhook made by hand for this team, so there's still only one
                    webhook_id = next((w.id for w in api.webhooks(token) if w.url.rstrip("/") == url), None)
                if webhook_id:
                    try:
                        api.update_webhook(token, webhook_id, spec)
                    except SepayError as e:
                        if e.kind == "not_found":      # deleted in SePay: make a new one
                            webhook_id = None
                        elif e.kind == "invalid":      # can't be changed in place: replace it
                            old, webhook_id = webhook_id, api.create_webhook(token, spec)
                            created = True
                            try:
                                api.delete_webhook(token, old)
                            except SepayError:
                                pass
                        else:
                            raise
                if not webhook_id:
                    webhook_id, created = api.create_webhook(token, spec), True
            except SepayError as e:
                raise _sepay_failed(e) from e
            team.sepay_webhook_id, team.sepay_key_hash = webhook_id, hash_secret(key)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_webhook",
                  after={"webhook_id": webhook_id, "account": team.bank_account, "created": created})
            uow.commit()
            return {"webhook_id": webhook_id, "created": created, "url": url, "bank": account.bank_short_name,
                    "account": account.account_number, "holder": account.holder}

    def new_sepay_key(self, team_id: int, actor: int) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            key = secrets.token_urlsafe(24)
            team.sepay_key_hash = hash_secret(key)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_key")
            uow.commit()
            return {"api_key": key, "webhook_url": self._webhook_url(team)}

    def web_link(self, team_id: int, actor: int, reset: bool = False) -> dict:
        """The team's pay page link for people without Telegram. Made on first use; reset makes a new one
        and the old link stops working."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            if reset or not team.public_token:
                team.public_token = new_public_token()
                audit(uow, self.d.clock, team.id, actor, "team.web_link", after={"reset": reset})
                uow.commit()
            return {"url": f"{self.d.config.public_url}/p/{team.public_token}/"}

    def link_group(self, team_id: int, actor: int, chat_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            before = team.group_chat_id
            team.group_chat_id = chat_id
            audit(uow, self.d.clock, team.id, actor, "team.group", before={"chat_id": before},
                  after={"chat_id": chat_id})
            uow.commit()
            return {"group_chat_id": chat_id}

    # ---- admins -------------------------------------------------------------------------------

    def list_admins(self, team_id: int) -> list[dict]:
        with self.d.uow() as uow:
            return [{**user_info(uow, a.tg_id), "role": a.role} for a in uow.admins.of_team(team_id)]

    def remove_admin(self, team_id: int, actor: int, tg_id: int) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            a = uow.admins.get(team.id, tg_id)
            if not a or a.role != MANAGER:
                raise DomainError(404, "not_a_manager")
            name = display_name(uow, tg_id)
            uow.admins.remove(a)
            audit(uow, self.d.clock, team.id, actor, "admin.remove", target=name)
            uow.commit()
            return {"tg_id": tg_id, "name": name}

    def transfer_owner(self, team_id: int, actor: int, tg_id: int) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            target = uow.admins.get(team.id, tg_id)
            if not target or target.role != MANAGER:
                raise DomainError(404, "not_a_manager")
            me = uow.admins.get(team.id, actor)
            me.role, target.role, team.owner_tg_id = MANAGER, OWNER, tg_id
            name = display_name(uow, tg_id)
            audit(uow, self.d.clock, team.id, actor, "team.transfer_owner", target=name)
            uow.commit()
            return {"tg_id": tg_id, "name": name, "previous_owner": actor}

    # ---- manager requests ---------------------------------------------------------------------

    def _mreq_dict(self, uow: UnitOfWork, r: ManagerRequest, team: Team) -> dict:
        return {"id": r.id, "status": r.status, **user_info(uow, r.tg_id), "owner_tg_id": team.owner_tg_id,
                "created_at": self.d.clock.fmt(r.created_at)}

    def request_manager(self, team_id: int, actor: int) -> dict:
        with self.d.uow() as uow:
            team = get_team(uow, team_id)
            if role_of(uow, team.id, actor):
                raise DomainError(409, "already_manager")
            if uow.manager_requests.has_pending(team.id, actor):
                raise DomainError(409, "request_pending")
            now = self.d.clock.now()
            last_reject = uow.manager_requests.last_rejected_at(team.id, actor)
            if last_reject and now - last_reject < REQUEST_COOLDOWN:
                raise DomainError(429, "cooldown", until=self.d.clock.fmt(last_reject + REQUEST_COOLDOWN))
            r = ManagerRequest(team_id=team.id, tg_id=actor, created_at=now)
            uow.manager_requests.add(r)
            uow.commit()
            return self._mreq_dict(uow, r, team)

    def pending_manager_requests(self, team_id: int, actor: int) -> list[dict]:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            return [self._mreq_dict(uow, r, team) for r in uow.manager_requests.pending(team.id)]

    def decide_manager_request(self, team_id: int, actor: int, request_id: int, approve: bool) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            r = uow.manager_requests.get(request_id)
            if not r or r.team_id != team.id:
                raise DomainError(404, "not_found")
            if r.status != "pending":
                raise DomainError(409, "already_decided", status=r.status)
            now = self.d.clock.now()
            r.status, r.decided_at = ("approved" if approve else "rejected"), now
            if approve and not role_of(uow, team.id, r.tg_id):
                uow.admins.add(TeamAdmin(team_id=team.id, tg_id=r.tg_id, role=MANAGER, added_by=actor, added_at=now))
            audit(uow, self.d.clock, team.id, actor, "manager_request." + r.status, target=display_name(uow, r.tg_id))
            uow.commit()
            return self._mreq_dict(uow, r, team)

    def audit_log(self, team_id: int, actor: int, limit: int = 20) -> list[dict]:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            return [{"at": self.d.clock.fmt(a.created_at), "actor": display_name(uow, a.actor_tg_id),
                     "action": a.action, "target": a.target, "before": a.before, "after": a.after}
                    for a in uow.audit.latest(team.id, min(limit, 100))]
