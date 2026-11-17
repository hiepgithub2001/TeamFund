"""SePay webhook controller: SePay calls this when money arrives in a team's bank account."""
import logging

from fastapi import APIRouter, Body, Header

from ...domain.entities import IncomingTransfer
from ...domain.errors import DomainError
from ..bot.run import call
from ..deps import S

router = APIRouter()
log = logging.getLogger("sepay")


def parse_webhook(payload: dict) -> IncomingTransfer:
    """SePay's JSON -> IncomingTransfer. SePay sends id, transferType ("in"/"out"), transferAmount, content,
    and optionally code and description; the payment code may be in any of the text fields."""
    content = " ".join(str(payload.get(k) or "") for k in ("code", "content", "description")).strip()
    return IncomingTransfer(tx_id=str(payload.get("id")), amount=int(float(payload.get("transferAmount") or 0)),
                            content=content, incoming=str(payload.get("transferType", "in")).lower() == "in")


@router.post("/sepay/{team_id}")
async def sepay_webhook(team_id: int, payload: dict = Body(...), authorization: str = Header("")):
    scheme, _, key = authorization.partition(" ")
    if scheme.lower() != "apikey":
        raise DomainError(401, "unauthorized")
    result = await call(S.money.record_transfer, team_id, key.strip(), parse_webhook(payload))
    log.info("team %s tx %s -> %s", team_id, payload.get("id"), result or "skipped")
    return {"success": True}
