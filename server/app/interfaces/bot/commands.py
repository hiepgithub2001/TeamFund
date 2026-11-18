"""Command menus. Telegram shows each person the menu for their scope, so managers see manager commands
and everyone else only sees the payment commands. The core still checks roles on every action."""
import logging

from aiogram import Bot
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat)

from ..deps import S
from .run import call
from .i18n import t

log = logging.getLogger("commands")

EVERYONE = ["start", "donate", "status", "fund", "history", "bank", "weblink", "matches", "contacts", "notify", "requestmanager", "lang", "guide", "help"]
MANAGER = ["start", "donate", "status", "fund", "history", "bank", "notify", "weblink", "members", "plans", "charge", "markpaid", "expense", "income", "matches", "contacts", "undo", "money", "remind", "followers", "unmatched", "export", "settings", "teamlang", "lang", "guide", "help"]
OWNER = MANAGER + ["sepaytoken", "setbank", "requests", "managers", "transferowner", "log"]
MASTER = ["start", "requestteam", "newteam", "myteams", "lang", "help"]
MASTER_ADMIN = MASTER + ["requests", "teams"]

LANGS = [("en", None), ("vi", "vi")]


async def _set_for(bot: Bot, names: list[str], scope) -> None:
    for lang, code in LANGS:
        await bot.set_my_commands([BotCommand(command=c, description=t(lang, f"cmd.{c}")) for c in names],
                                  scope=scope, language_code=code)


async def set_team_default_commands(bot: Bot) -> None:
    await _set_for(bot, EVERYONE, BotCommandScopeAllPrivateChats())
    for _, code in LANGS:  # team bots no longer work in groups: no group menu
        await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats(), language_code=code)


async def set_admin_commands(bot: Bot, chat_id: int, role: str) -> None:
    try:
        await _set_for(bot, OWNER if role == "owner" else MANAGER, BotCommandScopeChat(chat_id=chat_id))
    except Exception as e:
        # "chat not found" until the person has opened the bot; they get the menu on the next sync
        log.warning("commands for %s: %s", chat_id, e)


async def clear_admin_commands(bot: Bot, chat_id: int) -> None:
    for _, code in LANGS:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id), language_code=code)
        except Exception:
            pass


async def sync_admin_commands(bot: Bot, team_id: int) -> None:
    for a in await call(S.teams.list_admins, team_id):
        await set_admin_commands(bot, a["tg_id"], a["role"])


async def set_master_commands(bot: Bot, admin_ids: list[int]) -> None:
    await _set_for(bot, MASTER, BotCommandScopeAllPrivateChats())
    for admin_id in admin_ids:
        try:
            await _set_for(bot, MASTER_ADMIN, BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            log.warning("master admin commands for %s: %s", admin_id, e)
