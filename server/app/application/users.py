"""Use cases: Telegram users we have seen (names for display, chosen language)."""
from ..domain.entities import User
from ..domain.errors import invalid
from ..domain.rules import LANGS
from .access import is_super
from .ports import Deps


class UserUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def upsert_user(self, tg_id: int, first_name: str | None, last_name: str | None, username: str | None) -> dict:
        with self.d.uow() as uow:
            user = uow.users.get(tg_id)
            if not user:
                user = User(tg_id=tg_id)
                uow.users.add(user)
            user.first_name, user.last_name, user.username = first_name, last_name, username
            user.updated_at = self.d.clock.now()
            uow.commit()
            return {"tg_id": tg_id, "lang": user.lang, "is_super": is_super(self.d.config, tg_id)}

    def get_lang(self, tg_id: int) -> str | None:
        with self.d.uow() as uow:
            user = uow.users.get(tg_id)
            return user.lang if user else None

    def set_lang(self, tg_id: int, lang: str) -> None:
        if lang not in LANGS:
            raise invalid()
        with self.d.uow() as uow:
            user = uow.users.get(tg_id)
            if not user:
                user = User(tg_id=tg_id, updated_at=self.d.clock.now())
                uow.users.add(user)
            user.lang = lang
            uow.commit()
