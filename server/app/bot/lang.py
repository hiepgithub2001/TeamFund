import time

from aiogram.types import User

from ..domain import users
from .run import call

_cache: dict[int, str | None] = {}  # tg_id -> 'en' | 'vi' | None (no explicit choice)
_seen: dict[int, float] = {}        # tg_id -> last upsert, so names stay fresh without a call per message
UPSERT_EVERY = 6 * 60 * 60


async def lang_of(user: User | None, fallback: str) -> str:
    """Language for someone who just sent us an update. Also keeps their name in the core up to date."""
    if user is None:
        return fallback
    if time.monotonic() - _seen.get(user.id, -UPSERT_EVERY) >= UPSERT_EVERY:
        try:
            u = await call(users.upsert_user, user.id, user.first_name, user.last_name, user.username)
            _cache[user.id] = u["lang"]
            _seen[user.id] = time.monotonic()
        except Exception:
            pass
    return _cache.get(user.id) or fallback


async def lang_for(tg_id: int, fallback: str) -> str:
    """Language for someone we message first (an owner, a requester)."""
    if tg_id in _cache:
        return _cache[tg_id] or fallback
    try:
        lang = await call(users.get_lang, tg_id)
        _cache[tg_id] = lang
        return lang or fallback
    except Exception:
        return fallback


async def set_lang(tg_id: int, lang: str) -> None:
    await call(users.set_lang, tg_id, lang)
    _cache[tg_id] = lang
