"""Telegram webhooks: one URL for the master bot and one per team bot."""
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..bot import registry
from ..bot.master import master_dp

router = APIRouter()
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@router.post("/tg/master")
async def master_webhook(request: Request):
    bot = registry.master["bot"]
    if bot is None:
        return Response(status_code=503)
    if not _same(request.headers.get(SECRET_HEADER, ""), registry.settings.master_secret):
        return Response(status_code=401)
    await registry.feed(master_dp, bot, await request.json())
    return {"ok": True}


@router.post("/tg/{bot_id}")
async def team_webhook(bot_id: int, request: Request):
    entry = registry.by_bot.get(bot_id)
    if entry is None:
        return Response(status_code=404)
    if not _same(request.headers.get(SECRET_HEADER, ""), entry.secret):
        return Response(status_code=401)
    await registry.feed(registry.team_dp, entry.bot, await request.json())
    return {"ok": True}
