"""Operations: what the system admin needs to see to know everything works, from the database side.
The host side (disk, backups, bot webhooks, error log) is read by the interfaces layer, which owns those."""
import hashlib
import time
from datetime import timedelta

from ..domain.rules import SEPAY_NEAR, sepay_cycle
from .access import require_super
from .ports import Deps, SepayError

UNMATCHED_STALE = timedelta(days=3)     # money nobody assigned for this long gets flagged
QUIET_WINDOW = timedelta(days=7)        # QR codes made but no bank transfer heard at all: is SePay reaching us?
QUIET_MIN_QR = 3


class OpsUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def db_ok(self) -> bool:
        """The database answers (for /health)."""
        try:
            with self.d.uow() as uow:
                uow.teams.active()
            return True
        except Exception:
            return False

    def teams(self, actor: int | None) -> list[dict]:
        """Per active team: unmatched money, the last bank transfer heard from SePay, and recent QR codes.
        actor=None is the scheduler (it reports only to the system admins)."""
        if actor is not None:
            require_super(self.d.config, actor)
        now, fmt = self.d.clock.now(), self.d.clock.fmt
        out = []
        with self.d.uow() as uow:
            for team in uow.teams.active():
                unmatched = uow.payments.unmatched(team.id)
                oldest = min((p.created_at for p in unmatched), default=None)
                last = uow.payments.last_transfer_at(team.id)
                recent = uow.intents.since(team.id, now - QUIET_WINDOW)
                made = len(recent)
                paid = sum(i.status == "paid" for i in recent)
                open_qr = sum(i.status == "pending" and i.expires_at and i.expires_at > now for i in recent)
                quiet = (team.has_bank() and made >= QUIET_MIN_QR and paid == 0
                         and (last is None or last < now - QUIET_WINDOW))
                out.append({"team_id": team.id, "name": team.name, "bot_id": team.bot_id,
                            "bot_username": team.bot_username, "has_bank": team.has_bank(),
                            "unmatched": len(unmatched), "unmatched_amount": sum(p.amount for p in unmatched),
                            "unmatched_oldest_days": (now - oldest).days if oldest else None,
                            "unmatched_stale": bool(oldest and now - oldest > UNMATCHED_STALE),
                            "last_transfer": fmt(last) if last else None,
                            "last_transfer_days": (now - last).days if last else None,
                            "qr_made_7d": made, "qr_paid_7d": paid, "qr_open": open_qr, "sepay_quiet": quiet})
        return out

    def sepay_usage(self, actor: int | None) -> list[dict]:
        """Each SePay account the teams use (teams sharing a token share one plan): transactions recorded this
        cycle against the plan's limit, when the owner told it (/sepayplan). SePay's API gives the count but
        not the plan, so without a limit we still show the count."""
        if actor is not None:
            require_super(self.d.config, actor)
        today = self.d.clock.local_now().date()
        groups: dict[str, dict] = {}
        with self.d.uow() as uow:
            for team in uow.teams.active():
                if not team.sepay_token_enc:
                    continue
                token = self.d.cipher.decrypt(team.sepay_token_enc)
                g = groups.setdefault(hashlib.sha256(token.encode()).hexdigest(), {"token": token, "teams": [], "plans": []})
                g["teams"].append(team.name)
                if team.sepay_quota:
                    g["plans"].append((team.sepay_quota, team.sepay_cycle_day))
        out = []
        for n, g in enumerate(groups.values()):
            quota, day = min(g["plans"]) if g["plans"] else (None, None)
            start, reset = sepay_cycle(today, day)
            row = {"teams": g["teams"], "quota": quota, "since": start.strftime("%d/%m"),
                   "resets": reset.strftime("%d/%m"), "used": None, "pct": None, "near": False, "error": None}
            if n:
                time.sleep(0.4)  # SePay allows 3 requests a second
            try:
                row["used"] = self.d.sepay.count_transactions(g["token"], start)
            except SepayError as e:
                row["error"] = str(e)[:120]
            if quota and row["used"] is not None:
                row["pct"] = round(row["used"] * 100 / quota)
                row["near"] = row["used"] >= quota * SEPAY_NEAR
            out.append(row)
        return out
