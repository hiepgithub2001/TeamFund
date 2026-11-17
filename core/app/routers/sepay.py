"""Public SePay webhook. Reached from the internet through Caddy at /sepay/{team_id}."""
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import AppError
from ..models import Team
from ..services.billing import apply_sepay
from ..services.crypto import hash_key
from ..services.events import emit

router = APIRouter()
log = logging.getLogger("sepay")


@router.post("/sepay/{team_id}")
def sepay_webhook(team_id: int, background: BackgroundTasks, payload: dict = Body(...),
                  authorization: str = Header(""), db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    scheme, _, key = authorization.partition(" ")
    if (not team or not team.active or not team.sepay_key_hash or scheme.lower() != "apikey"
            or not hmac.compare_digest(hash_key(key.strip()), team.sepay_key_hash)):
        raise AppError(401, "unauthorized")
    event = apply_sepay(db, team, payload)
    log.info("team %s tx %s -> %s", team_id, payload.get("id"), event["type"] if event else "skipped")
    if event:
        background.add_task(emit, event)
    return {"success": True}
