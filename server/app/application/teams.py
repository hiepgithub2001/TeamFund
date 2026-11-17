"""Use cases: team settings, bank account, SePay key, group link, managers, audit log."""
import hmac
import secrets
from datetime import timedelta

from ..domain.entities import MANAGER, OWNER, ManagerRequest, Team, TeamAdmin
from ..domain.errors import DomainError, invalid
from ..domain.rules import LANGS, MAX_FEE, hash_secret, valid_bank
from .access import get_team, is_super, manager_team, owner_team, role_of
from .billing import reprice_current_month
from .people import audit, display_name, user_info
from .ports import Deps, UnitOfWork

REQUEST_COOLDOWN = timedelta(hours=24)


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
            return {"webhook_url": self._webhook_url(team), "connected": bool(team.sepay_key_hash)}

    def new_sepay_key(self, team_id: int, actor: int) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            key = secrets.token_urlsafe(24)
            team.sepay_key_hash = hash_secret(key)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_key")
            uow.commit()
            return {"api_key": key, "webhook_url": self._webhook_url(team)}

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
