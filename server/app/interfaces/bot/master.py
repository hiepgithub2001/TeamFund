"""The master bot: team registration requests, approval by the system admin, and /newteam."""
import logging
import re

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, Update

from . import registry, tips
from ...config import settings
from ..deps import S
from .registry import make_bot
from .run import call
from .fmt import esc
from .i18n import t
from .lang import lang_for, lang_of, set_lang
from ...domain.errors import DomainError
from .render import edit, error_text, kb, report_error
from .state import del_state, get_state, set_state

log = logging.getLogger("master")
router = Router(name="master")
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def is_super(tg_id: int) -> bool:
    """Only decides which menus to show; the use cases enforce the rule."""
    return tg_id in settings.master_admin_ids


def input_key(chat_id) -> str:
    return f"master:{chat_id}"


def request_card(lang, r):
    text = t(lang, "m.request_card", team=esc(r["team_name"]), name=esc(r["name"]),
             username=f" (@{esc(r['username'])})" if r["username"] else "", id=r["tg_id"], at=r["created_at"])
    return text, kb([(t(lang, "btn.approve"), f"tr:a:{r['id']}"), (t(lang, "btn.reject"), f"tr:r:{r['id']}")])


async def master_context(handler, update: Update, data: dict):
    chat, user = data.get("event_chat"), data.get("event_from_user")
    if chat is not None and chat.type != "private":
        return None  # the master bot only works in private chats
    code = getattr(user, "language_code", None)
    lang = await lang_of(user, code if code in ("en", "vi") else settings.default_lang)
    data["lang"] = lang
    await tips.arm(update, lang, "mtip")
    try:
        return await handler(update, data)
    except Exception as e:
        tips.drop()
        await report_error(update, lang, e)


@router.message(Command("start", "help"))
async def cmd_start(message: Message, lang):
    text = t(lang, "m.welcome_auto" if await call(S.registration.can_make_bot) else "m.welcome")
    if is_super(message.from_user.id):
        text += t(lang, "m.admin")
    await message.answer(text)


@router.message(Command("requestteam"))
async def cmd_requestteam(message: Message, lang):
    set_state(input_key(message.chat.id), {"type": "team_name"}, 600)
    await message.answer(t(lang, "m.ask_team_name"))


def make_bot_button(lang):
    return kb([(t(lang, "btn.make_bot"), "nb:make")])


@router.message(Command("newteam"))
async def cmd_newteam(message: Message, lang):
    r = await call(S.registration.approved_request, message.from_user.id)
    if not r["request"] and not r["is_super"]:
        await message.answer(t(lang, "err.not_approved"))
        return
    can = await call(S.registration.can_make_bot)
    if r["request"] and can:
        set_state(input_key(message.chat.id), {"type": "token"}, 600)
        await message.answer(t(lang, "m.newteam_choice", team=esc(r["request"]["team_name"])),
                             reply_markup=make_bot_button(lang))
    elif can:  # a system admin without a request: any team name
        set_state(input_key(message.chat.id), {"type": "bot_team_name"}, 600)
        await message.answer(t(lang, "m.newteam_admin"))
    else:
        set_state(input_key(message.chat.id), {"type": "token"}, 600)
        await message.answer(t(lang, "m.ask_token") + (t(lang, "m.login_hint") if r["is_super"] else ""))


@router.callback_query(F.data == "nb:make")
async def on_make_bot(cq: CallbackQuery, lang):
    """Create the team's bot through @BotFather (from the admin's account) and register it, in one tap."""
    await cq.answer()
    del_state(input_key(cq.message.chat.id))
    await edit(cq, t(lang, "m.making_bot"))
    try:
        made = await call(S.registration.make_bot, cq.from_user.id)
    except DomainError as e:
        await edit(cq, error_text(lang, e) + "\n\n" + t(lang, "m.make_bot_fallback"))
        return
    try:
        text = await register_token(cq.bot, cq.from_user.id, lang, made["token"])
    except DomainError as e:
        text = error_text(lang, e)
    await edit(cq, text)


async def make_and_register(message: Message, lang: str, team_name: str) -> None:
    """A system admin typed a team name: create its bot through @BotFather and register the team."""
    wait = await message.answer(t(lang, "m.making_bot"))
    try:
        made = await call(S.registration.make_bot, message.from_user.id, team_name)
        text = await register_token(message.bot, message.from_user.id, lang, made["token"], made["team_name"])
    except DomainError as e:
        text = error_text(lang, e) + "\n\n" + t(lang, "m.make_bot_fallback")
    await message.bot.edit_message_text(text, chat_id=wait.chat.id, message_id=wait.message_id)


