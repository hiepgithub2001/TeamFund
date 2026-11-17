"""Handlers shared by every team bot. The registry's middleware passes `entry` (bot + team) and `lang`."""
import logging
import re
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, ForceReply, Message

from ..deps import S
from .commands import clear_admin_commands, set_admin_commands
from .fmt import amount_and_title, esc, money, parse_amount, strip_tags
from .i18n import t
from .lang import lang_for, set_lang
from ...domain.errors import DomainError
from .render import (bill_label, edit, error_text, is_private, kb, owes_lines, pay_keyboard, say,
                     status_text)
from .run import call
from .state import del_state, get_state, set_state

router = Router(name="team")
log = logging.getLogger("team")
FORCE = ForceReply(force_reply=True)


# ---- helpers -----------------------------------------------------------------------------------

def tid(entry) -> int:
    return entry.team["team_id"]


def group_lang(entry) -> str:
    return entry.team["lang"]


def pick_key(bot_id, chat_id, msg_id) -> str:
    return f"k:{bot_id}:{chat_id}:{msg_id}"


def input_key(bot_id, chat_id) -> str:
    return f"in:{bot_id}:{chat_id}"


async def role(entry, user) -> str | None:
    return (await call(S.teams.role, tid(entry), user.id if user else None))["role"]


async def allow(ev, entry, lang, level: str, private_only: bool = True) -> bool:
    """True if the sender has the role; otherwise tells them and returns False."""
    if private_only and not is_private(ev):
        await say(ev, t(lang, "err.private_only"))
        return False
    r = await role(entry, ev.from_user)
    ok = r == "owner" if level == "owner" else r in ("owner", "manager")
    if not ok:
        text = t(lang, "err.not_owner" if level == "owner" else "err.not_manager")
        if isinstance(ev, CallbackQuery):
            await ev.answer(strip_tags(text), show_alert=True)
        else:
            await say(ev, text)
    return ok


async def post_to_group(entry, text: str, photo: str | None = None, reply_markup=None) -> None:
    chat_id = entry.team.get("group_chat_id")
    if not chat_id:
        return
    try:
        if photo:
            await entry.bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup)
        else:
            await entry.bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.warning("group post failed for team %s: %s", entry.team["team_id"], e)


# ---- multi-select picker (pay, charge, split, cash, assign) ------------------------------------------

def picker_keyboard(lang, st):
    per_row = 1 if st["kind"] in ("pay", "markpaid", "assign") else 2
    buttons = [(f"{'✅' if o['id'] in st['selected'] else '☐'} {o['label']}", f"k:t:{i}")
               for i, o in enumerate(st["options"])]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    n = len(st["selected"])
    if st["kind"] == "pay":
        total = sum(o["amount"] for o in st["options"] if o["id"] in st["selected"])
        rows += [[(t(lang, "pay.button", count=n, amount=money(total, lang)), "k:ok")], [("🔄", "k:r")]]
    else:
        rows += [[(t(lang, "btn.all"), "k:a"), (t(lang, "btn.confirm", n=n), "k:ok")],
                 [(t(lang, "btn.cancel"), "k:x")]]
    return kb(*rows)


async def open_picker(ev, lang, st) -> None:
    msg = await say(ev, st["title"], reply_markup=picker_keyboard(lang, st))
    set_state(pick_key(ev.bot.id, msg.chat.id, msg.message_id), st)


def unpaid_options(lang, unpaid):
    return [{"id": u["member_id"], "label": f"{u['name']} · {money(u['total'], lang)}", "amount": u["total"],
             "name": u["name"]} for u in unpaid]


async def open_pay_menu(ev, entry, lang) -> None:
    unpaid = await call(S.money.unpaid, tid(entry))
    if not unpaid:
        await say(ev, t(lang, "pay.none"))
        return
    await open_picker(ev, lang, {"kind": "pay", "title": t(lang, "pay.title", team=esc(entry.team["name"])),
                                 "options": unpaid_options(lang, unpaid), "selected": []})


