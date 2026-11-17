"""One aiogram Bot per team plus the master bot, sharing two dispatchers, connected to Telegram."""
import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from . import team
from .commands import set_team_default_commands, sync_admin_commands
from .config import ALLOWED_UPDATES, cfg, make_bot
from .lang import lang_of
from .render import report_error

log = logging.getLogger("registry")


@dataclass
class Entry:
    bot: Bot
    team: dict            # team_id, name, bot_id, bot_username, webhook_secret, lang, group_chat_id
    username: str
    secret: str
    poller: asyncio.Task | None = field(default=None, repr=False)


by_bot: dict[int, Entry] = {}
by_team: dict[int, Entry] = {}


async def team_context(handler, update: Update, data: dict):
    """Give every team handler its `entry` and `lang`, and report any error to the user."""
    entry = by_bot.get(data["bot"].id)
    if entry is None:
        return None
    user, chat = data.get("event_from_user"), data.get("event_chat")
    fallback = entry.team["lang"]
    if chat is not None and chat.type == "private":
        lang = await lang_of(user, fallback)
    else:
        lang = fallback  # group messages use the team's language
        if user:
            asyncio.create_task(lang_of(user, fallback))  # keep names fresh
    data["entry"], data["lang"] = entry, lang
    try:
        return await handler(update, data)
    except Exception as e:
        await report_error(update, lang, e)


team_dp = Dispatcher()
team_dp.update.outer_middleware(team_context)
team_dp.include_router(team.router)


async def _poll(bot: Bot, dp: Dispatcher, name: str) -> None:
    await bot.delete_webhook()
    offset = None
    log.info("polling @%s", name)
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=ALLOWED_UPDATES)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("polling @%s: %s", name, e)
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u.update_id + 1
            try:
                await dp.feed_update(bot, u)
            except Exception:
                log.exception("update failed for @%s", name)


async def connect(bot: Bot, path: str, secret: str, dp: Dispatcher, name: str) -> asyncio.Task | None:
    """Webhook to PUBLIC_URL, or a polling task."""
    if cfg.mode == "webhook":
        await bot.set_webhook(f"{cfg.public_url}{path}", secret_token=secret, allowed_updates=ALLOWED_UPDATES)
        return None
    return asyncio.create_task(_poll(bot, dp, name))


async def register_team(t: dict) -> Entry:
    old = by_bot.get(int(t["bot_id"]))
    if old:
        if old.poller:
            old.poller.cancel()
        await old.bot.session.close()
    bot = make_bot(t["token"])
    me = await bot.get_me()
    entry = Entry(bot=bot, team={k: v for k, v in t.items() if k != "token"}, username=me.username,
                  secret=t["webhook_secret"])
    by_bot[me.id] = entry
    by_team[int(t["team_id"])] = entry
    entry.poller = await connect(bot, f"/tg/{me.id}", entry.secret, team_dp, me.username)
    try:
        await set_team_default_commands(bot)
        await sync_admin_commands(bot, t["team_id"])
    except Exception as e:
        log.warning("commands for @%s: %s", me.username, e)
    log.info("team %s ready: @%s", t["team_id"], me.username)
    return entry


async def close_all() -> None:
    for e in by_bot.values():
        if e.poller:
            e.poller.cancel()
        await e.bot.session.close()
