"""Composition root: builds the adapters, wires them into the use cases, and starts the controllers.

Layers (dependencies point inward only, checked by tests/test_architecture.py):

  domain/          entities and business rules            imports nothing from the app
  application/     use cases and the ports they need      imports domain
  infrastructure/  SQLAlchemy, Fernet, SePay QR, openpyxl  implements application ports
  interfaces/      Telegram bots, HTTP webhooks, scheduler calls the use cases

Run with one worker (the bots and the scheduler live in this process):
  uvicorn app.main:app --workers 1"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .application.ports import AppConfig, Deps
from .application.services import build_services
from .config import settings
from .domain.errors import DomainError
from .infrastructure.botfather import SESSION, TelethonBotFactory
from .infrastructure.clock import SystemClock
from .infrastructure.crypto import FernetCipher
from .infrastructure.db import SqlUnitOfWork, make_session_factory
from .infrastructure.secret_store import SecretStore
from .infrastructure.sepay import SepayQr, SepayUserApi
from .infrastructure.xlsx import XlsxWriter
from .interfaces import deps, scheduler
from .interfaces.bot import registry
from .interfaces.bot.notifier import TelegramNotifier
from .interfaces.http import sepay, telegram, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def create_app() -> FastAPI:
    session_factory = make_session_factory(settings.database_url)
    notifier = TelegramNotifier()
    cipher = FernetCipher(settings.token_enc_key)
    secret_store = SecretStore(session_factory, cipher)
    services = build_services(Deps(
        uow=lambda: SqlUnitOfWork(session_factory),
        clock=SystemClock(settings.timezone),
        cipher=cipher,
        qr=SepayQr(),
        spreadsheet=XlsxWriter(),
        notifier=notifier,
        config=AppConfig(public_url=settings.public_url, master_admin_ids=tuple(settings.master_admin_ids),
                         default_lang=settings.default_lang),
        sepay=SepayUserApi(settings.sepay_api_url),
        bot_factory=TelethonBotFactory(settings.telegram_api_id, settings.telegram_api_hash,
                                       lambda: secret_store.get(SESSION)),
    ))
    deps.install(services)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        notifier.bind(asyncio.get_running_loop())
        await registry.start()
        jobs = scheduler.start(services.jobs, settings.timezone) if settings.run_jobs else None
        yield
        if jobs:
            jobs.shutdown(wait=False)
        await registry.stop()

    app = FastAPI(title="Team Fund", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(DomainError)
    def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, **exc.extra}})

    @app.get("/health")
    def health():
        return {"ok": True}

    app.include_router(telegram.router)
    app.include_router(sepay.router)
    app.include_router(web.router)
    return app


app = create_app()
