import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import Base, engine
from .errors import AppError
from .routers import master, members, money, sepay, team

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Team Fund Core", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.exception_handler(AppError)
def app_error(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, **exc.extra}})


@app.get("/health")
def health():
    return {"ok": True}


for r in (master.router, team.router, members.router, money.router, sepay.router):
    app.include_router(r)
