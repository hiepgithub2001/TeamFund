"""Handlers shared by every team bot. The registry's middleware passes `entry` (bot + team) and `lang`."""
import asyncio
import contextvars
import logging
import re
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, ForceReply, Message

from ..deps import S
from .commands import clear_admin_commands, set_admin_commands
from .fmt import amount_and_title, esc, money, parse_amount, parse_periods, split_date, strip_tags
from .i18n import has, t
from .lang import lang_for, set_lang
from ...domain.errors import DomainError
from ...domain.rules import add_months, cycle_start, period_end, period_name, split_share
from .render import (bill_label, edit, error_text, fee_text, is_private, kb, owes_lines, pay_keyboard, say,
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


def window_key(bot_id, chat_id) -> str:
    return f"win:{bot_id}:{chat_id}"


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


# who is acting in the current update: announcements skip them (they already got the reply)
acting_user: contextvars.ContextVar[int | None] = contextvars.ContextVar("acting_user", default=None)


async def send_to(entry, tg_id: int, text: str, photo: str | None = None, reply_markup=None) -> bool:
    """One private message from the team bot. Someone who blocked the bot is muted."""
    try:
        if photo:
            await entry.bot.send_photo(tg_id, photo, caption=text, reply_markup=reply_markup)
        else:
            await entry.bot.send_message(tg_id, text, reply_markup=reply_markup)
        return True
    except TelegramForbiddenError:
        await call(S.teams.set_muted, entry.team["team_id"], tg_id, True)
    except Exception as e:
        log.warning("message to %s failed (team %s): %s", tg_id, entry.team["team_id"], e)
    return False


async def broadcast(entry, text: str, photo: str | None = None, reply_markup=None) -> int:
    """A team announcement, privately to every follower except whoever caused it. Returns how many got it."""
    sent, skip = 0, acting_user.get()
    for tg_id in await call(S.teams.followers, entry.team["team_id"]):
        if tg_id != skip:
            sent += await send_to(entry, tg_id, text, photo, reply_markup)
            await asyncio.sleep(0.05)  # stay well under Telegram's 30 messages a second
    return sent


# ---- multi-select picker (pay, charge, split, cash, assign) ------------------------------------------

def picker_keyboard(lang, st):
    per_row = 1 if st["kind"] in ("pay", "markpaid", "assign", "plan", "window") else 2
    buttons = [(f"{'✅' if o['id'] in st['selected'] else '☐'} {o['label']}", f"k:t:{i}")
               for i, o in enumerate(st["options"])]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    n = len(st["selected"])
    if st["kind"] == "pay":
        total = sum(o["amount"] for o in st["options"] if o["id"] in st["selected"])
        rows += [[(t(lang, "pay.button", count=n, amount=money(total, lang)), "k:ok")],
                 [("🔄", "k:r"), (t(lang, "btn.donate"), "dn:a")]]
    else:
        everyone = n == len(st["options"]) and n > 0
        data = st.get("data") or {}
        if st["kind"] in ("charge", "split") and data.get("choose"):
            a = data["amount"]
            each = t(lang, "btn.charge_each", amount=money(a, lang))
            split = t(lang, "btn.charge_split", amount=money(a, lang))
            if n:
                each += " · " + t(lang, "pick.total", amount=money(a * n, lang))
                split += " · " + t(lang, "pick.each", amount=money(split_share(a, n), lang))
            rows += [[(t(lang, "btn.none") if everyone else t(lang, "btn.everyone", n=len(st["options"])), "k:a")],
                     [(each, "k:ok:1")], [(split, "k:ok:2")], [(t(lang, "btn.cancel"), "k:x")]]
            return kb(*rows)
        confirm = t(lang, "btn.confirm", n=n)
        amount = (st.get("data") or {}).get("amount")
        if st["kind"] in ("charge", "split") and n and amount:  # what the choice costs
            confirm += " · " + (t(lang, "pick.total", amount=money(amount * n, lang)) if st["kind"] == "charge"
                                else t(lang, "pick.each", amount=money(split_share(amount, n), lang)))
        rows += [[(t(lang, "btn.none" if everyone else "btn.all"), "k:a")], [(confirm, "k:ok")],
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
        await say(ev, t(lang, "pay.none"), reply_markup=kb([(t(lang, "btn.donate"), "dn:a")]))
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
        await broadcast(entry, t(G, "group.charge", title=esc(r["title"]), each=money(r["each"], G),
                                     count=r["count"]), reply_markup=pay_keyboard(G, entry.username))
    elif st["kind"] == "markpaid" and len(ids) == 1:
        # one person: ask whether the full amount came in, or something else
        o = await call(S.money.owed_by, tid(entry), cq.from_user.id, ids[0])
        del_state(key)
        await cq.answer()
        set_state(key, {"type": "cash", "member_id": o["member_id"], "name": o["name"], "owed": o["total"]}, 600)
        await edit(cq, t(lang, "cash.how_much", name=esc(o["name"]), owed=money(o["total"], lang)), kb(
            [(t(lang, "btn.cash_full", amount=money(o["total"], lang)), "mp:full")],
            [(t(lang, "btn.cash_other"), "mp:other")], [(t(lang, "btn.cancel"), "mp:x")]))
    elif st["kind"] == "markpaid":
        r = await call(S.money.markpaid, tid(entry), cq.from_user.id, ids)
        del_state(key)
        await cq.answer()
        members = esc(", ".join(r["members"]))
        await edit(cq, t(lang, "markpaid.done", members=members, amount=money(r["amount"], lang)))
        await broadcast(entry, t(G, "group.paid", members=members, amount=money(r["amount"], G)))
    elif st["kind"] == "plan":
        dt = st["data"]
        plan = await call(S.plans.save_plan, tid(entry), cq.from_user.id, dt["amount"], dt["months"], ids,
                          dt.get("plan_id"))
        del_state(key)
        await cq.answer()
        text, markup = plan_view(lang, plan)
        await edit(cq, f"{t(lang, 'plans.saved')}\n\n{text}", markup)
        joined = [m for m in plan["members"] if m["id"] not in dt.get("before", [])]
        if joined:
            await open_window_menu(cq, lang, plan, joined)
        if plan.get("conflicts"):  # overlapping another plan of theirs: not added; ask for other periods
            await say(cq, clash_text(lang, plan["conflicts"]) + "\n\n" + t(lang, "plans.overlap_hint"))
            seen = {}
            for c in plan["conflicts"]:
                seen.setdefault(c["member_id"], {"id": c["member_id"], "name": c["member"]})
            await open_window_menu(cq, lang, plan, list(seen.values()))
    elif st["kind"] == "window":
        plan = await call(S.plans.get_plan, tid(entry), cq.from_user.id, st["data"]["plan_id"])
        del_state(key)
        await cq.answer()
        chosen = [m for m in plan["members"] if m["id"] in ids]
        await open_window_menu(cq, lang, plan, chosen, edit_in_place=True)
    elif st["kind"] == "assign":
        r = await call(S.money.assign, tid(entry), cq.from_user.id, st["data"]["payment_id"], ids)
        del_state(key)
        await cq.answer()
        members = esc(", ".join(r["members"]))
        await edit(cq, t(lang, "assign.done", members=members))
        await broadcast(entry, t(G, "group.paid", members=members, amount=money(r["amount"], G)))


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
        if arg in ("1", "2") and st["kind"] in ("charge", "split"):  # /charge: the button says how
            st["kind"] = "charge" if arg == "1" else "split"
        await on_picker_confirm(cq, entry, lang, st, key)
        return
    set_state(key, st)
    await cq.answer()
    await edit(cq, st["title"], picker_keyboard(lang, st))


# ---- members ----------------------------------------------------------------------------------

async def members_list(ev, entry, lang, edit_in_place: bool = False) -> None:
    members = await call(S.members.list_members, tid(entry), ev.from_user.id)
    add = [(t(lang, "btn.add_members"), "m:add")]
    if not members:
        text, markup = t(lang, "members.empty"), kb(add)
    else:
        buttons = [(f"{'⏸ ' if m['status'] == 'paused' else ''}{m['name']}"
                    f"{' #' + m['shirt_number'] if m['shirt_number'] else ''}", f"m:v:{m['id']}") for m in members]
        markup = kb(*[buttons[i:i + 2] for i in range(0, len(buttons), 2)], add)
        text = t(lang, "members.title", n=len(members))
    if edit_in_place:
        await edit(ev, text, markup)
    else:
        await say(ev, text, reply_markup=markup)


def member_pack_line(lang: str, p: dict) -> str:
    """"• 200.000đ/quý · 2026Q4 ✅ đang áp dụng", "• 600.000đ/quý · từ 2027Q1"."""
    n = p["months"]
    a = period_name(p["from"], n) if p.get("from") else None
    b = period_name(p["until"], n) if p.get("until") else None
    if a and b:
        when = a if a == b else f"{a} – {b}"
    elif a:
        when = t(lang, "member.pack_from", p=a)
    else:
        when = t(lang, "member.pack_until", p=b) if b else ""
    tag = t(lang, "member.pack_now") if p.get("now") else ""
    return f"• {fee_text(lang, p['amount'], n)} · {when}{tag}"


def member_view(lang, m):
    dash = lambda v: esc(v) if v else "—"  # noqa: E731
    text = t(lang, "member.detail", name=esc(m["name"]), nickname=dash(m["nickname"]), shirt=dash(m["shirt_number"]),
             phone=dash(m["phone"]), note=dash(m["note"]), fee=fee_text(lang, m["fee"], m.get("months", 1)),
             custom=t(lang, "member.on_plan") if m.get("on_plan")
             else t(lang, "member.custom") if m["fee_override"] is not None else "",
             status=t(lang, f"status.{m['status']}"), owes=money(m["owes"], lang))
    if m.get("plans"):
        text += "\n\n" + t(lang, "member.plans", n=len(m["plans"]))
        for p in m["plans"]:
            text += "\n" + member_pack_line(lang, p)
    i = m["id"]
    paused = m["status"] == "paused"
    markup = kb(
        [(t(lang, "btn.name"), f"m:e:{i}:name"), (t(lang, "btn.nickname"), f"m:e:{i}:nickname")],
        [(t(lang, "btn.shirt_number"), f"m:e:{i}:shirt_number"), (t(lang, "btn.phone"), f"m:e:{i}:phone")],
        [(t(lang, "btn.note"), f"m:e:{i}:note"), (t(lang, "btn.fee"), f"m:e:{i}:fee")],
        [(t(lang, "btn.activate" if paused else "btn.pause"), f"m:s:{i}:{'active' if paused else 'paused'}"),
         (t(lang, "btn.bills"), f"m:b:{i}")],
        [(t(lang, "btn.member_packs"), f"m:pk:{i}")],
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


async def member_packs_view(ev, entry, lang, member_id: int, note: str = "", edit_in_place: bool = True) -> None:
    """One member's packages: each is a button (change its periods, take them off it), plus ➕ add one."""
    mem = await call(S.members.get_member, tid(entry), ev.from_user.id, member_id)
    lines = [member_pack_line(lang, p) for p in mem["plans"]] or [t(lang, "member.no_packs")]
    text = (note + "\n\n" if note else "") + t(lang, "member.packs_title", name=esc(mem["name"])) + "\n" + "\n".join(lines)
    rows = [[(f"✏️ {fee_text(lang, p['amount'], p['months'])}", f"m:po:{member_id}:{p['plan_id']}")]
            for p in mem["plans"]]
    rows += [[(t(lang, "btn.pack_add"), f"m:pa:{member_id}")], [(t(lang, "btn.back"), f"m:v:{member_id}")]]
    if edit_in_place:
        await edit(ev, text, kb(*rows))
    else:
        await say(ev, text, reply_markup=kb(*rows))


async def member_pack_action(cq, entry, lang, action: str, mid: int, arg: str | None) -> None:
    user = cq.from_user
    if action == "pk":
        await member_packs_view(cq, entry, lang, mid)
    elif action == "po":  # one package: change its periods or take them off it
        mem = await call(S.members.get_member, tid(entry), user.id, mid)
        p = next((x for x in mem["plans"] if x["plan_id"] == int(arg)), None)
        if not p:
            await member_packs_view(cq, entry, lang, mid)
            return
        await edit(cq, f"<b>{esc(mem['name'])}</b>\n{member_pack_line(lang, p)}",
                   kb([(t(lang, "btn.pack_window"), f"m:pw:{mid}:{arg}")],
                      [(t(lang, "btn.pack_drop"), f"m:pd:{mid}:{arg}")],
                      [(t(lang, "btn.back"), f"m:pk:{mid}")]))
    elif action in ("pw", "pj"):  # periods for this package (joining it if they aren't on it yet)
        mem = await call(S.members.get_member, tid(entry), user.id, mid)
        plan = await call(S.plans.get_plan, tid(entry), user.id, int(arg))
        await open_window_menu(cq, lang, plan, [{"id": mid, "name": mem["name"]}], edit_in_place=True, member_id=mid)
    elif action == "pd":
        await edit(cq, t(lang, "member.pack_drop_confirm"),
                   kb([(t(lang, "btn.pack_drop_yes"), f"m:pdy:{mid}:{arg}"), (t(lang, "btn.cancel"), f"m:pk:{mid}")]))
    elif action == "pdy":
        await call(S.plans.remove_members, tid(entry), user.id, int(arg), [mid])
        await member_packs_view(cq, entry, lang, mid, note=t(lang, "member.pack_dropped"))
    elif action == "pa":  # pick one of the team's packages, or type a new one
        v = await call(S.plans.list_plans, tid(entry), user.id)
        mem = await call(S.members.get_member, tid(entry), user.id, mid)
        mine = {p["plan_id"] for p in mem["plans"]}
        rows = [[(f"➕ {fee_text(lang, p['amount'], p['months'])}", f"m:pj:{mid}:{p['id']}")]
                for p in v["plans"] if p["id"] not in mine]
        rows += [[(t(lang, "btn.pack_new"), f"m:pn:{mid}")], [(t(lang, "btn.back"), f"m:pk:{mid}")]]
        await edit(cq, t(lang, "member.pack_pick", name=esc(mem["name"])), kb(*rows))
    elif action == "pn":
        set_state(input_key(cq.bot.id, cq.message.chat.id), {"type": "member_pack", "member_id": mid}, 600)
        await say(cq, t(lang, "member.pack_new_ask"), reply_markup=ForceReply(force_reply=True,
                                                                               input_field_placeholder="300k quý"))


async def member_pack_reply(message: Message, entry, lang, st: dict, text: str) -> None:
    """A new package typed for one member ("300k quý"): made (or the same one found), then its periods."""
    amount, rest = amount_and_title(text)
    months = parse_cycle(rest or "")
    if not amount or not months:
        set_state(input_key(message.bot.id, message.chat.id), st, 600)
        await message.answer(t(lang, "err.invalid") + "\n\n" + t(lang, "member.pack_new_ask"), reply_markup=FORCE)
        return
    mid = st["member_id"]
    plan = await call(S.plans.save_plan, tid(entry), message.from_user.id, amount, months, [mid])
    mem = await call(S.members.get_member, tid(entry), message.from_user.id, mid)
    if plan.get("conflicts"):
        await message.answer(clash_text(lang, plan["conflicts"]) + "\n\n" + t(lang, "plans.overlap_hint"))
    await open_window_menu(message, lang, plan, [{"id": mid, "name": mem["name"]}], member_id=mid)


@router.callback_query(F.data.regexp(r"^m:(pk|po|pw|pj|pd|pdy|pa|pn)(?::(\d+))(?::(\d+))?$").as_("m"))
async def on_member_pack(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    await cq.answer()
    await member_pack_action(cq, entry, lang, m.group(1), int(m.group(2)), m.group(3))


@router.callback_query(F.data.regexp(r"^m:(l|v|e|s|b|rm|rmy|add)(?::(\d+))?(?::(\w+))?$").as_("m"))
async def on_member(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    action, mid, arg = m.group(1), m.group(2), m.group(3)
    user = cq.from_user
    if action == "add":  # same as /add: one member per line
        set_state(input_key(cq.bot.id, cq.message.chat.id), {"type": "add"}, 600)
        await cq.answer()
        await say(cq, t(lang, "add.prompt"), reply_markup=FORCE)
    elif action == "l":
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
        if arg == "fee" and mem.get("on_plan"):
            prompt += t(lang, "member.ask_fee_plan", name=esc(mem["name"]))
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
    await send_manager_request(message, entry, lang, message.from_user.id)


@router.callback_query(F.data == "mr:ask")
async def on_request_manager(cq: CallbackQuery, entry, lang):
    await cq.answer()
    await send_manager_request(cq.message, entry, lang, cq.from_user.id)


async def send_manager_request(msg: Message, entry, lang, actor: int):
    r = await call(S.teams.request_manager, tid(entry), actor)
    await msg.answer(t(lang, "mr.sent"))
    text, markup = request_card(await lang_for(r["owner_tg_id"], group_lang(entry)), r)
    try:
        await msg.bot.send_message(r["owner_tg_id"], text, reply_markup=markup)
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
        await broadcast(entry, t(group_lang(entry), "group.new_manager", name=esc(r["name"])))


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
async def cmd_start(message: Message, entry, lang, new_follower: bool = False):
    if not is_private(message):
        await message.answer(t(lang, "btn.pay_now"), reply_markup=pay_keyboard(lang, entry.username))
        return
    if new_follower:
        await message.answer(t(lang, "follow.welcome", team=esc(entry.team["name"])))
    await open_pay_menu(message, entry, lang)


DONATE_CHOICES = (20000, 50000, 100000, 200000)


async def ask_donation(ev, lang) -> None:
    """How much to give: a few amounts to tap, or type any multiple of 1.000đ."""
    set_state(input_key(ev.bot.id, ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id),
              {"type": "donate"}, 600)
    buttons = [(money(a, lang), f"dn:{a}") for a in DONATE_CHOICES]
    await say(ev, t(lang, "donate.ask"), reply_markup=kb(buttons[:2], buttons[2:]))


async def send_donation_qr(ev, entry, lang, amount: int) -> None:
    user = ev.from_user
    try:
        r = await call(S.money.donate, tid(entry), user.id, amount, user.full_name)
    except DomainError as e:
        if e.code != "donation_amount":
            raise
        set_state(input_key(ev.bot.id, ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id),
                  {"type": "donate"}, 600)
        await say(ev, t(lang, "err.donation_amount"))
        return
    del_state(input_key(ev.bot.id, ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id))
    caption = t(lang, "donate.qr_caption", amount=money(r["amount"], lang), bank=esc(r["bank_name"]),
                account=esc(r["bank_account"]), holder=esc(r["bank_holder"] or ""), code=esc(r["content"]),
                expires=r["expires_at"])
    chat_id = ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id
    try:
        await ev.bot.send_photo(chat_id, r["qr_url"], caption=caption)
    except Exception:
        await say(ev, f"{caption}\n\n{t(lang, 'pay.qr_failed')}")


@router.message(Command("donate"))
async def cmd_donate(message: Message, command: CommandObject, entry, lang):
    """Give money to the fund: `/donate` asks how much, `/donate 50k` makes the QR at once."""
    if not is_private(message):
        return
    args = (command.args or "").strip()
    if args:
        amount = parse_amount(args)
        if amount:
            await send_donation_qr(message, entry, lang, amount)
            return
    await ask_donation(message, lang)


@router.callback_query(F.data.regexp(r"^dn:(a|\d+)$").as_("m"))
async def on_donate(cq: CallbackQuery, m: re.Match, entry, lang):
    await cq.answer()
    if m.group(1) == "a":
        await ask_donation(cq, lang)
    else:
        await send_donation_qr(cq, entry, lang, int(m.group(1)))


@router.message(Command("notify"))
async def cmd_notify(message: Message, entry, lang):
    """Turn the team's announcements off or on for yourself."""
    if not is_private(message):
        return
    r = await call(S.teams.set_muted, tid(entry), message.from_user.id)
    await message.answer(t(lang, "follow.off" if r["muted"] else "follow.on"))


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
    text = t(lang, "bank.text", bank=esc(s["bank_name"]), account=esc(s["pay_account"]),
             holder=esc(s["bank_holder"]))
    if s["pay_account"] != s["bank_account"]:
        text += "\n" + t(lang, "bank.va_note", account=esc(s["bank_account"]))
    if is_private(message) and s["role"] in ("owner", "manager"):
        text += "\n" + t(lang, "bank.sepay_on" if s["sepay_connected"] else "bank.sepay_off")
    await message.answer(text)


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject, entry, lang):
    """The latest transactions, or `/history 10/2026` for every transaction of that month."""
    raw = (command.args or "").strip()
    period = None
    if raw:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{4})", raw)
        if not m or not 1 <= int(m.group(1)) <= 12:
            await message.answer(t(lang, "usage.history"))
            return
        period = f"{m.group(2)}-{int(m.group(1)):02d}"
    items = await call(S.money.history, tid(entry), 10, period)
    label = f"{period[5:]}/{period[:4]}" if period else None
    if not items:
        await message.answer(t(lang, "history.month_empty", period=label) if period else t(lang, "history.empty"))
        return
    lines = [f"{'➕' if i['type'] == 'in' else '➖'} {i['at']} · <b>{money(i['amount'], lang)}</b> · {esc(i['title'])}"
             f"{' (' + t(lang, 'history.' + i['source']) + ')' if i['source'] in ('cash', 'other', 'donation') else ''}"
             for i in items]
    head = t(lang, "history.month_title", period=label, n=len(items),
             income=money(sum(i["amount"] for i in items if i["type"] == "in"), lang),
             expense=money(sum(i["amount"] for i in items if i["type"] == "out"), lang)) if period \
        else t(lang, "history.title")
    chunk = head
    for line in lines:  # a month can outgrow one Telegram message (4096 characters)
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    await message.answer(chunk)


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


@router.message(Command("guide"))
async def cmd_guide(message: Message, entry, lang):
    """A short how-to for members: the bot and the pay page. Managers get the page link made if needed,
    and a hint to forward the message to the team chat."""
    manager = bool(await role(entry, message.from_user))
    if manager:
        url = (await call(S.teams.web_link, tid(entry), message.from_user.id))["url"]
    else:
        url = await call(S.teams.public_link, tid(entry))
    me = await message.bot.me()
    text = t(lang, "guide.bot", bot=esc(me.username or ""))
    text += "\n\n" + t(lang, "guide.web", link=t(lang, "guide.link", url=esc(url)) if url else "")
    if manager:
        text += "\n\n" + t(lang, "guide.forward")
    await message.answer(text, disable_web_page_preview=True)


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


async def charge_picker(ev, entry, lang, kind: str, amount: int, title: str) -> dict | None:
    """The member picker for a charge ("charge": each pays the amount; "split": they share it)."""
    members = [m for m in await call(S.members.list_members, tid(entry), ev.from_user.id) if m["status"] == "active"]
    if not members:
        await say(ev, t(lang, "err.no_members"))
        return None
    if kind == "choose":  # /charge: nobody ticked yet; the person picks people, then how
        return {"kind": "charge", "title": t(lang, "pick.choose", title=esc(title), amount=money(amount, lang)),
                "options": [{"id": m["id"], "label": m["name"]} for m in members], "selected": [],
                "data": {"title": title, "amount": amount, "choose": True}}
    return {"kind": kind, "title": t(lang, f"pick.{kind}", title=esc(title), amount=money(amount, lang)),
            "options": [{"id": m["id"], "label": m["name"]} for m in members],
            "selected": [m["id"] for m in members], "data": {"title": title, "amount": amount}}


@router.message(Command("charge"))
async def cmd_charge(message: Message, command: CommandObject, entry, lang):
    """A one-off cost for chosen members: each pays the amount, or they split it (asked with two buttons)."""
    if not await allow(message, entry, lang, "manager"):
        return
    amount, title = amount_and_title(command.args)
    if not amount or not title:
        await message.answer(t(lang, "usage.charge"))
        return
    pick = await charge_picker(message, entry, lang, "choose", amount, title)
    if pick:
        await open_picker(message, lang, pick)


@router.callback_query(F.data.regexp(r"^c:(charge|split|x)$").as_("m"))
async def on_charge_how(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    key = pick_key(cq.bot.id, cq.message.chat.id, cq.message.message_id)
    st = get_state(key)
    await cq.answer()
    if m.group(1) == "x" or not st or st.get("type") != "charge_how":
        del_state(key)
        await edit(cq, t(lang, "cancelled" if st else "err.menu_expired"))
        return
    pick = await charge_picker(cq, entry, lang, m.group(1), st["amount"], st["title"])
    if pick:  # the same message becomes the member picker
        set_state(key, pick)
        await edit(cq, pick["title"], picker_keyboard(lang, pick))


@router.message(Command("split"))  # kept for people used to it; /charge asks the same question with a button
async def cmd_split(message: Message, command: CommandObject, entry, lang):
    if not await allow(message, entry, lang, "manager"):
        return
    amount, title = amount_and_title(command.args)
    if not amount or not title:
        await message.answer(t(lang, "usage.charge"))
        return
    pick = await charge_picker(message, entry, lang, "split", amount, title)
    if pick:
        await open_picker(message, lang, pick)


async def record_cash(ev, entry, lang, member_id: int, amount: int | None, settle: bool = True) -> str:
    """Record one member's cash (all they owe when amount is None), post it in the group, return the reply."""
    if amount is None:
        r = await call(S.money.markpaid, tid(entry), ev.from_user.id, [member_id])
        text = t(lang, "markpaid.done", members=esc(r["members"][0]), amount=money(r["amount"], lang))
    else:
        r = await call(S.money.markpaid_amount, tid(entry), ev.from_user.id, member_id, amount, settle)
        text = t(lang, "cash.done_part", name=esc(r["members"][0]), amount=money(r["amount"], lang))
        if r["let_go"]:
            text += "\n" + t(lang, "cash.let_go", amount=money(r["let_go"], lang))
        if r["still_owed"]:
            text += "\n" + t(lang, "cash.still_owed", amount=money(r["still_owed"], lang))
    G = group_lang(entry)
    await broadcast(entry, t(G, "group.paid", members=esc(", ".join(r["members"])), amount=money(r["amount"], G)))
    return text


@router.callback_query(F.data.regexp(r"^mp:(full|other|x|settle|owe)$").as_("m"))
async def on_cash(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    key = pick_key(cq.bot.id, cq.message.chat.id, cq.message.message_id)
    st, action = get_state(key), m.group(1)
    await cq.answer()
    if not st or st.get("type") != "cash" or action == "x":
        del_state(key)
        await edit(cq, t(lang, "cancelled" if st else "err.menu_expired"))
        return
    if action == "full":
        del_state(key)
        await edit(cq, await record_cash(cq, entry, lang, st["member_id"], None))
    elif action == "other":
        set_state(input_key(cq.bot.id, cq.message.chat.id), {**st, "type": "cash_amount", "menu": key}, 600)
        await say(cq, t(lang, "cash.ask_amount", name=esc(st["name"]), owed=money(st["owed"], lang)), reply_markup=FORCE)
    elif action in ("settle", "owe") and st.get("amount"):
        del_state(key)
        await edit(cq, await record_cash(cq, entry, lang, st["member_id"], st["amount"], settle=action == "settle"))


async def cash_amount_reply(message: Message, entry, lang, st: dict, text: str) -> None:
    """The manager typed how much came in. Equal to what's owed: done. Less: ask what happens to the rest."""
    amount = parse_amount(text)
    if not amount or amount > st["owed"]:
        set_state(input_key(message.bot.id, message.chat.id), st, 600)
        await message.answer(t(lang, "cash.bad_amount", owed=money(st["owed"], lang)), reply_markup=FORCE)
        return
    if amount == st["owed"]:
        await message.answer(await record_cash(message, entry, lang, st["member_id"], None))
        return
    rest = money(st["owed"] - amount, lang)
    msg = await message.answer(
        t(lang, "cash.rest", name=esc(st["name"]), amount=money(amount, lang), owed=money(st["owed"], lang), rest=rest),
        reply_markup=kb([(t(lang, "btn.cash_settle", rest=rest), "mp:settle")],
                        [(t(lang, "btn.cash_owe", rest=rest), "mp:owe")], [(t(lang, "btn.cancel"), "mp:x")]))
    set_state(pick_key(msg.bot.id, msg.chat.id, msg.message_id),
              {"type": "cash", "member_id": st["member_id"], "name": st["name"], "owed": st["owed"], "amount": amount}, 600)


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


def amount_title_date(text) -> tuple[int | None, str | None, object, bool]:
    """'300k Pitch 15/10' -> (300000, 'Pitch', date, True). The last value is False for a date like 31/02."""
    amount, rest = amount_and_title(text)
    if not amount or not rest:
        return amount, rest, None, True
    title, day, ok = split_date(rest, S.money.today())
    return amount, title or None, day, ok


def on_date_text(lang: str, r: dict, key: str) -> str:
    return t(lang, key, date=r["at"][:10]) if r.get("backdated") else ""


async def ask_money(message: Message, lang: str, kind: str, error: str | None = None) -> None:
    """Explain the format (amount, note, optional date) and wait for the reply."""
    set_state(input_key(message.bot.id, message.chat.id), {"type": kind}, 600)
    text = t(lang, f"ask.{kind}")
    await message.answer(f"{t(lang, error)}\n\n{text}" if error else text, reply_markup=FORCE)


async def record_money(message: Message, entry, lang: str, kind: str, text: str | None,
                       photo: str | None = None) -> None:
    """/expense or /income with "<amount> <note> [date]", typed after the command or as a reply to the guide."""
    if not (text or "").strip():
        await ask_money(message, lang, kind)
        return
    amount, title, day, ok = amount_title_date(text)
    if not ok:
        await ask_money(message, lang, kind, "err.bad_date")
        return
    if not amount or not title:
        await ask_money(message, lang, kind, "err.money_format")
        return
    G, user = group_lang(entry), message.from_user.id
    if kind == "expense":
        r = await call(S.money.add_expense, tid(entry), user, amount, title, photo, day)
    else:
        r = await call(S.money.add_income, tid(entry), user, amount, title, day)
    await message.answer(t(lang, f"{kind}.done", date=on_date_text(lang, r, "on_date"),
                           balance=money(r["balance"], lang)))
    await broadcast(entry, t(G, f"group.{kind}", amount=money(r["amount"], G), title=esc(r["title"]),
                                 by=esc(r["by"]), date=on_date_text(G, r, "group.on_date"),
                                 balance=money(r["balance"], G)), photo=photo)


@router.message(Command("money"))
async def cmd_money(message: Message, lang):
    """How to write amounts and dates in /expense, /income, /charge, /split and /plans."""
    await message.answer(t(lang, "money.help"))


@router.message(Command("expense"))  # also matches a receipt photo with the command as its caption
async def cmd_expense(message: Message, command: CommandObject, entry, lang):
    if await allow(message, entry, lang, "manager"):
        await record_money(message, entry, lang, "expense", command.args,
                           message.photo[-1].file_id if message.photo else None)


@router.message(Command("income"))
async def cmd_income(message: Message, command: CommandObject, entry, lang):
    """Money into the fund that isn't a member's fee: a prize, a sponsor, the balance from before the bot."""
    if await allow(message, entry, lang, "manager"):
        await record_money(message, entry, lang, "income", command.args)


# ---- fee plans --------------------------------------------------------------------------------------

CYCLE_WORDS = {1: ("tháng", "thang", "month", "monthly", "m", "1"),
               3: ("quý", "quy", "quarter", "quarterly", "q", "3", "3 tháng", "3 thang", "3 months"),
               6: ("6 tháng", "6 thang", "nửa năm", "nua nam", "half", "half year", "6 months", "6"),
               12: ("năm", "nam", "year", "yearly", "annual", "12", "12 tháng", "12 thang", "12 months")}


def parse_cycle(word: str) -> int | None:
    w = re.sub(r"\s+", " ", (word or "").strip().lower().lstrip("/"))
    return next((n for n, words in CYCLE_WORDS.items() if w in words), None)


def terms_text(lang: str, x: dict) -> str:
    """"📅 ngày 15, hạn +5 ngày · ⏰ 1.000đ/ngày" (with "(như mặc định)" where a plan follows the default)."""
    if not x.get("own_schedule", True) and not x.get("own_late", True):
        return t(lang, "plans.terms_default")  # follows the default package in both: say just that
    inherit = t(lang, "plans.inherit")
    sched = t(lang, "plans.schedule", notify=x["notify_day"], days=x["due_days"])
    late = t(lang, "plans.late_on", fee=money(x["late_fee"], lang)) if x["late_fee"] else t(lang, "plans.late_off")
    return (f"{sched}{inherit if not x.get('own_schedule', True) else ''} · "
            f"{late}{inherit if not x.get('own_late', True) else ''}")


def window_text(lang: str, m: dict, months: int, period: str) -> str:
    """How long a member is on a plan, when it isn't simply "from this period, for good":
    " (từ 2027Q1)", " (đến 2026Q4)", " (2027Q1–2027Q2)", " (đã hết 2026Q4)"."""
    f, u = m.get("from"), m.get("until")
    name = lambda p: period_name(p, months)  # noqa: E731
    late = bool(f) and f > cycle_start(period, months)
    if u and u < period:
        return t(lang, "window.ended", until=name(u))
    if late and u:
        return f" ({name(f)}–{name(u)})" if name(f) != name(u) else f" ({name(f)})"
    if late:
        return t(lang, "window.from", start=name(f))
    return t(lang, "window.until", until=name(u)) if u else ""


def member_names(lang: str, p: dict) -> str:
    """Members grouped by their periods: "An, Bình · Cường, Dũng (từ 12/2026)" instead of a label per name."""
    groups: dict[str, list[str]] = {}
    for m in p["members"]:
        groups.setdefault(window_text(lang, m, p["months"], p["period"]), []).append(esc(m["name"]))
    return " · ".join(", ".join(names) + label for label, names in groups.items())


def plans_text(lang: str, v: dict) -> str:
    lines = [t(lang, "plans.title"),
             (t(lang, "plans.default", fee=fee_text(lang, v["fee"], v.get("fee_months", 1)), n=len(v["default_members"]))
              if v["fee"]
              else t(lang, "plans.default_none", n=len(v["default_members"])))
             + "\n   " + terms_text(lang, v["default"])]
    for p in v["plans"]:
        fee = fee_text(lang, p["amount"], p["months"])
        lines.append((t(lang, "plans.line", fee=fee, names=member_names(lang, p)) if p["members"]
                      else t(lang, "plans.empty_line", fee=fee)) + "\n   " + terms_text(lang, p))
    if not v["plans"]:
        lines.append(t(lang, "plans.none"))
    return "\n".join(lines) + "\n\n" + t(lang, "plans.help")


def plan_view(lang: str, p: dict):
    fee = fee_text(lang, p["amount"], p["months"])
    text = t(lang, "plans.view", fee=fee, n=len(p["members"]),
             names=member_names(lang, p) or "—") + "\n" + terms_text(lang, p)
    i = p["id"]
    return text, kb([(t(lang, "btn.plan_members"), f"p:m:{i}"), (t(lang, "btn.plan_amount"), f"p:a:{i}")],
                    [(t(lang, "btn.plan_window"), f"p:w:{i}")] if p["members"] else [],
                    [(t(lang, "btn.plan_due"), f"p:ds:{i}"), (t(lang, "btn.plan_late"), f"p:dl:{i}")],
                    [(t(lang, "btn.plan_cycle"), f"p:k:{i}"), (t(lang, "btn.plan_delete"), f"p:d:{i}")],
                    [(t(lang, "btn.back"), "p:l")])


def default_view(lang: str, v: dict):
    """The default package: the team fee on its cycle (monthly unless set), for everyone not on a plan, and its terms."""
    d = v["default"]
    fee = fee_text(lang, v["fee"], v.get("fee_months", 1)) if v["fee"] else t(lang, "settings.not_set")
    text = t(lang, "plans.default_view", fee=fee, n=len(v["default_members"])) + "\n" + terms_text(lang, d)
    return text, kb([(t(lang, "btn.plan_amount"), "p:fa"), (t(lang, "btn.plan_cycle"), "p:fk")],
                    [(t(lang, "btn.plan_due"), "p:ds:0"), (t(lang, "btn.plan_late"), "p:dl:0")],
                    [(t(lang, "btn.back"), "p:l")])


def cycle_keyboard(lang: str, prefix: str, back: str):
    return kb([(t(lang, "cycle.1").capitalize(), f"{prefix}1"), (t(lang, "cycle.3").capitalize(), f"{prefix}3")],
              [(t(lang, "cycle.6").capitalize(), f"{prefix}6"), (t(lang, "cycle.12").capitalize(), f"{prefix}12")],
              [(t(lang, "btn.cancel"), back)])


def window_choices(p: dict) -> dict[str, tuple[str, str | None]]:
    """The quick choices for a plan's months, as (from, until): from next period (the usual for newcomers)
    or this period, each for good. Other ranges are picked period by period."""
    start = cycle_start(p["period"], p["months"])
    return {"next": (add_months(start, p["months"]), None), "cur": (start, None),
            "only": (start, add_months(start, p["months"] - 1))}


async def open_window_menu(ev, lang, p: dict, members: list[dict], edit_in_place: bool = False,
                           member_id: int | None = None) -> None:
    """Ask for how long these members are on plan p. From a member card (member_id), the answer goes back
    to that member's packages."""
    if not members:
        return
    c = window_choices(p)
    st = {"plan_id": p["id"], "ids": [m["id"] for m in members]}
    if member_id:
        st["member_id"] = member_id
    set_state(window_key(ev.bot.id, ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id), st, 600)
    cur = period_name(c["cur"][0], p["months"])
    rows = [[(t(lang, "btn.window_next", period=period_name(c["next"][0], p["months"])), "w:next")],
            [(t(lang, "btn.window_cur", period=cur), "w:cur")],
            [(t(lang, "btn.window_only", period=cur), "w:only")]]
    rows += [[(t(lang, "btn.window_in"), "w:in")], [(t(lang, "btn.window_keep"), "w:x")]]
    text = t(lang, "window.ask", fee=fee_text(lang, p["amount"], p["months"]),
             names=", ".join(esc(m["name"]) for m in members), cur=cur)
    if edit_in_place:
        await edit(ev, text, kb(*rows))
    else:
        await say(ev, text, reply_markup=kb(*rows))


def clash_text(lang: str, clashes: list[dict]) -> str:
    """"⚠️ Overlaps another package: • Khải: 600.000đ/quý (2026Q4 → không thời hạn)"."""
    lines = [t(lang, "plans.overlap")]
    for c in clashes:
        n = c["months"]
        a = period_name(c["from"], n) if c.get("from") else "…"
        b = period_name(c["until"], n) if c.get("until") else t(lang, "window.open_end")
        lines.append(f"• {esc(c['member'])}: {fee_text(lang, c['amount'], n)} ({a} → {b})")
    return "\n".join(lines)


async def apply_window(ev, entry, lang, st: dict, start: str, until: str | None, edit_in_place: bool) -> None:
    try:
        p = await call(S.plans.set_window, tid(entry), ev.from_user.id, st["plan_id"], st["ids"], start, until)
    except DomainError as e:
        if e.code != "plan_overlap":
            raise
        # say which plans clash, then ask again for the same members
        plan = await call(S.plans.get_plan, tid(entry), ev.from_user.id, st["plan_id"])
        names = {c["member_id"]: c["member"] for c in e.extra["clashes"]}
        names.update({m["id"]: m["name"] for m in plan["members"]})
        await say(ev, clash_text(lang, e.extra["clashes"]) + "\n\n" + t(lang, "plans.overlap_hint"))
        await open_window_menu(ev, lang, plan, [{"id": i, "name": names.get(i, "?")} for i in st["ids"]],
                               member_id=st.get("member_id"))
        return
    n = p["months"]
    a, b = period_name(start, n), until and period_name(until, n)
    span = (a if a == b else f"{a}–{b}") if b else a + t(lang, "window.forever")
    if st.get("member_id"):
        await member_packs_view(ev, entry, lang, st["member_id"], note=t(lang, "window.saved", span=span),
                                edit_in_place=edit_in_place)
        return
    text, markup = plan_view(lang, p)
    out = f"{t(lang, 'window.saved', span=span)}\n\n{text}"
    if edit_in_place:
        await edit(ev, out, markup)
    else:
        await say(ev, out, reply_markup=markup)


# how many periods the range picker offers, per cycle (months, quarters, half-years, years)
RANGE_COUNT = {1: 12, 3: 8, 6: 6, 12: 4}
RANGE_ROW = {1: 4, 3: 4, 6: 3, 12: 4}


def range_rows(p: dict, first: str, prefix: str, end: bool) -> list:
    """Buttons for the plan's own periods from `first` on (quarters for a quarterly plan, months for a
    monthly one...). For an end, each button carries the period's last month."""
    n = p["months"]
    periods = [add_months(first, i * n) for i in range(RANGE_COUNT[n])]
    buttons = [(period_name(x, n), f"{prefix}{add_months(x, n - 1) if end else x}".replace("-", "")) for x in periods]
    return [buttons[i:i + RANGE_ROW[n]] for i in range(0, len(buttons), RANGE_ROW[n])]


@router.callback_query(F.data.regexp(r"^w:(next|cur|only|in|x|s\d{6}|e\d{6}|e0)$").as_("m"))
async def on_window(cq: CallbackQuery, m: re.Match, entry, lang):
    """The months menu: from next / this period, or a range picked period by period (start, then end)."""
    if not await allow(cq, entry, lang, "manager"):
        return
    key, choice = window_key(cq.bot.id, cq.message.chat.id), m.group(1)
    st = get_state(key)
    await cq.answer()
    if not st or choice == "x":
        del_state(key)
        await edit(cq, t(lang, "window.kept" if st else "err.menu_expired"))
        return
    p = await call(S.plans.get_plan, tid(entry), cq.from_user.id, st["plan_id"])
    n, cur = p["months"], cycle_start(p["period"], p["months"])
    inp = input_key(cq.bot.id, cq.message.chat.id)
    if choice == "in":  # pick the first period, or type the whole range in the reply box that opens
        set_state(inp, {"type": "plan_window", **st}, 600)
        await edit(cq, t(lang, "window.pick_start"),
                   kb(*range_rows(p, cur, "w:s", end=False), [(t(lang, "btn.cancel"), "w:x")]))
        a, b = (period_name(add_months(cur, k * n), n) for k in (1, 2))
        await say(cq, type_guide(lang, p),
                  reply_markup=ForceReply(force_reply=True, input_field_placeholder=f"{a} - {b}"))
        return
    if choice.startswith("s"):  # then the last one, or no end (typed or tapped)
        start = f"{choice[1:5]}-{choice[5:]}"
        set_state(key, {**st, "start": start}, 600)
        set_state(inp, {"type": "plan_window", **st, "start": start}, 600)
        example = period_name(add_months(start, n), n)
        await edit(cq, t(lang, "window.pick_end", start=period_name(start, n), example=example),
                   kb([(t(lang, "btn.window_forever"), "w:e0")], *range_rows(p, start, "w:e", end=True),
                      [(t(lang, "btn.cancel"), "w:x")]))
        return
    if choice.startswith("e"):
        if not st.get("start"):
            await edit(cq, t(lang, "err.menu_expired"))
            return
        start, until = st["start"], (None if choice == "e0" else f"{choice[1:5]}-{choice[5:]}")
    else:
        start, until = window_choices(p)[choice]
    del_state(key)
    del_state(inp)
    await apply_window(cq, entry, lang, st, start, until, edit_in_place=True)


def type_guide(lang: str, p: dict) -> str:
    """How to type a range, with examples in the plan's own cycle."""
    n, cur = p["months"], cycle_start(p["period"], p["months"])
    a, b = period_name(add_months(cur, n), n), period_name(add_months(cur, 2 * n), n)
    return t(lang, f"window.type_{n}", a=a, b=b, cur=period_name(cur, n))


async def plan_window_reply(message: Message, entry, lang, st: dict, text: str) -> None:
    """A typed range ("2027Q1 - 2027Q2", "2027Q1", "- 2027Q2"), or, once the start is picked, the last period."""
    p = await call(S.plans.get_plan, tid(entry), message.from_user.id, st["plan_id"])
    n, cur = p["months"], cycle_start(p["period"], p["months"])
    w = parse_periods(text, n, cur)
    if w and st.get("start") and w[1] is None and "-" not in text and "–" not in text:
        w = (st["start"], period_end(w[0], n)) if w[0] >= st["start"] else None  # just the last period
    if w is None or w[0] < cur:
        set_state(input_key(message.bot.id, message.chat.id), st, 600)
        await message.answer(t(lang, "err.invalid") + "\n\n" + type_guide(lang, p), reply_markup=FORCE)
        return
    del_state(window_key(message.bot.id, message.chat.id))
    await apply_window(message, entry, lang, {"plan_id": st["plan_id"], "ids": st["ids"],
                                              "member_id": st.get("member_id")}, *w, edit_in_place=False)


async def plans_view(ev, entry, lang, edit_in_place: bool = False) -> None:
    v = await call(S.plans.list_plans, tid(entry), ev.from_user.id)
    rows = [[(f"✏️ {fee_text(lang, p['amount'], p['months'])}", f"p:v:{p['id']}")] for p in v["plans"]]
    rows.insert(0, [(t(lang, "btn.default_package"), "p:f")])
    rows.append([(t(lang, "btn.plan_new"), "p:n")])
    if edit_in_place:
        await edit(ev, plans_text(lang, v), kb(*rows))
    else:
        await say(ev, plans_text(lang, v), reply_markup=kb(*rows))


async def open_plan_picker(ev, entry, lang, amount: int, months: int, plan: dict | None = None) -> None:
    members = [m for m in await call(S.members.list_members, tid(entry), ev.from_user.id)]
    if not members:
        await say(ev, t(lang, "err.no_members"))
        return
    label = lambda m: f"{m['name']} · {fee_text(lang, m['fee'], m['months'])}" if m["fee"] else m["name"]  # noqa: E731
    await open_picker(ev, lang, {
        "kind": "plan", "title": t(lang, "plans.pick", fee=fee_text(lang, amount, months)),
        "options": [{"id": m["id"], "label": label(m)} for m in members],
        "selected": [m["id"] for m in plan["members"]] if plan else [],
        "data": {"amount": amount, "months": months, "plan_id": plan["id"] if plan else None,
                 "before": [m["id"] for m in plan["members"]] if plan else []}})


@router.message(Command("plans"))
async def cmd_plans(message: Message, command: CommandObject, entry, lang):
    """Fee plans, e.g. 150k a month for some and 300k a quarter for others. `/plans 300k quý` starts one."""
    if not await allow(message, entry, lang, "manager"):
        return
    args = (command.args or "").strip()
    if not args:
        await plans_view(message, entry, lang)
        return
    amount, rest = amount_and_title(args)
    months = parse_cycle(rest or "")
    if not amount or not months:
        await message.answer(t(lang, "usage.plans"))
        return
    await open_plan_picker(message, entry, lang, amount, months)


@router.callback_query(F.data.regexp(r"^p:(l|n|f|fa|fk|fkc|ds|dl|c|v|m|w|a|k|kc|d|dy)(?::(\d+))?(?::(\d+))?$").as_("m"))
async def on_plan(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    action, a, b = m.group(1), m.group(2), m.group(3)
    team, user = tid(entry), cq.from_user.id
    await cq.answer()
    if action == "l":
        await plans_view(cq, entry, lang, edit_in_place=True)
    elif action == "n":
        await edit(cq, t(lang, "plans.choose_cycle"), cycle_keyboard(lang, "p:c:", "p:l"))
    elif action == "f":  # the default package: the team fee and its terms
        await edit(cq, *default_view(lang, await call(S.plans.list_plans, team, user)))
    elif action == "fk":  # the default package's cycle
        await edit(cq, t(lang, "plans.default_cycle"), cycle_keyboard(lang, "p:fkc:", "p:f"))
    elif action == "fkc" and a:
        await call(S.teams.update_settings, team, user, fee_months=int(a))
        text, markup = default_view(lang, await call(S.plans.list_plans, team, user))
        await edit(cq, f"{t(lang, 'plans.saved')}\n\n{text}", markup)
    elif action == "fa":
        set_state(input_key(cq.bot.id, cq.message.chat.id), {"type": "default_fee"}, 600)
        await say(cq, t(lang, "plans.ask_default"), reply_markup=FORCE)
    elif action in ("ds", "dl") and a is not None:  # a package's deadline / late fee; 0 is the default package
        pid = int(a) or None
        set_state(input_key(cq.bot.id, cq.message.chat.id),
                  {"type": "plan_schedule" if action == "ds" else "plan_late", "plan_id": pid}, 600)
        inherit = t(lang, "plans.ask_inherit") if pid else ""
        await say(cq, t(lang, "plans.ask_schedule" if action == "ds" else "plans.ask_late") + inherit,
                  reply_markup=FORCE)
    elif action == "c" and a:
        set_state(input_key(cq.bot.id, cq.message.chat.id), {"type": "plan_amount", "months": int(a)}, 600)
        await say(cq, t(lang, "plans.ask_amount", cycle=t(lang, f"cycle.{a}")), reply_markup=FORCE)
    elif action == "v" and a:
        await edit(cq, *plan_view(lang, await call(S.plans.get_plan, team, user, int(a))))
    elif action == "m" and a:
        p = await call(S.plans.get_plan, team, user, int(a))
        await open_plan_picker(cq, entry, lang, p["amount"], p["months"], p)
    elif action == "w" and a:  # how long some of the plan's members are on it
        p = await call(S.plans.get_plan, team, user, int(a))
        await open_picker(cq, lang, {
            "kind": "window", "title": t(lang, "window.pick", fee=fee_text(lang, p["amount"], p["months"])),
            "options": [{"id": x["id"], "label": x["name"] + window_text(lang, x, p["months"], p["period"])}
                        for x in p["members"]],
            "selected": [], "data": {"plan_id": p["id"]}})
    elif action == "a" and a:
        p = await call(S.plans.get_plan, team, user, int(a))
        set_state(input_key(cq.bot.id, cq.message.chat.id),
                  {"type": "plan_amount", "months": p["months"], "plan_id": p["id"]}, 600)
        await say(cq, t(lang, "plans.ask_amount", cycle=t(lang, f"cycle.{p['months']}")), reply_markup=FORCE)
    elif action == "k" and a:
        p = await call(S.plans.get_plan, team, user, int(a))
        await edit(cq, t(lang, "plans.change_cycle", fee=fee_text(lang, p["amount"], p["months"])),
                   cycle_keyboard(lang, f"p:kc:{a}:", f"p:v:{a}"))
    elif action == "kc" and a and b:
        p = await call(S.plans.get_plan, team, user, int(a))
        p = await call(S.plans.save_plan, team, user, p["amount"], int(b), [x["id"] for x in p["members"]], p["id"])
        text, markup = plan_view(lang, p)
        await edit(cq, f"{t(lang, 'plans.saved')}\n\n{text}", markup)
    elif action == "d" and a:
        p = await call(S.plans.get_plan, team, user, int(a))
        await edit(cq, t(lang, "plans.confirm_delete", fee=fee_text(lang, p["amount"], p["months"]),
                         n=len(p["members"])),
                   kb([(t(lang, "btn.yes_delete"), f"p:dy:{a}"), (t(lang, "btn.cancel"), f"p:v:{a}")]))
    elif action == "dy" and a:
        r = await call(S.plans.delete_plan, team, user, int(a))
        await edit(cq, t(lang, "plans.deleted", fee=fee_text(lang, r["amount"], r["months"]), n=len(r["members"])),
                   kb([(t(lang, "btn.back"), "p:l")]))


async def plan_terms_reply(message: Message, entry, lang, st: dict, text: str) -> None:
    """A package's deadline ("15 5") or late fee ("1k", "0"); "-" puts a plan back on the default's."""
    pid, schedule = st.get("plan_id"), st["type"] == "plan_schedule"
    kw = {}
    if text == "-" and pid:
        kw = {"inherit_schedule": True} if schedule else {"inherit_late": True}
    elif schedule:
        parts = text.split()
        if len(parts) == 2 and all(x.isdigit() for x in parts) and 1 <= int(parts[0]) <= 28 and int(parts[1]) <= 60:
            kw = {"schedule": (int(parts[0]), int(parts[1]))}
    else:
        fee = 0 if text == "0" else parse_amount(text)
        if fee is not None:
            kw = {"late_fee": fee}
    if not kw:
        set_state(input_key(message.bot.id, message.chat.id), st, 600)
        await message.answer(t(lang, "err.invalid") + "\n\n" + t(lang, "plans.ask_schedule" if schedule else "plans.ask_late"),
                             reply_markup=FORCE)
        return
    await call(S.plans.set_terms, tid(entry), message.from_user.id, pid, **kw)
    v = await call(S.plans.list_plans, tid(entry), message.from_user.id)
    if pid:
        text, markup = plan_view(lang, next(p for p in v["plans"] if p["id"] == pid))
    else:
        text, markup = default_view(lang, v)
    await message.answer(f"{t(lang, 'plans.saved_terms')}\n\n{text}", reply_markup=markup)


async def plan_amount_reply(message: Message, entry, lang, st: dict, text: str) -> None:
    amount = parse_amount(text)
    if not amount:
        await message.answer(t(lang, "err.invalid"))
        return
    if st.get("plan_id"):
        p = await call(S.plans.get_plan, tid(entry), message.from_user.id, st["plan_id"])
        p = await call(S.plans.save_plan, tid(entry), message.from_user.id, amount, p["months"],
                       [x["id"] for x in p["members"]], p["id"])
        view, markup = plan_view(lang, p)
        await message.answer(f"{t(lang, 'plans.saved')}\n\n{view}", reply_markup=markup)
        return
    await open_plan_picker(message, entry, lang, amount, st["months"])


@router.message(Command("remind"))
async def cmd_remind(message: Message, entry, lang):
    """Send who still owes to everyone following the team bot."""
    if not await allow(message, entry, lang, "manager"):
        return
    G = group_lang(entry)
    text = status_text(G, await call(S.money.status, tid(entry)))
    n = await broadcast(entry, text, reply_markup=pay_keyboard(G, entry.username))
    await message.answer(t(lang, "remind.sent", n=n))


def action_text(lang: str, v: dict) -> str:
    """"Ghi khoản chi · Sân bóng · 300.000đ"."""
    key = f"act.{v['action']}"
    parts = [t(lang, key) if has(key) else v["action"]]
    if v.get("target"):
        parts.append(esc(v["target"]))
    if isinstance(v.get("amount"), int):
        parts.append(money(v["amount"], lang))
    return " · ".join(parts)


async def show_undo(ev, entry, lang, edit_in_place: bool = False, prefix: str = "") -> None:
    """Recent actions this person can undo, one button each."""
    items = await call(S.undo.recent, tid(entry), ev.from_user.id)
    if items:
        text = prefix + t(lang, "undo.pick")
        markup = kb(*[[(f"{action_text(lang, v)} · {v['at'][:5]}"[:60], f"un:{v['id']}")] for v in items],
                    [(t(lang, "btn.cancel"), "un:x")])
    else:
        text, markup = prefix + t(lang, "undo.nothing"), None
    if edit_in_place:
        await edit(ev, text, markup)
    else:
        await say(ev, text, reply_markup=markup)


@router.message(Command("undo"))
async def cmd_undo(message: Message, entry, lang):
    """Pick a recent action and take it back."""
    if await allow(message, entry, lang, "manager"):
        await show_undo(message, entry, lang)


@router.callback_query(F.data.regexp(r"^un:(x|l|\d+|y:\d+)$").as_("m"))
async def on_undo(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    arg = m.group(1)
    if arg == "x":
        await cq.answer()
        await edit(cq, t(lang, "cancelled"))
    elif arg == "l":
        await cq.answer()
        await show_undo(cq, entry, lang, edit_in_place=True)
    elif arg.isdigit():  # confirm first
        v = await call(S.undo.get, tid(entry), cq.from_user.id, int(arg))
        await cq.answer()
        await edit(cq, t(lang, "undo.ask", action=action_text(lang, v), actor=esc(v["actor"]), at=v["at"]),
                   kb([(t(lang, "btn.undo"), f"un:y:{v['id']}"), (t(lang, "btn.back"), "un:l")]))
    else:
        r = await call(S.undo.undo, tid(entry), cq.from_user.id, int(arg[2:]))  # a refusal shows as an alert
        await cq.answer()
        await edit(cq, t(lang, "undo.done", action=action_text(lang, r)), kb([(t(lang, "btn.undo_more"), "un:l")]))
        if r["announce"]:
            G = group_lang(entry)
            await broadcast(entry, t(G, "group.undone", action=action_text(G, r), by=esc(r["by"])))


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
    if s["bank_account"] and s["pay_account"] != s["bank_account"]:
        bank += f" · VA {esc(s['pay_account'])}"
    await message.answer(t(lang, "settings.text", team=esc(s["name"]), fee=money(s["fee"], lang) if s["fee"] else none,
                           due=t(lang, "settings.due", notify=s["notify_day"], days=s["due_days"])
                           + (" · " + t(lang, "latefee.on", fee=money(s["late_fee"], lang)) if s["late_fee"] else ""),
                           bank=bank, sepay="✅" if s["sepay_connected"] else none,
                           group=s["followers"],
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


@router.message(Command("weblink"))
async def cmd_weblink(message: Message, entry, lang):
    """The pay page link. Members see it (if a manager has made one); only managers make or change it."""
    if not is_private(message):
        return
    if not await role(entry, message.from_user):
        url = await call(S.teams.public_link, tid(entry))
        await message.answer(t(lang, "weblink.text", url=esc(url)) if url else t(lang, "weblink.none"),
                             disable_web_page_preview=True)
        return
    r = await call(S.teams.web_link, tid(entry), message.from_user.id)
    await message.answer(t(lang, "weblink.text", url=esc(r["url"])), disable_web_page_preview=True,
                         reply_markup=kb([(t(lang, "btn.weblink_reset"), "w:reset")]))


@router.callback_query(F.data.in_({"w:reset", "w:reset!"}))
async def on_weblink_reset(cq: CallbackQuery, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    await cq.answer()
    if cq.data == "w:reset":  # ask first: everyone with the old link loses it
        await say(cq, t(lang, "weblink.confirm"), reply_markup=kb([(t(lang, "btn.weblink_reset_yes"), "w:reset!")]))
        return
    r = await call(S.teams.web_link, tid(entry), cq.from_user.id, True)
    await say(cq, t(lang, "weblink.reset_done", url=esc(r["url"])))


# ---- commands: owner --------------------------------------------------------------------------------

@router.message(Command("setbank"))
async def cmd_setbank(message: Message, command: CommandObject, entry, lang):
    """Pick the team's bank account from the owner's SePay. Typing an account number is not supported:
    only an account SePay watches can confirm payments."""
    if not await allow(message, entry, lang, "owner"):
        return
    if (command.args or "").strip():
        await message.answer(t(lang, "setbank.no_typing"))
    await show_bank_picker(message, entry, lang, message.from_user.id)


async def show_bank_picker(msg: Message, entry, lang, actor: int):
    info = await call(S.teams.sepay_info, tid(entry), actor)
    if not info["token_set"]:
        await msg.answer(t(lang, "setbank.need_token"))
        return
    wait = await msg.answer(t(lang, "setbank.loading"))
    try:
        options = await call(S.teams.bank_options, tid(entry), actor)
    except DomainError as e:
        await wait.edit_text(error_text(lang, e))
        return
    if not options:
        await wait.edit_text(t(lang, "setbank.none"), disable_web_page_preview=True)
        return
    rows = [[(bank_option_label(lang, o), f"b:{i}")] for i, o in enumerate(options[:30])]
    rows.append([(t(lang, "btn.cancel"), "b:x")])
    set_state(pick_key(msg.bot.id, msg.chat.id, wait.message_id), {"type": "bank", "options": options[:30]}, 600)
    await wait.edit_text(t(lang, "setbank.pick"), reply_markup=kb(*rows), disable_web_page_preview=True)


def bank_option_label(lang: str, o: dict) -> str:
    last4 = "…" + o["account"][-4:]
    if o["needs_va"]:
        label = t(lang, "setbank.opt_needs_va", bank=o["bank"], account=last4)
    elif o["va"]:
        label = t(lang, "setbank.opt_va", bank=o["bank"], va=o["va"], account=last4)
    else:
        label = f"{o['bank']} · {last4} · {o['holder']}"
    return ("✅ " if o["current"] else "") + label


@router.callback_query(F.data.regexp(r"^b:(x|\d+)$").as_("m"))
async def on_bank_pick(cq: CallbackQuery, m, entry, lang):
    if not await allow(cq, entry, lang, "owner"):
        return
    key = pick_key(cq.bot.id, cq.message.chat.id, cq.message.message_id)
    st = get_state(key)
    if m.group(1) == "x" or not st or st.get("type") != "bank" or int(m.group(1)) >= len(st["options"]):
        del_state(key)
        await cq.answer(t(lang, "err.menu_expired") if m.group(1) != "x" else "")
        await edit(cq, t(lang, "setbank.cancelled") if m.group(1) == "x" else t(lang, "err.menu_expired"))
        return
    o = st["options"][int(m.group(1))]
    await cq.answer()
    if o["needs_va"]:  # keep the picker open: the owner creates a VA in SePay, then sends /setbank again
        await say(cq, t(lang, "err.sepay_va_missing", bank=esc(o["bank"]), account=esc(o["account"])),
                  disable_web_page_preview=True)
        return
    del_state(key)
    await apply_bank(cq, entry, lang, o)


async def apply_bank(cq: CallbackQuery, entry, lang, o: dict):
    """Run the choice and show every action as it happens, in the picker message itself."""
    choice = t(lang, "setbank.opt_va", bank=o["bank"], va=o["va"], account="…" + o["account"][-4:]) if o["va"] \
        else f"{o['bank']} · …{o['account'][-4:]}"
    head = f"🏦 <b>{esc(choice)}</b>"
    lines: list[str] = []
    now = {"text": t(lang, "setbank.working")}  # the step in progress
    loop, lock, pending = asyncio.get_running_loop(), asyncio.Lock(), []

    async def show(tail: str):
        async with lock:  # one edit at a time, always showing the latest lines
            await edit(cq, "\n".join([head, *lines, tail]))

    def progress(step: str, info: dict):  # called from the worker thread as each step starts and finishes
        if step.startswith("start:"):
            now["text"] = step_text(lang, step.replace(":", "_"), info)
        else:
            lines.append("✅ " + step_text(lang, step, info))
            now["text"] = t(lang, "setbank.working")
        pending.append(asyncio.run_coroutine_threadsafe(show("⏳ " + now["text"]), loop))

    async def settle():  # let the live edits finish, so they can't overwrite the final message
        await asyncio.gather(*(asyncio.wrap_future(f) for f in pending), return_exceptions=True)

    await show("⏳ " + now["text"])
    try:
        r = await call(S.teams.choose_bank, tid(entry), cq.from_user.id, o["xid"], o["va"], progress)
    except DomainError as e:
        await settle()
        await show(f"❌ {now['text']}\n{error_text(lang, e)}\n\n" + t(lang, "setbank.nothing_saved"))
        return
    await settle()
    if r["changed"] and await broadcast(entry, t(group_lang(entry), "group.bank_changed", by=esc(r["by"]),
                                                     bank=esc(r["bank"]), account=esc(r["va"] or r["account"]),
                                                     holder=esc(r["holder"]))):
        lines.append("✅ " + t(lang, "step.group"))
    tail = t(lang, "setbank.done", account=esc(r["va"] or r["account"]))
    if r["va"]:
        tail += "\n" + t(lang, "setbank.va_note", bank=esc(r["bank"]), account=esc(r["account"]))
    await show("\n" + tail)


def step_text(lang: str, step: str, info: dict) -> str:
    if step == "account":
        return t(lang, "step.account_va" if info["va_only"] else "step.account", bank=esc(info["bank"]),
                 account=esc("…" + info["account"][-4:]))
    if step == "webhook":
        return t(lang, f"step.webhook_{info['action']}", name=esc(info["name"]), url=esc(info["url"]))
    return t(lang, f"step.{step}", **{k: esc(str(v)) for k, v in info.items()})


@router.message(Command("sepaytoken"))
async def cmd_sepaytoken(message: Message, command: CommandObject, entry, lang):
    """/sepaytoken <SePay API token>: save the owner's token, then pick the account. No token: how to get one."""
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


async def save_sepay_token(message: Message, entry, lang, text: str, delete: bool = True):
    if delete:
        try:  # the token gives access to the SePay account: don't leave it in the chat
            await message.delete()
        except Exception:
            pass
    r = await call(S.teams.set_sepay_token, tid(entry), message.from_user.id, text)
    await message.answer(t(lang, "sepay.token_saved", n=r["bank_accounts"]))
    await show_bank_picker(message, entry, lang, message.from_user.id)


@router.message(Command("requests"))
async def cmd_requests(message: Message, entry, lang):
    if is_private(message) and await role(entry, message.from_user) != "owner":
        # easy to mix up with /requestmanager: explain, and offer to send the request from here
        await message.answer(t(lang, "mr.wrong_command"), reply_markup=kb([(t(lang, "btn.ask_manager"), "mr:ask")]))
        return
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
             f"{' ' + esc(r['target']) if r['target'] else ''}{' ↩️' if r['undone'] else ''}" for r in rows]
    await message.answer(t(lang, "log.title") + "\n" + "\n".join(lines))


# ---- replies to "send me ..." prompts, group joins, stale buttons ---------------------------------------

@router.message(F.text)
async def on_text(message: Message, entry, lang):
    if not is_private(message) or message.text.startswith("/"):
        return
    key = input_key(message.bot.id, message.chat.id)
    st = get_state(key)
    if not st:  # a months menu is open: typed periods still count
        win = get_state(window_key(message.bot.id, message.chat.id))
        st = {"type": "plan_window", **win} if win else None
    if not st:
        return
    del_state(key)
    if st["type"] == "donate":  # open to everyone
        amount = parse_amount(message.text.strip())
        if not amount:
            set_state(key, st, 600)
            await message.answer(t(lang, "err.donation_amount"))
            return
        await send_donation_qr(message, entry, lang, amount)
        return
    if not await allow(message, entry, lang, "manager"):
        return
    text = message.text.strip()
    if st["type"] == "sepay_token":
        await save_sepay_token(message, entry, lang, text)
    elif st["type"] in ("expense", "income"):
        await record_money(message, entry, lang, st["type"], text)
    elif st["type"] == "member_pack":
        await member_pack_reply(message, entry, lang, st, text)
    elif st["type"] == "cash_amount":
        await cash_amount_reply(message, entry, lang, st, text)
    elif st["type"] == "default_fee":
        fee = 0 if text == "0" else parse_amount(text)
        if fee is None:
            await message.answer(t(lang, "err.invalid"))
            return
        r = await call(S.teams.update_settings, tid(entry), message.from_user.id, fee=fee)
        await message.answer(t(lang, "setfee.done", fee=fee_text(lang, r["fee"], r["fee_months"])))
        await plans_view(message, entry, lang)
    elif st["type"] in ("plan_schedule", "plan_late"):
        await plan_terms_reply(message, entry, lang, st, text)
    elif st["type"] == "plan_amount":
        await plan_amount_reply(message, entry, lang, st, text)
    elif st["type"] == "plan_window":
        await plan_window_reply(message, entry, lang, st, text)
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


@router.callback_query()
async def on_stale_button(cq: CallbackQuery):
    await cq.answer()