async def on_picker_confirm(cq: CallbackQuery, entry, lang, st, key) -> None:
    ids = st["selected"]
    if not ids:
        await cq.answer(t(lang, "pay.select_first"))
        return
    G = group_lang(entry)

    if st["kind"] == "pay":
        intent = await call(S.money.create_intent, tid(entry), cq.from_user.id, ids)
        del_state(key)
        await cq.answer()
        try:
            await cq.bot.edit_message_reply_markup(chat_id=cq.message.chat.id, message_id=cq.message.message_id,
                                                   reply_markup=None)
        except Exception:
            pass
        caption = t(lang, "pay.qr_caption", members=esc(", ".join(intent["members"])),
                    amount=money(intent["amount"], lang), bank=esc(intent["bank_name"]),
                    account=esc(intent["bank_account"]), holder=esc(intent["bank_holder"] or ""),
                    code=esc(intent["content"]), expires=intent["expires_at"])
        try:
            await cq.bot.send_photo(cq.message.chat.id, intent["qr_url"], caption=caption)
        except Exception:
            await say(cq, f"{caption}\n\n{t(lang, 'pay.qr_failed')}")
        return

    if not await allow(cq, entry, lang, "manager"):
        return
    if st["kind"] in ("charge", "split"):
        r = await call(S.money.charges, tid(entry), cq.from_user.id, st["data"]["title"], st["data"]["amount"],
                       st["kind"], ids)
        del_state(key)
        await cq.answer()
        await edit(cq, t(lang, "charge.done", count=r["count"], title=esc(r["title"]), each=money(r["each"], lang)))
        await post_to_group(entry, t(G, "group.charge", title=esc(r["title"]), each=money(r["each"], G),
                                     count=r["count"]), reply_markup=pay_keyboard(G, entry.username))
    elif st["kind"] == "markpaid":
        r = await call(S.money.markpaid, tid(entry), cq.from_user.id, ids)
        del_state(key)
        await cq.answer()
        members = esc(", ".join(r["members"]))
        await edit(cq, t(lang, "markpaid.done", members=members, amount=money(r["amount"], lang)))
        await post_to_group(entry, t(G, "group.paid", members=members, amount=money(r["amount"], G)))
    elif st["kind"] == "assign":
        r = await call(S.money.assign, tid(entry), cq.from_user.id, st["data"]["payment_id"], ids)
        del_state(key)
        await cq.answer()
        members = esc(", ".join(r["members"]))
        await edit(cq, t(lang, "assign.done", members=members))
        await post_to_group(entry, t(G, "group.paid", members=members, amount=money(r["amount"], G)))


@router.callback_query(F.data.regexp(r"^k:(t|a|ok|x|r)(?::(\d+))?$").as_("m"))
async def on_picker(cq: CallbackQuery, m: re.Match, entry, lang):
    action, arg = m.group(1), m.group(2)
    key = pick_key(cq.bot.id, cq.message.chat.id, cq.message.message_id)
    st = get_state(key)
    if not st:
        await cq.answer(t(lang, "err.menu_expired"), show_alert=True)
        return
    if action == "t":
        idx = int(arg)
        if idx < len(st["options"]):
            oid = st["options"][idx]["id"]
            st["selected"] = [x for x in st["selected"] if x != oid] if oid in st["selected"] else st["selected"] + [oid]
    elif action == "a":
        st["selected"] = [] if len(st["selected"]) == len(st["options"]) else [o["id"] for o in st["options"]]
    elif action == "r":
        unpaid = await call(S.money.unpaid, tid(entry))
        if not unpaid:
            del_state(key)
            await cq.answer()
            await edit(cq, t(lang, "pay.none"))
            return
        st["options"] = unpaid_options(lang, unpaid)
        st["selected"] = [i for i in st["selected"] if any(o["id"] == i for o in st["options"])]
    elif action == "x":
        del_state(key)
        await cq.answer()
        await edit(cq, t(lang, "cancelled"))
        return
    elif action == "ok":
        await on_picker_confirm(cq, entry, lang, st, key)
        return
    set_state(key, st)
    await cq.answer()
    await edit(cq, st["title"], picker_keyboard(lang, st))


# ---- members ----------------------------------------------------------------------------------

async def members_list(ev, entry, lang, edit_in_place: bool = False) -> None:
    members = await call(S.members.list_members, tid(entry), ev.from_user.id)
    if not members:
        text, markup = t(lang, "members.empty"), None
    else:
        buttons = [(f"{'⏸ ' if m['status'] == 'paused' else ''}{m['name']}"
                    f"{' #' + m['shirt_number'] if m['shirt_number'] else ''}", f"m:v:{m['id']}") for m in members]
        markup = kb(*[buttons[i:i + 2] for i in range(0, len(buttons), 2)])
        text = t(lang, "members.title", n=len(members))
    if edit_in_place:
        await edit(ev, text, markup)
    else:
        await say(ev, text, reply_markup=markup)


