"""Events sent from the core to the bot gateway, which turns them into Telegram messages."""
import logging

import httpx

from ..config import settings

log = logging.getLogger("events")


def emit(event: dict) -> bool:
    try:
        r = httpx.post(
            f"{settings.gateway_url}/internal/events",
            json=event,
            headers={"X-Internal-Key": settings.internal_key},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # the gateway being down must never break a payment
        log.warning("could not deliver event %s: %s", event.get("type"), exc)
        return False
