"""BotFactory port: creates bots by chatting with @BotFather from the system admin's own Telegram account.

Telegram has no "create bot" API; BotFather is the only way. Telethon logs in as the admin (a user session,
made once with `python -m app.telegram_login`, stored encrypted in app_secrets) and goes through
/newbot exactly as a person would: name, then usernames until one is free, then reads the token.
The bots belong to the admin's account (BotFather allows 20 per account)."""
import asyncio
import logging
import re
import threading
from typing import Callable

from ..application.ports import BotFactoryError, CreatedBot

log = logging.getLogger("botfather")
SESSION = "telegram_session"   # app_secrets name of the admin's login
BOTFATHER = "BotFather"
TOKEN_RE = re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{30,})\b")
TIMEOUT = 30


def _text(msg) -> str:
    return (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()


def _check(text: str) -> None:
    low = text.lower()
    if "too many attempts" in low:
        m = re.search(r"(\d+)\s*seconds?", low)
        raise BotFactoryError("flood", f"try again in {m.group(1)} s" if m else text[:120])
    if "20 bots" in low or "cannot do" in low:
        raise BotFactoryError("limit", text[:120])


async def converse(conv, name: str, usernames: list[str]) -> CreatedBot:
    """The /newbot dialogue. `conv` is a Telethon conversation (send_message / get_response)."""
    await conv.send_message("/cancel")  # leave whatever BotFather was waiting for
    await conv.get_response()
    await conv.send_message("/newbot")
    reply = _text(await conv.get_response())
    _check(reply)
    if "name" not in reply.lower():
        raise BotFactoryError("unavailable", f"unexpected reply to /newbot: {reply[:120]}")
    await conv.send_message(name)
    reply = _text(await conv.get_response())
    _check(reply)
    if "username" not in reply.lower():
        raise BotFactoryError("unavailable", f"name refused: {reply[:120]}")
    for username in usernames:
        await conv.send_message(username)
        reply = _text(await conv.get_response())
        m = TOKEN_RE.search(reply)
        if m:
            return CreatedBot(token=m.group(1), username=username)
        _check(reply)
        log.info("username %s not available: %s", username, reply[:80])
    await conv.send_message("/cancel")
    raise BotFactoryError("taken", ", ".join(usernames))


class TelethonBotFactory:
    def __init__(self, api_id: int, api_hash: str, load_session: Callable[[], str | None]):
        self.api_id, self.api_hash, self._load = api_id, api_hash, load_session
        self._lock = threading.Lock()  # one BotFather dialogue at a time, or the replies would interleave

    def available(self) -> bool:
        try:
            return bool(self.api_id and self.api_hash and self._load())
        except Exception:
            log.exception("could not read the Telegram session")
            return False

    def create_bot(self, name: str, usernames: list[str]) -> CreatedBot:
        session = self._load() if self.api_id and self.api_hash else None
        if not session:
            raise BotFactoryError("not_configured")
        with self._lock:
            return asyncio.run(self._create(session, name, usernames))  # runs in a worker thread, no loop there

    async def _create(self, session: str, name: str, usernames: list[str]) -> CreatedBot:
        from telethon import TelegramClient, errors
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(session), self.api_id, self.api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise BotFactoryError("not_configured", "the Telegram login expired")
            async with client.conversation(BOTFATHER, timeout=TIMEOUT, exclusive=True) as conv:
                return await converse(conv, name, usernames)
        except errors.FloodWaitError as e:
            raise BotFactoryError("flood", f"try again in {e.seconds} s") from e
        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            raise BotFactoryError("unavailable", str(e) or type(e).__name__) from e
        finally:
            await client.disconnect()
