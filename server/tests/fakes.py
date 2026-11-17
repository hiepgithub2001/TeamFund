"""Test doubles for the ports, plus a helper that wires real SQLite adapters with them."""
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from app.application.ports import AppConfig, Deps
from app.application.services import build_services
from app.infrastructure.clock import SystemClock
from app.infrastructure.crypto import FernetCipher
from app.infrastructure.db import SqlUnitOfWork, make_session_factory
from app.infrastructure.sepay import SepayQr
from app.infrastructure.xlsx import XlsxWriter

TZ = "Asia/Ho_Chi_Minh"


class FixedClock(SystemClock):
    """A clock that says whatever local time the test sets."""

    def __init__(self, local: datetime):
        super().__init__(TZ)
        self.local = local.replace(tzinfo=ZoneInfo(TZ))

    def local_now(self) -> datetime:
        return self.local

    def now(self) -> datetime:
        return self.local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def advance(self, **kw) -> None:
        self.local += timedelta(**kw)


class RecordingNotifier:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)

    def deliver(self, event: dict) -> bool:
        self.events.append(event)
        return True


def make_services(local_time: datetime, admin_ids=(1,)):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    factory = make_session_factory(f"sqlite:///{db.name}")
    clock, notifier = FixedClock(local_time), RecordingNotifier()
    services = build_services(Deps(
        uow=lambda: SqlUnitOfWork(factory), clock=clock, cipher=FernetCipher(Fernet.generate_key().decode()),
        qr=SepayQr(), spreadsheet=XlsxWriter(), notifier=notifier,
        config=AppConfig(public_url="https://example.test", master_admin_ids=tuple(admin_ids)),
    ))
    return services, clock, notifier