def member_view(lang, m):
    dash = lambda v: esc(v) if v else "—"  # noqa: E731
    text = t(lang, "member.detail", name=esc(m["name"]), nickname=dash(m["nickname"]), shirt=dash(m["shirt_number"]),
             phone=dash(m["phone"]), note=dash(m["note"]), fee=money(m["fee"], lang),
             custom=t(lang, "member.custom") if m["fee_override"] is not None else "",
             status=t(lang, f"status.{m['status']}"), owes=money(m["owes"], lang))
    i = m["id"]
    paused = m["status"] == "paused"
    markup = kb(
        [(t(lang, "btn.name"), f"m:e:{i}:name"), (t(lang, "btn.nickname"), f"m:e:{i}:nickname")],
        [(t(lang, "btn.shirt_number"), f"m:e:{i}:shirt_number"), (t(lang, "btn.phone"), f"m:e:{i}:phone")],
        [(t(lang, "btn.note"), f"m:e:{i}:note"), (t(lang, "btn.fee"), f"m:e:{i}:fee")],
        [(t(lang, "btn.activate" if paused else "btn.pause"), f"m:s:{i}:{'active' if paused else 'paused'}"),
         (t(lang, "btn.bills"), f"m:b:{i}")],
        [(t(lang, "btn.remove"), f"m:rm:{i}"), (t(lang, "btn.back"), "m:l")],
    )
    return text, markup


async def bills_view(cq, entry, lang, member_id) -> None:
    r = await call(S.members.member_bills, tid(entry), cq.from_user.id, int(member_id))
    rows = [[(f"✖️ {bill_label(lang, b)} · {money(b['amount'], lang)}", f"b:w:{b['id']}:{member_id}")]
            for b in r["bills"]]
    rows.append([(t(lang, "btn.back"), f"m:v:{member_id}")])
    key = "bills.title" if r["bills"] else "bills.none"
    await edit(cq, t(lang, key, name=esc(r["member"]["name"])), kb(*rows))


