"""Small encrypted key/value store for infrastructure secrets (the app_secrets table)."""
from datetime import datetime, timezone

from sqlalchemy import delete, select

from .crypto import FernetCipher
from .orm import app_secrets


class SecretStore:
    def __init__(self, session_factory, cipher: FernetCipher):
        self._sessions, self._cipher = session_factory, cipher

    def get(self, name: str) -> str | None:
        with self._sessions() as s:
            enc = s.execute(select(app_secrets.c.value_enc).where(app_secrets.c.name == name)).scalar()
        return self._cipher.decrypt(enc) if enc else None

    def set(self, name: str, value: str) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._sessions() as s:
            s.execute(delete(app_secrets).where(app_secrets.c.name == name))
            s.execute(app_secrets.insert().values(name=name, value_enc=self._cipher.encrypt(value), updated_at=now))
            s.commit()

    def delete(self, name: str) -> None:
        with self._sessions() as s:
            s.execute(delete(app_secrets).where(app_secrets.c.name == name))
            s.commit()
