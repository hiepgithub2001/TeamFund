"""Team Fund server: one process with three layers.

  domain/  business rules and use cases (no HTTP, no Telegram)
  bot/     Telegram controllers: the master bot and every team bot
  api/     HTTP controllers: Telegram and SePay webhooks
  jobs.py  scheduled jobs (monthly bills, reminders, report)

Run with a single worker: uvicorn app.main:app --workers 1"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import jobs
from .api import sepay, telegram
from .bot import registry
from .config import settings
from .db import Base, engine
from .domain.errors import DomainError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    await registry.start()
    scheduler = jobs.start() if settings.run_jobs else None
    yield
    if scheduler:
        scheduler.shutdown(wait=False)
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