@router.callback_query(F.data.regexp(r"^m:(l|v|e|s|b|rm|rmy)(?::(\d+))?(?::(\w+))?$").as_("m"))
async def on_member(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    action, mid, arg = m.group(1), m.group(2), m.group(3)
    user = cq.from_user
    if action == "l":
        await cq.answer()
        await members_list(cq, entry, lang, edit_in_place=True)
    elif action == "v":
        text, markup = member_view(lang, await call(S.members.get_member, tid(entry), user.id, int(mid)))
        await cq.answer()
        await edit(cq, text, markup)
    elif action == "e":
        mem = await call(S.members.get_member, tid(entry), user.id, int(mid))
        set_state(input_key(cq.bot.id, cq.message.chat.id), {"type": "edit", "member_id": int(mid), "field": arg}, 600)
        await cq.answer()
        prompt = (t(lang, "member.ask_fee", name=esc(mem["name"])) if arg == "fee"
                  else t(lang, "member.ask", field=t(lang, f"field.{arg}"), name=esc(mem["name"])))
        await say(cq, prompt, reply_markup=FORCE)
    elif action == "s":
        mem = await call(S.members.update_member, tid(entry), user.id, int(mid), {"status": arg})
        text, markup = member_view(lang, mem)
        await cq.answer(t(lang, "member.updated"))
        await edit(cq, text, markup)
    elif action == "b":
        await cq.answer()
        await bills_view(cq, entry, lang, mid)
    elif action == "rm":
        mem = await call(S.members.get_member, tid(entry), user.id, int(mid))
        await cq.answer()
        await edit(cq, t(lang, "member.confirm_remove", name=esc(mem["name"])),
                   kb([(t(lang, "btn.yes_remove"), f"m:rmy:{mid}"), (t(lang, "btn.cancel"), f"m:v:{mid}")]))
    elif action == "rmy":
        mem = await call(S.members.remove_member, tid(entry), user.id, int(mid))
        await cq.answer()
        await edit(cq, t(lang, "member.removed", name=esc(mem["name"])), kb([(t(lang, "btn.back"), "m:l")]))


@router.callback_query(F.data.regexp(r"^b:w:(\d+):(\d+)$").as_("m"))
async def on_waive(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    await call(S.money.waive, tid(entry), cq.from_user.id, int(m.group(1)))
    await cq.answer(t(lang, "bills.waived"))
    await bills_view(cq, entry, lang, m.group(2))


def parse_member_lines(text: str) -> list[dict]:
    out = []
    for line in text.split("\n"):
        parts = [p.strip() for p in line.split("|")]
        if parts[0]:
            out.append({"name": parts[0][:100],
                        "shirt_number": (parts[1] if len(parts) > 1 else "") or None,
                        "phone": (parts[2] if len(parts) > 2 else "") or None})
    return out


async def add_members(ev, entry, lang, text: str) -> None:
    members = parse_member_lines(text)
    if not members:
        await say(ev, t(lang, "add.prompt"))
        return
    created = await call(S.members.add_members, tid(entry), ev.from_user.id, members)
    await say(ev, t(lang, "add.done", n=len(created), names=esc(", ".join(m["name"] for m in created))))


# ---- manager requests ----------------------------------------------------------------------------

def request_card(lang, r):
    text = t(lang, "mr.card", name=esc(r["name"]), username=f" (@{esc(r['username'])})" if r["username"] else "",
             id=r["tg_id"], at=r["created_at"])
    return text, kb([(t(lang, "btn.approve"), f"mr:a:{r['id']}"), (t(lang, "btn.reject"), f"mr:r:{r['id']}")])


@router.message(Command("requestmanager"))
async def cmd_request_manager(message: Message, entry, lang):
    if not is_private(message):
        await message.answer(t(lang, "err.private_only"))
        return
    r = await call(S.teams.request_manager, tid(entry), message.from_user.id)
    await message.answer(t(lang, "mr.sent"))
    text, markup = request_card(await lang_for(r["owner_tg_id"], group_lang(entry)), r)
    try:
        await message.bot.send_message(r["owner_tg_id"], text, reply_markup=markup)
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^mr:(a|r):(\d+)$").as_("m"))
async def on_decide_manager(cq: CallbackQuery, m: re.Match, entry, lang):
    r = await call(S.teams.decide_manager_request, tid(entry), cq.from_user.id, int(m.group(2)), m.group(1) == "a")
    approved = r["status"] == "approved"
    await cq.answer()
    await edit(cq, t(lang, "mr.approved_card" if approved else "mr.rejected_card", name=esc(r["name"])))
    user_lang = await lang_for(r["tg_id"], group_lang(entry))
    try:
        await cq.bot.send_message(r["tg_id"], t(user_lang, "mr.approved_user" if approved else "mr.rejected_user",
                                                team=esc(entry.team["name"])))
    except Exception:
        pass
    if approved:
        await set_admin_commands(cq.bot, r["tg_id"], "manager")
        await post_to_group(entry, t(group_lang(entry), "group.new_manager", name=esc(r["name"])))


# ---- owner tools -------------------------------------------------------------------------------

async def managers_view(ev, entry, lang, edit_in_place: bool = False) -> None:
    admins = await call(S.teams.list_admins, tid(entry))
    lines = [t(lang, "managers.title")]
    rows = []
    for a in admins:
        lines.append(t(lang, "managers.owner" if a["role"] == "owner" else "managers.manager", name=esc(a["name"])))
        if a["role"] == "manager":
            rows.append([(t(lang, "managers.remove", name=a["name"]), f"a:rm:{a['tg_id']}")])
    text, markup = "\n".join(lines), kb(*rows)
    if edit_in_place:
        await edit(ev, text, markup)
    else:
        await say(ev, text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^a:(rm|to|toy|x):(\d+)$").as_("m"))
async def on_admin(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "owner"):
        return
    action, tg_id = m.group(1), int(m.group(2))
    if action == "rm":
        r = await call(S.teams.remove_admin, tid(entry), cq.from_user.id, tg_id)
        await clear_admin_commands(cq.bot, tg_id)
        await cq.answer(strip_tags(t(lang, "managers.removed", name=r["name"])))
        try:
            await cq.bot.send_message(tg_id, t(await lang_for(tg_id, group_lang(entry)), "managers.removed_user",
                                               team=esc(entry.team["name"])))
        except Exception:
            pass
        await managers_view(cq, entry, lang, edit_in_place=True)
    elif action == "to":
        admins = await call(S.teams.list_admins, tid(entry))
        name = next((a["name"] for a in admins if a["tg_id"] == tg_id), str(tg_id))
        await cq.answer()
        await edit(cq, t(lang, "to.confirm", name=esc(name)),
                   kb([(t(lang, "to.yes"), f"a:toy:{tg_id}"), (t(lang, "btn.cancel"), "a:x:0")]))
    elif action == "toy":
        r = await call(S.teams.transfer_owner, tid(entry), cq.from_user.id, tg_id)
        await set_admin_commands(cq.bot, r["tg_id"], "owner")
        await set_admin_commands(cq.bot, r["previous_owner"], "manager")
        await cq.answer()
        await edit(cq, t(lang, "to.done", name=esc(r["name"])))
        try:
            await cq.bot.send_message(r["tg_id"], t(await lang_for(r["tg_id"], group_lang(entry)), "to.done_user",
                                                    team=esc(entry.team["name"])))
        except Exception:
            pass
    else:
        await cq.answer()
        await edit(cq, t(lang, "cancelled"))


# ---- unmatched payments --------------------------------------------------------------------------

def unmatched_card(lang, p):
    reason = t(lang, f"reason.{p.get('reason') or 'no_code'}", needed=money(p.get("needed") or 0, lang))
    text = t(lang, "unmatched.card", amount=money(p["amount"], lang), content=esc(p.get("content") or "—"),
             at=p["at"], reason=reason)
    return text, kb([(t(lang, "btn.assign"), f"u:a:{p['payment_id']}")],
                    [(t(lang, "btn.dismiss"), f"u:d:{p['payment_id']}")])


@router.callback_query(F.data.regexp(r"^u:(a|d):(\d+)$").as_("m"))
async def on_unmatched(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    action, pid = m.group(1), m.group(2)
    if action == "d":
        await call(S.money.dismiss, tid(entry), cq.from_user.id, int(pid))
        await cq.answer()
        await edit(cq, f"{esc(cq.message.text or '')}\n\n{t(lang, 'dismiss.done')}")
        return
    p = next((x for x in await call(S.money.unmatched, tid(entry), cq.from_user.id) if str(x["payment_id"]) == pid), None)
    if not p:
        await cq.answer(t(lang, "err.already_resolved"), show_alert=True)
        return
    unpaid = await call(S.money.unpaid, tid(entry))
    if not unpaid:
        await cq.answer(t(lang, "pick.markpaid_none"), show_alert=True)
        return
    await cq.answer()
    await open_picker(cq, lang, {"kind": "assign", "title": t(lang, "pick.assign", amount=money(p["amount"], lang)),
                                 "options": unpaid_options(lang, unpaid), "selected": [],
                                 "data": {"payment_id": p["payment_id"]}})


# ---- commands: everyone --------------------------------------------------------------------------

@router.message(Command("start", "pay"))
async def cmd_start(message: Message, entry, lang):
    if not is_private(message):
        await message.answer(t(lang, "btn.pay_now"), reply_markup=pay_keyboard(lang, entry.username))
        return
    await open_pay_menu(message, entry, lang)


@router.message(Command("status"))
async def cmd_status(message: Message, entry, lang):
    s = await call(S.money.status, tid(entry))
    await message.answer(status_text(lang, s), reply_markup=pay_keyboard(lang, entry.username) if s["unpaid"] else None)


@router.message(Command("fund"))
async def cmd_fund(message: Message, entry, lang):
    f = await call(S.money.fund, tid(entry))
    text = t(lang, "fund.text", income=money(f["income"], lang), expense=money(f["expense"], lang),
             balance=money(f["balance"], lang))
    if f["unmatched"] and is_private(message) and await role(entry, message.from_user) in ("owner", "manager"):
        text += "\n\n" + t(lang, "fund.unmatched", n=f["unmatched"])
    await message.answer(text)


@router.message(Command("bank"))
async def cmd_bank(message: Message, entry, lang):
    """The account members pay into. Public: it is printed on every QR code anyway."""
    s = await call(S.teams.team_info, tid(entry), message.from_user.id if message.from_user else None)
    if not s["bank_account"]:
        await message.answer(t(lang, "bank.not_set"))
        return
    text = t(lang, "bank.text", bank=esc(s["bank_name"]), account=esc(s["bank_account"]),
             holder=esc(s["bank_holder"]))
    if is_private(message) and s["role"] in ("owner", "manager"):
        text += "\n" + t(lang, "bank.sepay_on" if s["sepay_connected"] else "bank.sepay_off")
    await message.answer(text)


@router.message(Command("history"))
async def cmd_history(message: Message, entry, lang):
    items = await call(S.money.history, tid(entry))
    if not items:
        await message.answer(t(lang, "history.empty"))
        return
    lines = [f"{'➕' if i['type'] == 'in' else '➖'} {i['at']} · <b>{money(i['amount'], lang)}</b> · {esc(i['title'])}"
             f"{' (' + t(lang, 'history.cash') + ')' if i['source'] == 'cash' else ''}" for i in items]
    await message.answer(t(lang, "history.title") + "\n" + "\n".join(lines))


@router.message(Command("lang"))
async def cmd_lang(message: Message, lang):
    await message.answer(t(lang, "lang.choose"), reply_markup=kb([("English", "l:en"), ("Tiếng Việt", "l:vi")]))


@router.callback_query(F.data.regexp(r"^l:(en|vi)$").as_("m"))
async def on_lang(cq: CallbackQuery, m: re.Match):
    await set_lang(cq.from_user.id, m.group(1))
    await cq.answer()
    await edit(cq, t(m.group(1), "lang.done"))


@router.message(Command("help"))
async def cmd_help(message: Message, entry, lang):
    r = await role(entry, message.from_user)
    text = t(lang, "help.everyone")
    if r:
        text += t(lang, "help.manager")
    if r == "owner":
        text += t(lang, "help.owner")
    await message.answer(text)


# ---- commands: managers --------------------------------------------------------------------------

@router.message(Command("members"))
async def cmd_members(message: Message, entry, lang):
    if await allow(message, entry, lang, "manager"):
        await members_list(message, entry, lang)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    if command.args and command.args.strip():
        await add_members(message, entry, lang, command.args)
        return
    set_state(input_key(message.bot.id, message.chat.id), {"type": "add"}, 600)
    await message.answer(t(lang, "add.prompt"), reply_markup=FORCE)


@router.message(Command("setfee"))
async def cmd_setfee(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    raw = (command.args or "").strip()
    fee = 0 if raw == "0" else parse_amount(raw)
    if fee is None:
        await message.answer(t(lang, "usage.setfee"))
        return
    r = await call(S.teams.update_settings, tid(entry), message.from_user.id, fee=fee)
    await message.answer(t(lang, "setfee.done", fee=money(r["fee"], lang)))


@router.message(Command("setdue"))
async def cmd_setdue(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 28:
        await message.answer(t(lang, "usage.setdue"))
        return
    r = await call(S.teams.update_settings, tid(entry), message.from_user.id, due_day=int(raw))
    await message.answer(t(lang, "setdue.done", day=r["due_day"]))


async def _charge(message: Message, command: CommandObject, entry, lang, kind: str):
    if not await allow(message, entry, lang, "manager"):
        return
    amount, title = amount_and_title(command.args)
    if not amount or not title:
        await message.answer(t(lang, f"usage.{kind}"))
        return
    members = [m for m in await call(S.members.list_members, tid(entry), message.from_user.id) if m["status"] == "active"]
    if not members:
        await message.answer(t(lang, "err.no_members"))
        return
    await open_picker(message, lang, {
        "kind": kind, "title": t(lang, f"pick.{kind}", title=esc(title), amount=money(amount, lang)),
        "options": [{"id": m["id"], "label": m["name"]} for m in members],
        "selected": [m["id"] for m in members], "data": {"title": title, "amount": amount}})


@router.message(Command("charge"))
async def cmd_charge(message: Message, command: CommandObject, entry, lang):
    await _charge(message, command, entry, lang, "charge")


@router.message(Command("split"))
async def cmd_split(message: Message, command: CommandObject, entry, lang):
    await _charge(message, command, entry, lang, "split")


@router.message(Command("markpaid"))
async def cmd_markpaid(message: Message, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    unpaid = await call(S.money.unpaid, tid(entry))
    if not unpaid:
        await message.answer(t(lang, "pick.markpaid_none"))
        return
    await open_picker(message, lang, {"kind": "markpaid", "title": t(lang, "pick.markpaid"),
                                      "options": unpaid_options(lang, unpaid), "selected": []})


@router.message(Command("expense"))  # also matches a receipt photo with the command as its caption
async def cmd_expense(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    amount, title = amount_and_title(command.args)
    if not amount or not title:
        await message.answer(t(lang, "usage.expense"))
        return
    photo = message.photo[-1].file_id if message.photo else None
    r = await call(S.money.add_expense, tid(entry), message.from_user.id, amount, title, photo)
    await message.answer(t(lang, "expense.done", balance=money(r["balance"], lang)))
    G = group_lang(entry)
    await post_to_group(entry, t(G, "group.expense", amount=money(r["amount"], G), title=esc(r["title"]),
                                 by=esc(r["by"]), balance=money(r["balance"], G)), photo=photo)


@router.message(Command("remind"))
async def cmd_remind(message: Message, entry, lang):
    if not await allow(message, entry, lang, "manager", private_only=False):
        return
    G = group_lang(entry)
    text = status_text(G, await call(S.money.status, tid(entry)))
    markup = pay_keyboard(G, entry.username)
    group = entry.team.get("group_chat_id")
    if group and message.chat.id != group:
        await post_to_group(entry, text, reply_markup=markup)
        await message.answer("✅")
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("unmatched"))
async def cmd_unmatched(message: Message, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    items = await call(S.money.unmatched, tid(entry), message.from_user.id)
    if not items:
        await message.answer(t(lang, "unmatched.none"))
        return
    for p in items:
        text, markup = unmatched_card(lang, p)
        await message.answer(text, reply_markup=markup)


@router.message(Command("export"))
async def cmd_export(message: Message, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    data = await call(S.money.export, tid(entry), message.from_user.id)
    name = re.sub(r"[^\w]+", "_", entry.team["name"]).strip("_") or "team"
    await message.answer_document(BufferedInputFile(data, f"{name}_{date.today().isoformat()}.xlsx"),
                                  caption=t(lang, "export.caption", team=esc(entry.team["name"])))


@router.message(Command("settings"))
async def cmd_settings(message: Message, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    s = await call(S.teams.team_info, tid(entry), message.from_user.id)
    none = t(lang, "settings.not_set")
    bank = (f"{esc(s['bank_name'])} · {esc(s['bank_account'])} · {esc(s['bank_holder'])}"
            if s["bank_account"] else none)
    await message.answer(t(lang, "settings.text", team=esc(s["name"]), fee=money(s["fee"], lang) if s["fee"] else none,
                           due=s["due_day"], bank=bank, sepay="✅" if s["sepay_connected"] else none,
                           group=t(lang, "settings.linked") if s["group_chat_id"] else none,
                           lang="Tiếng Việt" if s["lang"] == "vi" else "English"))


@router.message(Command("teamlang"))
async def cmd_teamlang(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    new = (command.args or "").strip().lower()
    if new not in ("en", "vi"):
        await message.answer(t(lang, "usage.teamlang"))
        return
    await call(S.teams.update_settings, tid(entry), message.from_user.id, lang=new)
    entry.team["lang"] = new
    await message.answer(t(new, "teamlang.done"))


@router.message(Command("linkgroup"))
async def cmd_linkgroup(message: Message, entry, lang):
    if is_private(message):
        await message.answer(t(lang, "err.group_only"))
        return
    if not await allow(message, entry, lang, "manager", private_only=False):
        return
    await call(S.teams.link_group, tid(entry), message.from_user.id, message.chat.id)
    entry.team["group_chat_id"] = message.chat.id
    await message.answer(t(group_lang(entry), "group.linked", team=esc(entry.team["name"])))


# ---- commands: owner --------------------------------------------------------------------------------

@router.message(Command("setbank"))
async def cmd_setbank(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "owner"):
        return
    m = re.match(r"^(\S+)\s+(\S+)\s+(.+)$", (command.args or "").strip())
    if not m:
        await message.answer(t(lang, "usage.setbank"))
        return
    r = await call(S.teams.set_bank, tid(entry), message.from_user.id, m.group(1), m.group(2), m.group(3))
    await message.answer(t(lang, "setbank.done"))
    G = group_lang(entry)
    await post_to_group(entry, t(G, "group.bank_changed", by=esc(r["by"]), bank=esc(r["bank_name"]),
                                 account=esc(r["bank_account"]), holder=esc(r["bank_holder"])))


@router.message(Command("sepay"))
async def cmd_sepay(message: Message, entry, lang):
    """Connect SePay: with the owner's SePay API token, create or update the team's one webhook."""
    if not await allow(message, entry, lang, "owner"):
        return
    info = await call(S.teams.sepay_info, tid(entry), message.from_user.id)
    if not info["has_bank"]:
        await message.answer(t(lang, "err.bank_not_set"))
    elif not info["token_set"]:
        await ask_sepay_token(message, entry, lang)
    else:
        await sync_sepay(message, entry, lang, message.from_user.id)


@router.message(Command("sepaytoken"))
async def cmd_sepaytoken(message: Message, command: CommandObject, entry, lang):
    """/sepaytoken <SePay API token>: save the owner's token and set up the webhook. No token: show how to get one."""
    token = (command.args or "").strip()
    if token:
        try:  # remove it from the chat first, whoever sent it and wherever
            await message.delete()
        except Exception:
            pass
    if not await allow(message, entry, lang, "owner"):
        return
    if not token:
        await ask_sepay_token(message, entry, lang)
        return
    await save_sepay_token(message, entry, lang, token, delete=False)


async def ask_sepay_token(ev, entry, lang):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    set_state(input_key(msg.bot.id, msg.chat.id), {"type": "sepay_token"}, 600)
    await msg.answer(t(lang, "sepay.ask_token"), reply_markup=FORCE, disable_web_page_preview=True)


async def sync_sepay(msg: Message, entry, lang, actor: int):
    """Create or overwrite the team's webhook in SePay and report the result with the follow-up buttons."""
    buttons = kb([(t(lang, "btn.sepay_sync"), "s:sync")], [(t(lang, "btn.sepay_token"), "s:tok")],
                 [(t(lang, "btn.sepay_manual"), "s:man")])
    wait = await msg.answer(t(lang, "sepay.syncing"))
    try:
        r = await call(S.teams.sync_sepay, tid(entry), actor)
    except DomainError as e:
        await wait.edit_text(error_text(lang, e), reply_markup=buttons)
        return
    await wait.edit_text(t(lang, "sepay.synced_new" if r["created"] else "sepay.synced_updated",
                           bank=esc(r["bank"]), account=esc(r["account"]), holder=esc(r["holder"]),
                           url=esc(r["url"])), reply_markup=buttons)


@router.callback_query(F.data.in_({"s:sync", "s:tok", "s:man"}))
async def on_sepay_button(cq: CallbackQuery, entry, lang):
    if not await allow(cq, entry, lang, "owner"):
        return
    await cq.answer()
    if cq.data == "s:sync":
        await sync_sepay(cq.message, entry, lang, cq.from_user.id)
    elif cq.data == "s:tok":
        await ask_sepay_token(cq, entry, lang)
    else:
        s = await call(S.teams.sepay_info, tid(entry), cq.from_user.id)
        await cq.message.answer(t(lang, "sepay.info", url=esc(s["webhook_url"]),
                                  status=t(lang, "sepay.connected" if s["connected"] else "sepay.not_connected")),
                                reply_markup=kb([(t(lang, "btn.gen_key"), "s:gen")]))


async def save_sepay_token(message: Message, entry, lang, text: str, delete: bool = True):
    if delete:
        try:  # the token gives access to the SePay account: don't leave it in the chat
            await message.delete()
        except Exception:
            pass
    r = await call(S.teams.set_sepay_token, tid(entry), message.from_user.id, text)
    await message.answer(t(lang, "sepay.token_saved", n=r["bank_accounts"]))
    await sync_sepay(message, entry, lang, message.from_user.id)


@router.callback_query(F.data == "s:gen")
async def on_sepay_key(cq: CallbackQuery, entry, lang):
    if not await allow(cq, entry, lang, "owner"):
        return
    r = await call(S.teams.new_sepay_key, tid(entry), cq.from_user.id)
    await cq.answer()
    await say(cq, t(lang, "sepay.key", key=esc(r["api_key"])))


@router.message(Command("requests"))
async def cmd_requests(message: Message, entry, lang):
    if not await allow(message, entry, lang, "owner"):
        return
    items = await call(S.teams.pending_manager_requests, tid(entry), message.from_user.id)
    if not items:
        await message.answer(t(lang, "mr.none"))
        return
    for r in items:
        text, markup = request_card(lang, r)
        await message.answer(text, reply_markup=markup)


@router.message(Command("managers"))
async def cmd_managers(message: Message, entry, lang):
    if await allow(message, entry, lang, "owner"):
        await managers_view(message, entry, lang)


@router.message(Command("transferowner"))
async def cmd_transferowner(message: Message, entry, lang):
    if not await allow(message, entry, lang, "owner"):
        return
    managers = [a for a in await call(S.teams.list_admins, tid(entry)) if a["role"] == "manager"]
    if not managers:
        await message.answer(t(lang, "to.none"))
        return
    await message.answer(t(lang, "to.choose"), reply_markup=kb(*[[(a["name"], f"a:to:{a['tg_id']}")] for a in managers]))


@router.message(Command("log"))
async def cmd_log(message: Message, entry, lang):
    if not await allow(message, entry, lang, "owner"):
        return
    rows = await call(S.teams.audit_log, tid(entry), message.from_user.id)
    if not rows:
        await message.answer(t(lang, "log.empty"))
        return
    lines = [f"{r['at']} · {esc(r['actor'])} · <code>{esc(r['action'])}</code>"
             f"{' ' + esc(r['target']) if r['target'] else ''}" for r in rows]
    await message.answer(t(lang, "log.title") + "\n" + "\n".join(lines))


# ---- replies to "send me ..." prompts, group joins, stale buttons ---------------------------------------

@router.message(F.text)
async def on_text(message: Message, entry, lang):
    if not is_private(message) or message.text.startswith("/"):
        return
    key = input_key(message.bot.id, message.chat.id)
    st = get_state(key)
    if not st:
        return
    del_state(key)
    if not await allow(message, entry, lang, "manager"):
        return
    text = message.text.strip()
    if st["type"] == "sepay_token":
        await save_sepay_token(message, entry, lang, text)
    elif st["type"] == "add":
        await add_members(message, entry, lang, text)
    elif st["type"] == "edit":
        if st["field"] == "fee":
            fee = None if text == "-" else 0 if text == "0" else parse_amount(text)
            if text != "-" and fee is None:
                await message.answer(t(lang, "err.invalid"))
                return
            body = {"fee_override": fee}
        else:
            body = {st["field"]: None if text == "-" else text}
        mem = await call(S.members.update_member, tid(entry), message.from_user.id, st["member_id"], body)
        view, markup = member_view(lang, mem)
        await message.answer(f"{t(lang, 'member.updated')}\n\n{view}", reply_markup=markup)


@router.my_chat_member()
async def on_added_to_group(event: ChatMemberUpdated, entry):
    if event.chat.type != "private" and event.new_chat_member.status in ("member", "administrator"):
        await event.bot.send_message(event.chat.id, t(group_lang(entry), "group.added"))


@router.callback_query()
async def on_stale_button(cq: CallbackQuery):
    await cq.answer()
