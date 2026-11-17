"""Telegram users we have seen: names for display, and their chosen language."""
from sqlalchemy.orm import Session

from ..models import TgUser, utcnow
from .access import is_super
from .errors import invalid


def upsert_user(db: Session, tg_id: int, first_name: str | None, last_name: str | None,
                username: str | None) -> dict:
    user = db.get(TgUser, tg_id)
    if not user:
        user = TgUser(tg_id=tg_id)
        db.add(user)
    user.first_name, user.last_name, user.username = first_name, last_name, username
    user.updated_at = utcnow()
    db.commit()
    return {"tg_id": tg_id, "lang": user.lang, "is_super": is_super(tg_id)}


def get_lang(db: Session, tg_id: int) -> str | None:
    user = db.get(TgUser, tg_id)
    return user.lang if user else None


def set_lang(db: Session, tg_id: int, lang: str) -> None:
    if lang not in ("en", "vi"):
        raise invalid()
    user = db.get(TgUser, tg_id) or TgUser(tg_id=tg_id)
    user.lang = lang
    db.add(user)
    db.commit()
