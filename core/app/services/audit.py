from sqlalchemy.orm import Session

from ..models import AuditLog, TgUser


def audit(db: Session, team_id, actor, action, target=None, before=None, after=None):
    db.add(AuditLog(team_id=team_id, actor_tg_id=actor, action=action,
                    target=target, before=before, after=after))


def display_name(db: Session, tg_id: int | None) -> str:
    if tg_id is None:
        return "—"
    u = db.get(TgUser, tg_id)
    if not u:
        return str(tg_id)
    full = " ".join(x for x in (u.first_name, u.last_name) if x)
    return full or (f"@{u.username}" if u.username else str(tg_id))


def user_info(db: Session, tg_id: int) -> dict:
    u = db.get(TgUser, tg_id)
    return {"tg_id": tg_id, "name": display_name(db, tg_id), "username": u.username if u else None}
