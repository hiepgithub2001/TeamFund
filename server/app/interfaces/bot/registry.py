"""Runs one aiogram Bot per team plus the master bot, and connects them to Telegram."""
import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import Update

from ...config import settings
from ..deps import S
from . import matches, team
from .commands import set_master_commands, set_team_default_commands, sync_admin_commands
from .lang import lang_of
from .render import report_error
from .run import call

log = logging.getLogger("bots")
ALLOWED_UPDATES = ["message", "callback_query"]


def make_bot(token: str) -> Bot:
    api = TelegramAPIServer.from_base(settings.telegram_api_root) if settings.telegram_api_root else None
    session = AiohttpSession(api=api) if api else AiohttpSession()
    return Bot(token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


@dataclass
class Entry:
    bot: Bot
    team: dict            # team_id, name, bot_id, bot_username, webhook_secret, lang
    username: str
    secret: str
    poller: asyncio.Task | None = field(default=None, repr=False)


by_bot: dict[int, Entry] = {}
_followed: set[tuple[int, int]] = set()  # (team, person) already recorded as a follower since startup
by_team: dict[int, Entry] = {}
master: dict = {"bot": None, "poller": None}


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
    if user and chat is not None and chat.type == "private":
        key = (entry.team["team_id"], user.id)
        if key not in _followed:
            data["new_follower"] = await call(S.teams.follow, *key)
            _followed.add(key)
        team.acting_user.set(user.id)
    try:
        return await handler(update, data)
    except Exception as e:
        await report_error(update, lang, e)


team_dp = Dispatcher()
team_dp.update.outer_middleware(team_context)
team_dp.include_router(matches.router)  # before team's catch-all text and button handlers
team_dp.include_router(team.router)


async def feed(dp: Dispatcher, bot: Bot, data: dict) -> None:
    try:
        await dp.feed_update(bot, Update.model_validate(data, context={"bot": bot}))
    except Exception:
        log.exception("update failed")


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


async def _connect(bot: Bot, path: str, secret: str, dp: Dispatcher, name: str) -> asyncio.Task | None:
    """Webhook to PUBLIC_URL, or a polling task."""
    if settings.telegram_mode == "webhook":
        await bot.set_webhook(f"{settings.public_url}{path}", secret_token=secret, allowed_updates=ALLOWED_UPDATES)
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
    entry.poller = await _connect(bot, f"/tg/{me.id}", entry.secret, team_dp, me.username)
    try:
        await set_team_default_commands(bot)
        await sync_admin_commands(bot, t["team_id"])
    except Exception as e:
        log.warning("commands for @%s: %s", me.username, e)
    log.info("team %s ready: @%s", t["team_id"], me.username)
    return entry


async def start() -> None:
    """Connect every team bot and the master bot. A bot that fails is logged and skipped."""
    if settings.telegram_mode == "webhook" and not settings.public_url.startswith("https://"):
        raise RuntimeError("PUBLIC_URL must be an https:// URL in webhook mode (or set TELEGRAM_MODE=polling)")
    for t in await call(S.registration.active_bots):
        try:
            await register_team(t)
        except Exception as e:
            log.error("could not start bot for team %s (@%s): %s", t["team_id"], t["bot_username"], e)
    if not settings.master_token:
        log.warning("MASTER_BOT_TOKEN is not set; the master bot is off")
        return
    from .master import master_dp  # imported here: master registers new teams through this module
    try:
        bot = make_bot(settings.master_token)
        me = await bot.get_me()
        master["poller"] = await _connect(bot, "/tg/master", settings.master_secret, master_dp, me.username)
        master["bot"] = bot
        await set_master_commands(bot, settings.master_admin_ids)
        log.info("master bot ready: @%s (%s)", me.username, settings.telegram_mode)
    except Exception as e:
        log.error("could not start the master bot: %s", e)


async def stop() -> None:
    for e in list(by_bot.values()):
        if e.poller:
            e.poller.cancel()
        await e.bot.session.close()
    if master["poller"]:
        master["poller"].cancel()
    if master["bot"]:
        await master["bot"].session.close()