@router.callback_query(F.data.regexp(r"^tr:(a|r):(\d+)$").as_("m"))
async def on_decide(cq: CallbackQuery, m: re.Match, lang):
    r = await call(S.registration.decide_team_request, cq.from_user.id, int(m.group(2)), m.group(1) == "a")
    approved = r["status"] == "approved"
    await cq.answer()
    await edit(cq, t(lang, "m.approved_card" if approved else "m.rejected_card",
                     team=esc(r["team_name"]), name=esc(r["name"])))
    user_lang = await lang_for(r["tg_id"], settings.default_lang)
    try:
        auto = approved and await call(S.registration.can_make_bot)
        await cq.bot.send_message(r["tg_id"], t(user_lang, "m.approved_user_auto" if auto else
                                                "m.approved_user" if approved else "m.rejected_user",
                                                team=esc(r["team_name"])),
                                  reply_markup=make_bot_button(user_lang) if auto else None)
    except Exception as e:
        log.warning("requester notify failed: %s", e)


@router.message(Command("requests"))
async def cmd_requests(message: Message, lang):
    if not is_super(message.from_user.id):
        await message.answer(t(lang, "err.not_super"))
        return
    items = await call(S.registration.pending_team_requests, message.from_user.id)
    if not items:
        await message.answer(t(lang, "m.requests_none"))
        return
    for r in items:
        text, markup = request_card(lang, r)
        await message.answer(text, reply_markup=markup)


@router.message(Command("teams"))
async def cmd_teams(message: Message, lang):
    if not is_super(message.from_user.id):
        await message.answer(t(lang, "err.not_super"))
        return
    teams = await call(S.registration.all_teams, message.from_user.id)
    lines = [t(lang, "m.team_line", name=esc(x["name"]), bot=x["bot_username"], owner=esc(x["owner"]),
               members=x["members"]) for x in teams]
    await message.answer("\n".join([t(lang, "m.teams_title", n=len(teams)), *lines]))


@router.message(Command("ops"))
async def cmd_ops(message: Message, lang):
    """System admins: the whole system on one screen (server, backups, bots, SePay, money to check)."""
    if not is_super(message.from_user.id):
        await message.answer(t(lang, "err.not_super"))
        return
    from . import ops
    await message.answer(ops.report(lang, await ops.collect(message.from_user.id)))


@router.message(Command("myteams"))
async def cmd_myteams(message: Message, lang):
    teams = await call(S.registration.my_teams, message.from_user.id)
    if not teams:
        await message.answer(t(lang, "m.myteams_empty"))
        return
    lines = [f"• <b>{esc(x['name'])}</b>: @{x['bot_username']} ({t(lang, 'role.' + x['role'])})" for x in teams]
    await message.answer("\n".join([t(lang, "m.myteams_title"), *lines]))


@router.message(Command("lang"))
async def cmd_lang(message: Message, lang):
    await message.answer(t(lang, "lang.choose"), reply_markup=kb([("English", "l:en"), ("Tiếng Việt", "l:vi")]))


@router.callback_query(F.data.regexp(r"^l:(en|vi)$").as_("m"))
async def on_lang(cq: CallbackQuery, m: re.Match):
    await set_lang(cq.from_user.id, m.group(1))
    await cq.answer()
    await edit(cq, t(m.group(1), "lang.done"))


@router.message(F.text)
async def on_text(message: Message, lang):
    text = message.text.strip()
    # A pasted token is accepted even without a prompt, so it never sits in the chat.
    st = get_state(input_key(message.chat.id)) or ({"type": "token"} if TOKEN_RE.match(text) else None)
    if not st or text.startswith("/"):
        return
    del_state(input_key(message.chat.id))

    if st["type"] == "team_name":
        r = await call(S.registration.request_team, message.from_user.id, text[:100])
        await message.answer(t(lang, "m.request_sent", team=esc(r["team_name"])))
        for admin_id in r["admin_ids"]:
            card, markup = request_card(await lang_for(admin_id, settings.default_lang), r)
            try:
                await message.bot.send_message(admin_id, card, reply_markup=markup)
            except Exception as e:
                log.warning("admin %s notify failed: %s", admin_id, e)
        return

    if st["type"] == "bot_team_name" and not TOKEN_RE.match(text):
        await make_and_register(message, lang, text[:100])
        return

    # token
    try:
        await message.delete()
        await message.answer(t(lang, "m.token_deleted"))
    except Exception:
        pass
    if not TOKEN_RE.match(text):
        await message.answer(t(lang, "m.bad_token"))
        return
    await message.answer(await register_token(message.bot, message.from_user.id, lang, text))


async def register_token(bot, actor: int, lang: str, token: str, team_name: str | None = None) -> str:
    """Check a bot token with Telegram, register the team and start its bot. Returns the message to show."""
    probe = make_bot(token)
    try:
        me = await probe.get_me()
    except Exception:
        return t(lang, "m.token_rejected")
    finally:
        await probe.session.close()
    created = await call(S.registration.create_team, actor, token, me.id, me.username, team_name or me.first_name)
    await registry.register_team(created)
    return t(lang, "m.team_created", team=esc(created["name"]), bot=created["bot_username"])


@router.callback_query()
async def on_stale_button(cq: CallbackQuery):
    await cq.answer()


master_dp = Dispatcher()
master_dp.update.outer_middleware(master_context)
master_dp.include_router(router)
