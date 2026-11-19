"""Use cases: team settings, bank account, SePay key, group link, managers, audit log."""
import hmac
import logging
import re
import secrets
import unicodedata
from datetime import timedelta
from typing import Callable

from ..domain.entities import MANAGER, OWNER, Follower, ManagerRequest, Team, TeamAdmin
from ..domain.errors import DomainError, invalid
from ..domain.rules import (MAX_SEPAY_QUOTA, CYCLES, LANGS, MAX_DUE_DAYS, MAX_FEE, MAX_LATE_FEE, due_date, hash_secret, new_public_token,
                            schedule, terms, valid_bank)
from .access import get_team, is_super, manager_team, owner_team, role_of
from .billing import reprice_current_month, update_late_fees
from .people import audit, display_name, user_info
from .ports import Conflict, Deps, SepayError, SepayWebhookSpec, UnitOfWork

REQUEST_COOLDOWN = timedelta(hours=24)
log = logging.getLogger("teams")
Progress = Callable[[str, dict], None]  # (step, details), called as each step of /setbank finishes
SEPAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _ascii(text: str) -> str:
    """SePay shows non-ASCII characters in webhook names as "?": "Đội Bóng" -> "Doi Bong"."""
    text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in text if c.isascii() and not unicodedata.combining(c)).strip() or "team"


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
            notify, days = schedule(t)
            return {"team_id": t.id, "name": t.name, "bot_username": t.bot_username, "fee": t.fee,
                    "notify_day": notify, "due_days": days, "late_fee": t.late_fee or 0, "lang": t.lang, "bank_name": t.bank_name, "bank_account": t.bank_account,
                    "pay_account": t.pay_to(),
                    "bank_holder": t.bank_holder, "sepay_connected": bool(t.sepay_key_hash),
                    "sepay_url": self._webhook_url(t), "followers": len(uow.followers.active_ids(t.id)),
                    "role": role_of(uow, t.id, actor)}

    def role(self, team_id: int, actor: int | None) -> dict:
        with self.d.uow() as uow:
            get_team(uow, team_id)
            return {"role": role_of(uow, team_id, actor), "is_super": is_super(self.d.config, actor)}

    def update_settings(self, team_id: int, actor: int, fee: int | None = None, lang: str | None = None,
                        notify_day: int | None = None, due_days: int | None = None,
                        late_fee: int | None = None, fee_months: int | None = None) -> dict:
        """Change the default fee and its cycle (1, 3, 6 or 12 months), group language, the schedule (announce on
        notify_day, due due_days later) or the late fee per overdue day (0 turns it off)."""
        ok = lambda v, lo, hi: v is None or (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi)  # noqa: E731
        if not (ok(fee, 0, MAX_FEE) and ok(notify_day, 1, 28) and ok(due_days, 0, MAX_DUE_DAYS)
                and ok(late_fee, 0, MAX_LATE_FEE)) or (lang is not None and lang not in LANGS) \
                or (fee_months is not None and fee_months not in CYCLES):
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            fields = ("fee", "lang", "notify_day", "due_days", "late_fee", "fee_months")
            before = {k: getattr(team, k) for k in fields}
            changes = {k: v for k, v in zip(fields, (fee, lang, notify_day, due_days, late_fee, fee_months))
                       if v is not None}
            if (notify_day is None) != (due_days is None):  # the schedule is set as a pair
                cur = schedule(team)
                changes.setdefault("notify_day", cur[0])
                changes.setdefault("due_days", cur[1])
            if "late_fee" in changes:
                was_on = bool(team.late_fee)
                if changes["late_fee"] and not was_on:
                    team.late_fee_since = self.d.clock.local_now().date()
                elif not changes["late_fee"]:
                    team.late_fee_since = None
            for k, v in changes.items():
                setattr(team, k, v)
            if "fee" in changes or "fee_months" in changes:
                reprice_current_month(uow, self.d, team)
            audit(uow, self.d.clock, team.id, actor, "team.settings", before=before, after=changes)
            uow.commit()
            if {"late_fee", "notify_day", "due_days"} & changes.keys():
                update_late_fees(uow, self.d, team)
            notify, days = schedule(team)
            return {"fee": team.fee, "fee_months": team.fee_months or 1, "lang": team.lang, "notify_day": notify,
                    "due_days": days,
                    "late_fee": team.late_fee or 0,
                    "due": due_date(terms(team), self.d.clock.current_period()).strftime("%d/%m")}

    def sepay_plan(self, team_id: int, actor: int) -> dict:
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            return {"quota": team.sepay_quota, "day": team.sepay_cycle_day or 1}

    def set_sepay_plan(self, team_id: int, actor: int, quota: int | None, day: int | None = None) -> dict:
        """The owner's SePay plan: how many transactions a cycle allows and the day it restarts (None: forget it).
        Only for /ops: SePay's API doesn't tell the plan, only how many transactions it recorded."""
        if quota is not None and not (1 <= quota <= MAX_SEPAY_QUOTA and (day is None or 1 <= day <= 31)):
            raise invalid()
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            team.sepay_quota, team.sepay_cycle_day = quota, (day if quota is not None and day != 1 else None)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_plan", target=str(quota or "-"))
            uow.commit()
            return {"quota": team.sepay_quota, "day": team.sepay_cycle_day or 1}

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

    # ---- bank account: only from the owner's SePay -------------------------------------------------
    # The team's account is never typed: it is picked from the accounts linked in the owner's SePay, so
    # SePay is sure to watch it, the right VA is used for banks that need one, and the webhook follows.

    def _token(self, team: Team) -> str:
        if not team.sepay_token_enc:
            raise DomainError(400, "sepay_token_missing")
        return self.d.cipher.decrypt(team.sepay_token_enc)

    def bank_options(self, team_id: int, actor: int) -> list[dict]:
        """What the owner can pick: each linked account, or each of its VAs when the bank only reports VAs."""
        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            token, api = self._token(team), self.d.sepay
            try:
                options = []
                for a in api.bank_accounts(token):
                    if not a.active:
                        continue
                    base = {"xid": a.xid, "bank": a.bank_short_name, "account": a.account_number,
                            "holder": a.holder, "va": None, "needs_va": False,
                            "current": _digits(a.account_number) == _digits(team.bank_account)}
                    if not a.va_only:
                        options.append(base)
                        continue
                    vas = [v.va for v in api.virtual_accounts(token, a.xid) if v.active and v.official]
                    options += [{**base, "va": va, "current": base["current"] and va == team.pay_account}
                                for va in vas] or [{**base, "needs_va": True}]
                return options
            except SepayError as e:
                raise _sepay_failed(e) from e

    def choose_bank(self, team_id: int, actor: int, account_xid: str, va: str | None = None,
                    progress: Progress | None = None) -> dict:
        """Make one of the owner's SePay accounts (or one of its VAs) the team's, and create or overwrite the
        team's one webhook for it with a fresh key. Choosing the current account again repairs the webhook.

        SePay is asked again, so a stale or forged choice can't slip through. Each step is reported to
        `progress(step, info)` as it finishes, written to the server log, and kept in the audit log."""
        steps: list[dict] = []

        def done(step: str, **info):
            steps.append({"step": step, **info})
            log.info("team %s setbank: %s %s", team_id, step, info)
            if progress:
                progress(step, info)

        def start(step: str, **info):  # about to do something slow: say what, so a failure has a name
            log.info("team %s setbank: %s…", team_id, step)
            if progress:
                progress("start:" + step, info)

        with self.d.uow() as uow:
            team = owner_team(uow, team_id, actor)
            token, api = self._token(team), self.d.sepay
            try:
                start("accounts")
                accounts = api.bank_accounts(token)
                done("accounts", count=len(accounts))
                account = next((a for a in accounts if a.xid == account_xid and a.active), None)
                if not account:
                    raise DomainError(404, "not_found")
                number = re.sub(r"\s", "", account.account_number)
                done("account", bank=account.bank_short_name, account=number, va_only=account.va_only)
                if account.va_only:
                    start("va")
                pay_account = self._pick_va(api, token, account, va)
                if pay_account:
                    done("va", va=pay_account)
                if not valid_bank(account.bank_short_name, number, account.holder or "-"):
                    raise invalid()
                before = {"bank_name": team.bank_name, "bank_account": team.bank_account,
                          "bank_holder": team.bank_holder, "va": team.pay_account}
                team.bank_name, team.bank_account = account.bank_short_name, number
                team.bank_holder, team.pay_account = (account.holder or "").upper(), pay_account
                hook = self._point_webhook(api, token, team, account, done, start)
            except SepayError as e:
                raise _sepay_failed(e) from e
            after = {"bank_name": team.bank_name, "bank_account": team.bank_account,
                     "bank_holder": team.bank_holder, "va": pay_account}
            audit(uow, self.d.clock, team.id, actor, "team.bank", before=before, after=after)
            audit(uow, self.d.clock, team.id, actor, "team.sepay_webhook", target=hook["name"],
                  after={"webhook_id": hook["webhook_id"], "action": hook["action"], "account": team.bank_account})
            start("save")
            uow.commit()
            done("saved")
            return {**hook, "bank": account.bank_short_name, "account": number, "holder": team.bank_holder,
                    "va": pay_account, "changed": before != after, "by": display_name(uow, actor), "steps": steps}

    @staticmethod
    def _pick_va(api, token: str, account, wanted: str | None) -> str | None:
        """The VA payers must use, or None when the bank reports transfers to the main account."""
        if not account.va_only:  # e.g. BIDV personal is VA-only: SePay never sees the main number
            return None
        vas = [v.va for v in api.virtual_accounts(token, account.xid) if v.active and v.official]
        if not vas:
            raise DomainError(400, "sepay_va_missing", bank=account.bank_short_name, account=account.account_number)
        if wanted and wanted not in vas:  # gone, inactive, or not this account's
            raise DomainError(404, "not_found")
        return wanted or vas[0]

    def _point_webhook(self, api, token: str, team: Team, account, done, start) -> dict:
        """Create or overwrite the team's one webhook for this account with a new key; stores the key's hash.
        action: updated | adopted (a webhook made by hand for this team) | created | replaced."""
        url = self._webhook_url(team)
        name = f"Team Fund - {_ascii(team.name)}"[:100]
        key = secrets.token_urlsafe(24)
        done("key")
        spec = SepayWebhookSpec(name=name, url=url, bank_account_xid=account.xid, api_key=key)
        start("webhook", name=name, url=url)
        webhook_id, action = team.sepay_webhook_id, "updated"
        if not webhook_id:  # adopt a webhook made by hand for this team, so there's still only one
            webhook_id = next((w.id for w in api.webhooks(token) if w.url.rstrip("/") == url), None)
            action = "adopted"
        if webhook_id:
            try:
                api.update_webhook(token, webhook_id, spec)
            except SepayError as e:
                if e.kind == "not_found":      # deleted in SePay: make a new one
                    webhook_id = None
                elif e.kind == "invalid":      # can't be changed in place: replace it
                    old, webhook_id, action = webhook_id, api.create_webhook(token, spec), "replaced"
                    try:
                        api.delete_webhook(token, old)
                    except SepayError:
                        pass
                else:
                    raise
        if not webhook_id:
            webhook_id, action = api.create_webhook(token, spec), "created"
        done("webhook", action=action, name=name, url=url)
        team.sepay_webhook_id, team.sepay_key_hash = webhook_id, hash_secret(key)
        return {"webhook_id": webhook_id, "action": action, "created": action in ("created", "replaced"),
                "name": name, "url": url}

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

    def public_link(self, team_id: int) -> str | None:
        """The pay page link if a manager has made one (for /guide; anyone in the team may see it)."""
        with self.d.uow() as uow:
            team = get_team(uow, team_id)
            return f"{self.d.config.public_url}/p/{team.public_token}/" if team.public_token else None

    # ---- followers: who gets the team's announcements ---------------------------------------------

    def follow(self, team_id: int, tg_id: int) -> bool:
        """Someone opened the team bot. True the first time (they get announcements from now on)."""
        with self.d.uow() as uow:
            get_team(uow, team_id)
            if uow.followers.get(team_id, tg_id):
                return False
            uow.followers.add(Follower(team_id=team_id, tg_id=tg_id, created_at=self.d.clock.now()))
            try:
                uow.commit()
            except Conflict:  # two updates from the same person at once
                return False
            return True

    def set_muted(self, team_id: int, tg_id: int, muted: bool | None = None) -> dict:
        """Turn someone's announcements off or on (None: toggle). Also used when they block the bot."""
        with self.d.uow() as uow:
            f = uow.followers.get(team_id, tg_id)
            if f is None:
                f = Follower(team_id=team_id, tg_id=tg_id, created_at=self.d.clock.now())
                uow.followers.add(f)
            f.muted = (not f.muted) if muted is None else muted
            uow.commit()
            return {"muted": f.muted}

    def followers(self, team_id: int) -> list[int]:
        with self.d.uow() as uow:
            return uow.followers.active_ids(team_id)

    def bot_users(self, team_id: int, actor: int) -> dict:
        """Who has opened the team bot (and so gets reminders), for managers: name, role, muted, since."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            roles = {a.tg_id: a.role for a in uow.admins.of_team(team.id)}
            people = [{**user_info(uow, f.tg_id), "role": roles.get(f.tg_id), "muted": f.muted,
                       "since": self.d.clock.fmt(f.created_at)} for f in uow.followers.of_team(team.id)]
            return {"people": people, "on": sum(not p["muted"] for p in people),
                    "members": len(uow.members.of_team(team.id, active_only=True))}

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
                     "action": a.action, "target": a.target, "before": a.before, "after": a.after,
                     "undone": a.undone_at is not None}
                    for a in uow.audit.latest(team.id, min(limit, 100))]
