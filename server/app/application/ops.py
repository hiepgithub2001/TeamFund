"""Operations: what the system admin needs to see to know everything works, from the database side.
The host side (disk, backups, bot webhooks, error log) is read by the interfaces layer, which owns those."""
from datetime import timedelta

from .access import require_super
from .ports import Deps

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
