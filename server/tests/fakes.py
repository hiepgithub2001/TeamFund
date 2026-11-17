"""Test doubles for the ports, plus a helper that wires real SQLite adapters with them."""
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from app.application.ports import (AppConfig, BotFactoryError, CreatedBot, Deps, SepayBankAccount, SepayError, SepayVirtualAccount,
                                   SepayWebhook)
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


class FakeBotFactory:
    """@BotFather: `taken` usernames are refused; `fail` makes every call raise that kind."""

    def __init__(self, ready: bool = True):
        self.ready, self.taken, self.fail, self.calls = ready, set(), None, []

    def available(self) -> bool:
        return self.ready

    def create_bot(self, name: str, usernames: list[str]) -> CreatedBot:
        self.calls.append((name, usernames))
        if not self.ready:
            raise BotFactoryError("not_configured")
        if self.fail:
            raise BotFactoryError(self.fail)
        for u in usernames:
            if u not in self.taken:
                self.taken.add(u)
                return CreatedBot(token=f"{700000 + len(self.calls)}:{'A' * 35}", username=u)
        raise BotFactoryError("taken")


class FakeSepay:
    """An in-memory SePay account: linked bank accounts, webhooks, and one valid API token."""
    TOKEN = "T" * 40

    def __init__(self):
        self.accounts = [SepayBankAccount("acc-1", "101003630278", "VietinBank", "ICB", "TRAN LE HIEP", True)]
        self.hooks: dict[str, dict] = {}
        self.vas: dict[str, list] = {}         # bank account xid -> [SepayVirtualAccount]
        self.calls: list[str] = []
        self.fail_update: str | None = None   # make update_webhook raise this kind once

    def _auth(self, token):
        if token != self.TOKEN:
            raise SepayError("unauthorized")

    def bank_accounts(self, token):
        self._auth(token); self.calls.append("accounts")
        return list(self.accounts)

    def virtual_accounts(self, token, bank_account_xid):
        self._auth(token); self.calls.append("vas")
        return list(self.vas.get(bank_account_xid, []))

    def webhooks(self, token):
        self._auth(token); self.calls.append("webhooks")
        return [SepayWebhook(i, h["url"]) for i, h in self.hooks.items()]

    def create_webhook(self, token, spec):
        self._auth(token); self.calls.append("create")
        i = f"wh-{len(self.hooks) + 1}-{len(self.calls)}"
        self.hooks[i] = {"url": spec.url, "bank": spec.bank_account_xid, "key": spec.api_key}
        return i

    def update_webhook(self, token, webhook_id, spec):
        self._auth(token); self.calls.append("update")
        if self.fail_update:
            kind, self.fail_update = self.fail_update, None
            raise SepayError(kind)
        if webhook_id not in self.hooks:
            raise SepayError("not_found")
        self.hooks[webhook_id] = {"url": spec.url, "bank": spec.bank_account_xid, "key": spec.api_key}

    def delete_webhook(self, token, webhook_id):
        self._auth(token); self.calls.append("delete")
        self.hooks.pop(webhook_id, None)


def make_services(local_time: datetime, admin_ids=(1,), sepay=None, bot_factory=None):
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    factory = make_session_factory(f"sqlite:///{db.name}")
    clock, notifier = FixedClock(local_time), RecordingNotifier()
    services = build_services(Deps(
        uow=lambda: SqlUnitOfWork(factory), clock=clock, cipher=FernetCipher(Fernet.generate_key().decode()),
        qr=SepayQr(), spreadsheet=XlsxWriter(), notifier=notifier,
        config=AppConfig(public_url="https://example.test", master_admin_ids=tuple(admin_ids)),
        sepay=sepay or FakeSepay(),
        bot_factory=bot_factory or FakeBotFactory(),
    ))
    return services, clock, notifier
