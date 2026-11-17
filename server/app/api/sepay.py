"""SePay webhook: SePay calls this when money arrives in a team's bank account."""
import asyncio
import logging

from fastapi import APIRouter, Body, Header

from ..bot import notify
from ..bot.run import call
from ..domain import money
from ..domain.errors import DomainError

router = APIRouter()
log = logging.getLogger("sepay")
_pending: set[asyncio.Task] = set()


@router.post("/sepay/{team_id}")
async def sepay_webhook(team_id: int, payload: dict = Body(...), authorization: str = Header("")):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "apikey":
        raise DomainError(401, "unauthorized")
    event = await call(money.record_sepay, team_id, key.strip(), payload)
    log.info("team %s tx %s -> %s", team_id, payload.get("id"), event["type"] if event else "skipped")
    if event:
        # answer SePay right away; the Telegram messages go out in the background
        task = asyncio.create_task(notify.deliver(event))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    return {"success": True}
