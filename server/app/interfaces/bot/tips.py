"""A short 💡 tip under the reply to a bare command: the shortcut or the next step people don't know about.

Tips live in the locales as `tip.<command>` (team bots) and `mtip.<command>` (master bot); a command without
one gets none, and `tip.<command>.manager` replaces it for managers. A command typed with arguments gets no
tip: whoever typed them already knows the form.

How it attaches: the middleware arms the tip for the update, and the request middleware below appends it to
the first message the bot then sends to that chat. So no handler needs to know about tips. A refusal or an
error drops it (`drop()`), so "managers only" never comes with a how-to."""
import contextvars
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import SendDocument, SendMessage, SendPhoto
from aiogram.types import Update

from ...config import settings
from .i18n import has, t

_pending: contextvars.ContextVar[dict | None] = contextvars.ContextVar("tip", default=None)
LIMITS = {SendMessage: ("text", 4096), SendPhoto: ("caption", 1024), SendDocument: ("caption", 1024)}


def command_of(update: Update) -> tuple[str | None, str]:
    """('history', '') for '/history', ('history', '10/2026') for '/history@fc37it_bot 10/2026'."""
    msg = update.message
    text = (msg.text or msg.caption or "") if msg else ""
    if not text.startswith("/"):
        return None, ""
    parts = text.split(maxsplit=1)
    return parts[0][1:].split("@", 1)[0].lower() or None, parts[1].strip() if len(parts) > 1 else ""


async def arm(update: Update, lang: str, prefix: str = "tip", manager=None, private: bool = True) -> None:
    """Set (or clear) the tip for this update. `manager`: an async callable, asked only when a manager tip exists."""
    _pending.set(None)
    cmd, args = command_of(update)
    if not cmd or args or not has(f"{prefix}.{cmd}") or (cmd == "start" and not private):
        return  # /start in a group only shows a Pay button
    key = f"{prefix}.{cmd}"
    if manager is not None and has(f"{key}.manager") and await manager():
        key += ".manager"
    month = datetime.now(ZoneInfo(settings.timezone)).strftime("%m/%Y")
    _pending.set({"chat": update.message.chat.id, "text": t(lang, key, month=month)})


def drop() -> None:
    p = _pending.get()
    if p:
        p["text"] = None


class TipRequests(BaseRequestMiddleware):
    """Appends the armed tip to the first message (or photo / file caption) sent to the command's chat."""

    async def __call__(self, make_request, bot, method):
        p = _pending.get()
        if p and p["text"] and type(method) in LIMITS and method.chat_id == p["chat"] \
                and (isinstance(method.parse_mode, Default) or method.parse_mode == "HTML"):
            field, limit = LIMITS[type(method)]
            body = getattr(method, field) or ""
            if len(body) + len(p["text"]) + 2 <= limit and not getattr(method, "entities" if field == "text" else "caption_entities", None):
                setattr(method, field, f"{body}\n\n{p['text']}" if body else p["text"])
                p["text"] = None
        return await make_request(bot, method)
