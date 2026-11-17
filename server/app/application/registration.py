"""Use cases: registering teams through the master bot (request, approval, creation)."""
import logging
import secrets
import threading

from ..domain.entities import OWNER, Team, TeamAdmin, TeamRequest
from ..domain.errors import DomainError, invalid
from ..domain.rules import bot_usernames
from .access import is_super, require_super
from .people import audit, display_name, user_info
from .ports import BotFactoryError, Deps, UnitOfWork

log = logging.getLogger("registration")
_making: set[int] = set()  # people whose bot is being created right now: a double tap makes one bot
_making_lock = threading.Lock()


class RegistrationUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _request_dict(self, uow: UnitOfWork, r: TeamRequest) -> dict:
        return {"id": r.id, "team_name": r.team_name, "status": r.status, **user_info(uow, r.tg_id),
                "created_at": self.d.clock.fmt(r.created_at)}

    def bot_payload(self, t: Team) -> dict:
        """What the bot layer needs to run a team's bot."""
        return {"team_id": t.id, "name": t.name, "bot_id": t.bot_id, "bot_username": t.bot_username,
                "token": self.d.cipher.decrypt(t.bot_token_enc), "webhook_secret": t.webhook_secret, "lang": t.lang,
                "group_chat_id": t.group_chat_id}

    def request_team(self, actor: int, team_name: str) -> dict:
        name = (team_name or "").strip()[:100]
        if not name:
            raise invalid()
        with self.d.uow() as uow:
            if uow.team_requests.has_pending(actor):
                raise DomainError(409, "request_pending")
            r = TeamRequest(tg_id=actor, team_name=name, created_at=self.d.clock.now())
            uow.team_requests.add(r)
            uow.commit()
            return {**self._request_dict(uow, r), "admin_ids": sorted(self.d.config.master_admin_ids)}

    def pending_team_requests(self, actor: int) -> list[dict]:
        require_super(self.d.config, actor)
        with self.d.uow() as uow:
            return [self._request_dict(uow, r) for r in uow.team_requests.pending()]

    def decide_team_request(self, actor: int, request_id: int, approve: bool) -> dict:
        require_super(self.d.config, actor)
        with self.d.uow() as uow:
            r = uow.team_requests.get(request_id)
            if not r:
                raise DomainError(404, "not_found")
            if r.status != "pending":
                raise DomainError(409, "already_decided", status=r.status)
            r.status = "approved" if approve else "rejected"
            r.decided_by, r.decided_at = actor, self.d.clock.now()
            audit(uow, self.d.clock, None, actor, "team_request." + r.status, target=r.team_name)
            uow.commit()
            return self._request_dict(uow, r)

    def approved_request(self, actor: int) -> dict:
        with self.d.uow() as uow:
            r = uow.team_requests.latest_approved(actor)
            return {"request": self._request_dict(uow, r) if r else None, "is_super": is_super(self.d.config, actor)}

    def can_make_bot(self) -> bool:
        """Is the master bot able to create team bots itself (the admin's Telegram login is set up)?"""
        return self.d.bot_factory.available()

    def make_bot(self, actor: int, team_name: str | None = None) -> dict:
        """Create a team's bot through @BotFather: for the actor's approved team request, or, for a system
        admin, any team name. Returns its token and username; registering it is then the same as a pasted
        token (create_team, with the same team name)."""
        with self.d.uow() as uow:
            req = uow.team_requests.latest_approved(actor)
            if req:
                name = req.team_name
            elif is_super(self.d.config, actor) and (team_name or "").strip():
                name = team_name.strip()[:100]
            else:
                raise DomainError(403, "not_approved")
        with _making_lock:
            if actor in _making:
                raise DomainError(409, "botfather_busy")
            _making.add(actor)
        try:
            bot = self.d.bot_factory.create_bot(f"{name} · Team Fund"[:64], bot_usernames(name))
        except BotFactoryError as e:
            log.warning("bot factory failed for %s: %s", actor, e)
            raise DomainError(400 if e.kind != "unavailable" else 503, f"botfather_{e.kind}") from e
        finally:
            with _making_lock:
                _making.discard(actor)
        with self.d.uow() as uow:
            audit(uow, self.d.clock, None, actor, "bot.create", target=bot.username)
            uow.commit()
        return {"token": bot.token, "username": bot.username, "team_name": name}

    def create_team(self, actor: int, token: str, bot_id: int, bot_username: str, name: str | None) -> dict:
        with self.d.uow() as uow:
            req = uow.team_requests.latest_approved(actor)
            if not req and not is_super(self.d.config, actor):
                raise DomainError(403, "not_approved")
            if uow.teams.bot_exists(bot_id):
                raise DomainError(409, "bot_exists")
            now = self.d.clock.now()
            team_name = (req.team_name if req else None) or name or bot_username
            team = Team(name=team_name, bot_id=bot_id, bot_username=bot_username,
                        bot_token_enc=self.d.cipher.encrypt(token), webhook_secret=secrets.token_urlsafe(32),
                        owner_tg_id=actor, lang=self.d.config.default_lang, created_at=now)
            uow.teams.add(team)
            uow.flush()
            uow.admins.add(TeamAdmin(team_id=team.id, tg_id=actor, role=OWNER, added_by=actor, added_at=now))
            if req:
                req.status = "used"
            audit(uow, self.d.clock, team.id, actor, "team.create", target=team_name)
            uow.commit()
            return self.bot_payload(team)

    def my_teams(self, actor: int) -> list[dict]:
        with self.d.uow() as uow:
            return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username, "role": role}
                    for t, role in uow.teams.for_admin(actor)]

    def all_teams(self, actor: int) -> list[dict]:
        require_super(self.d.config, actor)
        with self.d.uow() as uow:
            counts = uow.members.count_by_team()
            return [{"team_id": t.id, "name": t.name, "bot_username": t.bot_username,
                     "owner": display_name(uow, t.owner_tg_id), "members": counts.get(t.id, 0)}
                    for t in uow.teams.active()]

    def active_bots(self) -> list[dict]:
        with self.d.uow() as uow:
            return [self.bot_payload(t) for t in uow.teams.active()]
