"""/matches (the team's match calendar) and /contacts (opposing teams), in the team bot.

Everyone can see the matches; managers add, change and cancel them, set repeating calendars and keep the
contacts. Nothing here touches money."""
import re
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ForceReply, Message

from ..deps import S
from .fmt import esc, parse_contact, parse_match, parse_phone, show_phone
from .i18n import t
from .render import edit, is_private, kb, say
from .run import call
from .state import del_state, get_state, set_state
from .team import FORCE, allow, broadcast, group_lang, input_key, tid

router = Router()
WD = {"vi": ["T2", "T3", "T4", "T5", "T6", "T7", "CN"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}
REASONS = ("rain", "opp", "short", "holiday", "pitch")
PASTE_KEY = "mtp:{}:{}"


def _wd(lang: str, i: int) -> str:
    return WD.get(lang, WD["en"])[i]


def _iso(d: date) -> str:
    return d.strftime("%Y%m%d")


def _d(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def day_label(lang: str, d: date) -> str:
    return f"{_wd(lang, d.weekday())} {d:%d/%m}"


def match_line(lang: str, x: dict) -> str:
    """"CN 30/11 19:00 · FC Moreh · @Sân A · 📝 áo trắng", struck through with the reason if cancelled."""
    d = date.fromisoformat(x["day"]) if isinstance(x["day"], str) else x["day"]
    parts = [day_label(lang, d) + (f" {x['at']}" if x.get("at") else ""),
             esc(x["opponent"]) if x.get("opponent") else t(lang, "mt.no_opp")]
    if x.get("place"):
        parts.append("@" + esc(x["place"]))
    if x.get("note"):
        parts.append("📝 " + esc(x["note"]))
    line = " · ".join(parts)
    if x.get("status") == "cancelled":
        line = f"<s>{line}</s> 🚫" + (f" {esc(x['reason'])}" if x.get("reason") else "")
    return line


def schedule_line(lang: str, s: dict) -> str:
    if s["every"] == "monthly" and s.get("mday"):
        when = t(lang, "mt.every_mday", day=s["mday"])
    elif s["every"] == "monthly":
        nth = t(lang, "mt.nth_last") if s["nth"] == -1 else t(lang, "mt.nth", n=s["nth"])
        when = t(lang, "mt.every_month", nth=nth, wd=t(lang, f"mt.wd{s['weekday']}"))
    else:
        when = t(lang, f"mt.every_{s['every']}", wd=t(lang, f"mt.wd{s['weekday']}"))
    extra = "".join([f", {s['at']}" if s.get("at") else "", f" @{esc(s['place'])}" if s.get("place") else ""])
    return f"🔁 {when}{extra}"


def list_view(lang: str, v: dict):
    lines = [t(lang, "mt.title", n=v["limit"])]
    lines += [schedule_line(lang, s) for s in v["schedules"]]
    lines += ["• " + match_line(lang, x) for x in v["matches"]] or [t(lang, "mt.empty")]
    n = v["limit"]
    more = [(t(lang, "mt.show", n=k), f"mt:l:{k}") for k in (n + 5, n + 10)] + [(t(lang, "mt.show_all"), "mt:l:200")] \
        if v["more"] else []
    rows = [more]
    if v["can_edit"]:
        rows += [[(t(lang, "mt.btn_add"), "mt:a"), (t(lang, "mt.btn_sched"), "mt:s")],
                 [(t(lang, "mt.btn_edit"), f"mt:e:{n}"), (t(lang, "mt.btn_past"), "mt:p")]]
    else:
        rows.append([(t(lang, "mt.btn_past"), "mt:p")])
    if v["can_edit"]:
        lines.append("\n" + t(lang, "mt.help"))
    return "\n".join(lines), kb(*rows)


async def show_list(ev, entry, lang, limit: int = 5, edit_in_place: bool = False) -> None:
    v = await call(S.matches.show, tid(entry), ev.from_user.id, limit)
    text, markup = list_view(lang, v)
    await (edit(ev, text, markup) if edit_in_place else say(ev, text, reply_markup=markup))


def match_view(lang: str, x: dict):
    lines = [t(lang, "mt.view"), match_line(lang, x)]
    if x.get("schedule_id"):
        lines.append(t(lang, "mt.from_sched"))
    i = x["id"]
    cancel = (t(lang, "mt.btn_restore"), f"mt:r:{i}") if x["status"] == "cancelled" else (t(lang, "mt.btn_cancel"), f"mt:c:{i}")
    contact = [(t(lang, "mt.btn_contact"), f"ct:v:{x['contact_id']}")] if x.get("contact_id") else (
        [(t(lang, "mt.btn_save_contact"), f"mt:sc:{i}")] if x.get("opponent") else [])
    return "\n".join(lines), kb([(t(lang, "mt.btn_when"), f"mt:ed:{i}"), (t(lang, "mt.btn_opp"), f"mt:eo:{i}")],
                                [(t(lang, "mt.btn_note"), f"mt:en:{i}"), cancel], contact,
                                [(t(lang, "mt.btn_delete"), f"mt:d:{i}"), (t(lang, "btn.back"), "mt:l:5")])


async def show_match(ev, entry, lang, match_id: int, prefix: str = "", edit_in_place: bool = True) -> None:
    x = await call(S.matches.get, tid(entry), ev.from_user.id, match_id)
    text, markup = match_view(lang, x)
    text = prefix + text
    await (edit(ev, text, markup) if edit_in_place else say(ev, text, reply_markup=markup))


def _chat(ev) -> int:
    return ev.message.chat.id if isinstance(ev, CallbackQuery) else ev.chat.id


def ikey(ev) -> str:
    return input_key(ev.bot.id, _chat(ev))


def ask(ev, lang, st: dict, text: str, placeholder: str | None = None):
    set_state(ikey(ev), st, 900)
    return say(ev, text, reply_markup=ForceReply(force_reply=True, input_field_placeholder=placeholder)
               if placeholder else FORCE)


# ---- /matches ---------------------------------------------------------------------------------------

@router.message(Command("matches"))
async def cmd_matches(message: Message, command: CommandObject, entry, lang):
    """The next 5 matches; `/matches 10`; managers paste lines to add or `/matches cancel 30/11 mưa`."""
    if not is_private(message):
        return
    args = (command.args or "").strip()
    if not args or args.isdigit():
        await show_list(message, entry, lang, int(args) if args else 5)
        return
    if not await allow(message, entry, lang, "manager"):
        return
    today = date.today()
    m = re.match(r"(?is)^(cancel|huỷ|huy|hủy)\s+(.+)$", args)
    if m:
        p = parse_match(m.group(2), today)
        if not p:
            await message.answer(t(lang, "mt.usage"))
            return
        reason = " ".join(x for x in (p["opponent"], p["note"]) if x) or None
        x = await call(S.matches.cancel_on, tid(entry), message.from_user.id, p["day"], reason)
        await announce_cancel(entry, x)
        await show_match(message, entry, lang, x["id"], t(lang, "mt.cancelled") + "\n\n", edit_in_place=False)
        return
    await paste_preview(message, entry, lang, args)


async def paste_preview(message: Message, entry, lang, text: str) -> None:
    today = date.today()
    good, bad = [], []
    for line in [x.strip().lstrip("-–•* ") for x in text.splitlines()]:
        if not line:
            continue
        p = parse_match(line, today)
        (good if p else bad).append(p or line)
    if not good:
        await message.answer(t(lang, "mt.usage"))
        return
    set_state(PASTE_KEY.format(message.bot.id, message.chat.id),
              [{**p, "day": p["day"].isoformat()} for p in good], 900)
    lines = [t(lang, "mt.read", n=len(good))] + ["• " + match_line(lang, p) for p in good]
    if bad:
        lines.append(t(lang, "mt.skipped", lines="\n".join("  " + esc(b) for b in bad[:10])))
    await message.answer("\n".join(lines), reply_markup=kb([(t(lang, "mt.btn_save"), "mt:ok"),
                                                            (t(lang, "btn.cancel"), "mt:x")]))


async def announce_cancel(entry, x: dict, restored: bool = False) -> None:
    G = group_lang(entry)
    line = match_line(G, {**x, "status": "scheduled"})
    if restored:
        await broadcast(entry, t(G, "mt.ann_restored", match=line))
    else:
        why = t(G, "mt.ann_reason", reason=esc(x["reason"])) if x.get("reason") else ""
        await broadcast(entry, t(G, "mt.ann_cancelled", match=line) + why)


def reasons_kb(lang: str, i: int):
    r = [(t(lang, f"mt.reason_{k}"), f"mt:cr:{i}:{k}") for k in REASONS]
    return kb(r[:3], r[3:], [(t(lang, "mt.reason_other"), f"mt:co:{i}"), (t(lang, "mt.reason_none"), f"mt:cr:{i}:none")],
              [(t(lang, "btn.back"), f"mt:v:{i}")])


async def opponent_kb(ev, entry, lang, prefix: str, back: str):
    contacts = await call(S.contacts.search, tid(entry), ev.from_user.id)
    btn = [(c["name"][:30], f"{prefix}{c['id']}") for c in contacts[:12]]
    rows = [btn[i:i + 2] for i in range(0, len(btn), 2)]
    return kb(*rows, [(t(lang, "mt.btn_no_opp"), f"{prefix}0")], [(t(lang, "btn.cancel"), back)])


@router.callback_query(F.data.regexp(r"^mt:(\w+)(?::(\w+))?(?::(\w+))?$").as_("m"))
async def on_matches(cq: CallbackQuery, m: re.Match, entry, lang):
    action, a, b = m.group(1), m.group(2), m.group(3)
    team, user = tid(entry), cq.from_user.id
    if action == "l":
        await cq.answer()
        await show_list(cq, entry, lang, int(a or 5), edit_in_place=True)
        return
    if action == "p":
        await cq.answer()
        past = await call(S.matches.past, team, user, 10)
        lines = [t(lang, "mt.past")] + ["• " + match_line(lang, x) for x in past] if past else [t(lang, "mt.past_empty")]
        await edit(cq, "\n".join(lines), kb([(t(lang, "btn.back"), "mt:l:5")]))
        return
    if not await allow(cq, entry, lang, "manager"):
        return
    await cq.answer()
    pk = PASTE_KEY.format(cq.bot.id, cq.message.chat.id)

    if action == "ok":
        items = get_state(pk)
        del_state(pk)
        if not items:
            await edit(cq, t(lang, "err.menu_expired"))
            return
        made = await call(S.matches.add, team, user, [{**x, "day": date.fromisoformat(x["day"])} for x in items])
        await edit(cq, t(lang, "mt.saved", n=len(made)))
        await show_list(cq, entry, lang)
    elif action == "x":
        del_state(pk)
        del_state(ikey(cq))
        await edit(cq, t(lang, "cancelled"))

    # add one match: day -> time and pitch -> opponent
    elif action == "a":
        days = await call(S.matches.suggest_days, team, user, 4)
        btn = [(day_label(lang, date.fromisoformat(d)), f"mt:ad:{d.replace('-', '')}") for d in days]
        set_state(ikey(cq), {"type": "mt_add_day"}, 900)
        await say(cq, t(lang, "mt.ask_day"), reply_markup=kb(btn[:2], btn[2:], [(t(lang, "btn.cancel"), "mt:x")]))
        await ask(cq, lang, {"type": "mt_add_day"}, t(lang, "mt.type_day"), "7/12")
    elif action == "ad" and a:
        await add_day(cq, entry, lang, _d(a))
    elif action == "at" and a:
        st = get_state(ikey(cq)) or {}
        if st.get("type") != "mt_add_time":
            await edit(cq, t(lang, "err.menu_expired"))
            return
        if a == "d":
            st.update(st.get("defaults") or {})
        await add_opponent(cq, entry, lang, st)
    elif action == "ao" and a is not None:
        st = get_state(ikey(cq)) or {}
        if st.get("type") != "mt_add_opp":
            await edit(cq, t(lang, "err.menu_expired"))
            return
        name = None
        if a != "0":
            c = next((c for c in await call(S.contacts.search, team, user) if c["id"] == int(a)), None)
            name = c["name"] if c else None
        await add_save(cq, entry, lang, st, name)

    # edit a match
    elif action == "e":
        v = await call(S.matches.show, team, user, int(a or 5))
        btn = [[(re.sub(r"<[^>]+>", "", match_line(lang, x))[:60], f"mt:v:{x['id']}")] for x in v["matches"]]
        await edit(cq, t(lang, "mt.pick_edit"), kb(*btn, [(t(lang, "btn.back"), "mt:l:5")]))
    elif action == "v" and a:
        await show_match(cq, entry, lang, int(a))
    elif action == "ed" and a:
        await ask(cq, lang, {"type": "mt_when", "id": int(a)}, t(lang, "mt.ask_when"), "7/12 19h @Sân A")
    elif action == "eo" and a:
        await edit(cq, t(lang, "mt.ask_opp"), await opponent_kb(cq, entry, lang, f"mt:eop:{a}:", f"mt:v:{a}"))
        await ask(cq, lang, {"type": "mt_opp", "id": int(a)}, t(lang, "mt.type_opp"), "FC Moreh")
    elif action == "eop" and a and b is not None:
        del_state(ikey(cq))
        name = None
        if b != "0":
            c = next((c for c in await call(S.contacts.search, team, user) if c["id"] == int(b)), None)
            name = c["name"] if c else None
        await call(S.matches.update, team, user, int(a), {"opponent": name})
        await show_match(cq, entry, lang, int(a), t(lang, "mt.updated") + "\n\n")
    elif action == "en" and a:
        await ask(cq, lang, {"type": "mt_note", "id": int(a)}, t(lang, "mt.ask_note"), "áo trắng")
    elif action == "c" and a:
        x = await call(S.matches.get, team, user, int(a))
        await edit(cq, t(lang, "mt.ask_cancel", match=match_line(lang, x)), reasons_kb(lang, int(a)))
    elif action == "cr" and a and b:
        reason = None if b == "none" else t(group_lang(entry), f"mt.reason_{b}").split(" ", 1)[-1]
        x = await call(S.matches.cancel, team, user, int(a), reason)
        await announce_cancel(entry, x)
        await show_match(cq, entry, lang, int(a), t(lang, "mt.cancelled") + "\n\n")
    elif action == "co" and a:
        await ask(cq, lang, {"type": "mt_reason", "id": int(a)}, t(lang, "mt.ask_reason"), "Sân sửa mặt cỏ")
    elif action == "r" and a:
        x = await call(S.matches.restore, team, user, int(a))
        await announce_cancel(entry, x, restored=True)
        await show_match(cq, entry, lang, int(a), t(lang, "mt.restored") + "\n\n")
    elif action == "d" and a:
        x = await call(S.matches.get, team, user, int(a))
        await edit(cq, t(lang, "mt.confirm_delete", match=match_line(lang, x)),
                   kb([(t(lang, "btn.yes_delete"), f"mt:dy:{a}"), (t(lang, "btn.cancel"), f"mt:v:{a}")]))
    elif action == "dy" and a:
        await call(S.matches.delete, team, user, int(a))
        await edit(cq, t(lang, "mt.deleted"))
        await show_list(cq, entry, lang)
    elif action == "sc" and a:
        x = await call(S.matches.get, team, user, int(a))
        await call(S.contacts.save_many, team, user, [{"name": x["opponent"]}])
        await show_match(cq, entry, lang, int(a), t(lang, "ct.saved_one") + "\n\n")

    # repeating calendars
    elif action == "s":
        v = await call(S.matches.show, team, user, 5)
        lines = [t(lang, "mt.sched_title")] + ([schedule_line(lang, s) for s in v["schedules"]] or [t(lang, "mt.sched_none")])
        rows = [[(t(lang, "mt.btn_stop", what=schedule_line(lang, s)[2:40]), f"mt:sx:{s['id']}")] for s in v["schedules"]]
        await edit(cq, "\n".join(lines), kb(*rows, [(t(lang, "mt.btn_sched_new"), "mt:sn")], [(t(lang, "btn.back"), "mt:l:5")]))
    elif action == "sx" and a:
        await edit(cq, t(lang, "mt.confirm_stop"), kb([(t(lang, "mt.btn_stop_yes"), f"mt:sxy:{a}"), (t(lang, "btn.cancel"), "mt:s")]))
    elif action == "sxy" and a:
        r = await call(S.matches.stop_schedule, team, user, int(a))
        await edit(cq, t(lang, "mt.stopped", n=r["dropped"]), kb([(t(lang, "btn.back"), "mt:s")]))
    elif action == "sn":
        await edit(cq, t(lang, "mt.ask_every"), kb([(t(lang, "mt.btn_weekly"), "mt:sf:weekly"),
                                                    (t(lang, "mt.btn_biweekly"), "mt:sf:biweekly")],
                                                   [(t(lang, "mt.btn_monthly"), "mt:sf:monthly")],
                                                   [(t(lang, "btn.cancel"), "mt:s")]))
    elif action == "sf" and a in ("weekly", "biweekly"):
        await edit(cq, t(lang, "mt.ask_weekday"), weekday_kb(lang, f"mt:sw:{a}:"))
    elif action == "sf" and a == "monthly":
        nth = [(t(lang, "mt.nth", n=k), f"mt:sm:{k}") for k in (1, 2, 3, 4)]
        await edit(cq, t(lang, "mt.ask_nth"), kb(nth[:2], nth[2:], [(t(lang, "mt.nth_last"), "mt:sm:9")],
                                                 [(t(lang, "mt.btn_mday"), "mt:smd")], [(t(lang, "btn.cancel"), "mt:s")]))
    elif action == "sm" and a:
        await edit(cq, t(lang, "mt.ask_weekday"), weekday_kb(lang, f"mt:smw:{a}:"))
    elif action == "smd":
        await ask(cq, lang, {"type": "mt_mday"}, t(lang, "mt.ask_mday"), "15")
    elif action == "smw" and a and b is not None:
        await sched_time(cq, lang, {"every": "monthly", "nth": -1 if a == "9" else int(a), "weekday": int(b)})
    elif action == "sw" and a == "weekly" and b is not None:
        await sched_time(cq, lang, {"every": "weekly", "weekday": int(b)})
    elif action == "sw" and a == "biweekly" and b is not None:
        today = date.today()
        first = today + timedelta(days=(int(b) - today.weekday()) % 7)
        opts = [first, first + timedelta(days=7)]
        await edit(cq, t(lang, "mt.ask_anchor"), kb([(day_label(lang, d), f"mt:sa:{_iso(d)}") for d in opts],
                                                    [(t(lang, "btn.cancel"), "mt:s")]))
    elif action == "sa" and a:
        await sched_time(cq, lang, {"every": "biweekly", "anchor": a})
    elif action == "sk":
        st = get_state(ikey(cq)) or {}
        if st.get("type") != "mt_sched":
            await edit(cq, t(lang, "err.menu_expired"))
            return
        del_state(ikey(cq))
        await sched_save(cq, entry, lang, st, None, None)


def weekday_kb(lang: str, prefix: str):
    b = [(t(lang, f"mt.wd{i}"), f"{prefix}{i}") for i in range(7)]
    return kb(b[:4], b[4:], [(t(lang, "btn.cancel"), "mt:s")])


async def sched_time(ev, lang, spec: dict) -> None:
    st = {"type": "mt_sched", **spec}
    if isinstance(ev, CallbackQuery):
        await edit(ev, t(lang, "mt.sched_time_q"), kb([(t(lang, "mt.btn_skip"), "mt:sk")], [(t(lang, "btn.cancel"), "mt:x")]))
    await ask(ev, lang, st, t(lang, "mt.ask_sched_time"), "19h @Sân Mỹ Đình")


async def sched_save(ev, entry, lang, st: dict, at, place) -> None:
    kw = {k: st.get(k) for k in ("weekday", "nth", "mday")}
    if st.get("anchor"):
        kw["anchor"] = _d(st["anchor"])
    await call(S.matches.add_schedule, tid(entry), ev.from_user.id, st["every"], at=at, place=place, **kw)
    await say(ev, t(lang, "mt.sched_saved"))
    await show_list(ev, entry, lang)


async def add_day(ev, entry, lang, day: date) -> None:
    d = await call(S.matches.defaults, tid(entry), ev.from_user.id)
    st = {"type": "mt_add_time", "day": day.isoformat(), "defaults": d}
    rows = []
    if d.get("at") or d.get("place"):
        usual = " ".join(x for x in (d.get("at"), f"@{d['place']}" if d.get("place") else None) if x)
        rows.append([(t(lang, "mt.btn_usual", usual=usual), "mt:at:d")])
    rows += [[(t(lang, "mt.btn_skip"), "mt:at:0")], [(t(lang, "btn.cancel"), "mt:x")]]
    await say(ev, t(lang, "mt.ask_time", day=day_label(lang, day)), reply_markup=kb(*rows))
    await ask(ev, lang, st, t(lang, "mt.type_time"), "19h @Sân A")


async def add_opponent(ev, entry, lang, st: dict) -> None:
    st = {**st, "type": "mt_add_opp"}
    await say(ev, t(lang, "mt.ask_opp"), reply_markup=await opponent_kb(ev, entry, lang, "mt:ao:", "mt:x"))
    await ask(ev, lang, st, t(lang, "mt.type_opp"), "FC Moreh")


async def add_save(ev, entry, lang, st: dict, opponent: str | None) -> None:
    del_state(ikey(ev))
    item = {"day": date.fromisoformat(st["day"]), "at": st.get("at"), "place": st.get("place"), "opponent": opponent}
    x = (await call(S.matches.add, tid(entry), ev.from_user.id, [item]))[0]
    await show_match(ev, entry, lang, x["id"], t(lang, "mt.added") + "\n\n", edit_in_place=False)


# ---- /contacts --------------------------------------------------------------------------------------

def contact_list(lang: str, items: list, query: str | None):
    head = t(lang, "ct.found", q=esc(query), n=len(items)) if query else t(lang, "ct.title", n=len(items))
    btn = [(c["name"][:30], f"ct:v:{c['id']}") for c in items[:40]]
    rows = [btn[i:i + 2] for i in range(0, len(btn), 2)]
    text = head + ("" if items else "\n" + t(lang, "ct.empty")) + "\n\n" + t(lang, "ct.help")
    return text, kb(*rows, [(t(lang, "ct.btn_add"), "ct:a")])


@router.message(Command("contacts"))
async def cmd_contacts(message: Message, command: CommandObject, entry, lang):
    """Contacts of opposing teams: list, `/contacts moreh` to search, or paste lines to add."""
    if not await allow(message, entry, lang, "manager"):
        return
    args = (command.args or "").strip()
    if args and ("\n" in args or "(" in args or re.search(r"\d{4}", args)):
        await contacts_preview(message, entry, lang, args)
        return
    items = await call(S.contacts.search, tid(entry), message.from_user.id, args or None)
    text, markup = contact_list(lang, items, args or None)
    await message.answer(text, reply_markup=markup)


async def contacts_preview(message: Message, entry, lang, text: str) -> None:
    items = [p for p in (parse_contact(x) for x in text.splitlines()) if p]
    if not items:
        await message.answer(t(lang, "ct.usage"))
        return
    marked = await call(S.contacts.preview, tid(entry), message.from_user.id, items)
    set_state(f"ctp:{message.bot.id}:{message.chat.id}", items, 900)
    lines = [t(lang, "ct.read", n=len(items))]
    for c in marked:
        tag = t(lang, "ct.tag_update") if c["update"] else t(lang, "ct.tag_new")
        lines.append(f"• {tag} <b>{esc(c['name'])}</b>" + (f" · 📞 {show_phone(c['phone'])}" if c["phone"] else "")
                     + (f" · {esc(c['note'])}" if c["note"] else ""))
    await message.answer("\n".join(lines), reply_markup=kb([(t(lang, "mt.btn_save"), "ct:ok"), (t(lang, "btn.cancel"), "ct:x")]))


async def ask_new_phone(ev, lang, item: dict, invalid: bool = False) -> None:
    set_state(ikey(ev), {"type": "ct_new_phone", "item": item}, 900)
    await say(ev, (t(lang, "err.invalid") + "\n\n" if invalid else "") + t(lang, "ct.new_phone", name=esc(item["name"])),
              reply_markup=kb([(t(lang, "mt.btn_skip"), "ct:skp")]))
    await say(ev, t(lang, "ct.type_below"), reply_markup=ForceReply(force_reply=True, input_field_placeholder="0912 345 678"))


async def contact_next(ev, entry, lang, item: dict, asked_phone: bool = False, asked_note: bool = False) -> None:
    """Adding one team: ask for the phone, then the note, unless already given or skipped; then save."""
    if not item.get("phone") and not asked_phone:
        await ask_new_phone(ev, lang, item)
        return
    if not item.get("note") and not asked_note:
        set_state(ikey(ev), {"type": "ct_new_note", "item": item}, 900)
        await say(ev, t(lang, "ct.new_note", name=esc(item["name"])), reply_markup=kb([(t(lang, "mt.btn_skip"), "ct:skn")]))
        await say(ev, t(lang, "ct.type_below"),
                  reply_markup=ForceReply(force_reply=True, input_field_placeholder="fb: Dũng, zalo: 09…"))
        return
    del_state(ikey(ev))
    team, user = tid(entry), ev.from_user.id
    r = await call(S.contacts.save_many, team, user, [item])
    c = next(c for c in await call(S.contacts.search, team, user, item["name"]) if c["name"] == item["name"])
    text, markup = contact_view(lang, await call(S.contacts.get, team, user, c["id"]))
    head = t(lang, "ct.saved_one") if r["added"] else t(lang, "ct.updated")
    await say(ev, f"{head}\n\n{text}", reply_markup=markup)


def contact_view(lang: str, c: dict):
    lines = [f"📇 <b>{esc(c['name'])}</b>", f"📞 {show_phone(c['phone'])}" if c.get("phone") else t(lang, "ct.no_phone")]
    if c.get("note"):
        lines.append(f"📝 {esc(c['note'])}")
    hist = t(lang, "ct.history", played=c["played"], cancelled=c["cancelled"])
    if c.get("last"):
        hist += t(lang, "ct.last", day=day_label(lang, date.fromisoformat(c["last"])))
    if c.get("next"):
        hist += t(lang, "ct.next", day=day_label(lang, date.fromisoformat(c["next"])))
    lines.append("⚽ " + hist)
    i = c["id"]
    return "\n".join(lines), kb([(t(lang, "ct.btn_name"), f"ct:en:{i}"), (t(lang, "ct.btn_phone"), f"ct:ph:{i}"),
                                 (t(lang, "ct.btn_note"), f"ct:nt:{i}")],
                                [(t(lang, "mt.btn_delete"), f"ct:d:{i}"), (t(lang, "ct.btn_list"), "ct:l")])


@router.callback_query(F.data.regexp(r"^ct:(\w+)(?::(\d+))?$").as_("m"))
async def on_contacts(cq: CallbackQuery, m: re.Match, entry, lang):
    if not await allow(cq, entry, lang, "manager"):
        return
    await cq.answer()
    action, a = m.group(1), m.group(2)
    team, user = tid(entry), cq.from_user.id
    pk = f"ctp:{cq.bot.id}:{cq.message.chat.id}"
    if action == "l":
        text, markup = contact_list(lang, await call(S.contacts.search, team, user), None)
        await edit(cq, text, markup)
    elif action == "v" and a:
        c = await call(S.contacts.get, team, user, int(a))
        text, markup = contact_view(lang, c)
        await edit(cq, text, markup)
    elif action == "a":
        await ask(cq, lang, {"type": "ct_add"}, t(lang, "ct.ask_add"), "FC Moreh")
    elif action in ("skp", "skn"):  # skip the phone / the note while adding one team
        st = get_state(ikey(cq)) or {}
        want = "ct_new_phone" if action == "skp" else "ct_new_note"
        if st.get("type") != want:
            await edit(cq, t(lang, "err.menu_expired"))
            return
        await edit(cq, t(lang, "ct.skipped"))
        await contact_next(cq, entry, lang, st["item"], asked_phone=True, asked_note=action == "skn")
    elif action == "en" and a:
        await ask(cq, lang, {"type": "ct_name", "id": int(a)}, t(lang, "ct.ask_name"), "FC Moreh")
    elif action == "ph" and a:
        await ask(cq, lang, {"type": "ct_phone", "id": int(a)}, t(lang, "ct.ask_phone"), "0912 345 678")
    elif action == "nt" and a:
        await ask(cq, lang, {"type": "ct_note", "id": int(a)}, t(lang, "ct.ask_note"), "0912 345 678, fb: Dũng")
    elif action == "d" and a:
        c = await call(S.contacts.get, team, user, int(a))
        await edit(cq, t(lang, "ct.confirm_delete", name=esc(c["name"])),
                   kb([(t(lang, "btn.yes_delete"), f"ct:dy:{a}"), (t(lang, "btn.cancel"), f"ct:v:{a}")]))
    elif action == "dy" and a:
        c = await call(S.contacts.delete, team, user, int(a))
        await edit(cq, t(lang, "ct.deleted", name=esc(c["name"])), kb([(t(lang, "ct.btn_list"), "ct:l")]))
    elif action == "ok":
        items = get_state(pk)
        del_state(pk)
        if not items:
            await edit(cq, t(lang, "err.menu_expired"))
            return
        r = await call(S.contacts.save_many, team, user, items)
        await edit(cq, t(lang, "ct.saved", added=r["added"], updated=r["updated"]), kb([(t(lang, "ct.btn_list"), "ct:l")]))
    elif action == "x":
        del_state(pk)
        await edit(cq, t(lang, "cancelled"))


# ---- typed replies for the prompts above ------------------------------------------------------------

def _mine(message: Message) -> bool:
    if not message.text or message.text.startswith("/"):
        return False
    st = get_state(input_key(message.bot.id, message.chat.id))
    return bool(st) and str(st.get("type", "")).startswith(("mt_", "ct_"))


@router.message(F.text, _mine)
async def on_reply(message: Message, entry, lang):
    key = input_key(message.bot.id, message.chat.id)
    st = get_state(key)
    del_state(key)
    if not await allow(message, entry, lang, "manager"):
        return
    text, kind, team, user = message.text.strip(), st["type"], tid(entry), message.from_user.id
    today = date.today()

    def again(prompt_key: str, placeholder: str):
        return ask(message, lang, st, t(lang, "err.invalid") + "\n\n" + t(lang, prompt_key), placeholder)

    if kind == "mt_add_day":
        p = parse_match(text, today)
        if not p:
            await again("mt.type_day", "7/12")
            return
        await add_day(message, entry, lang, p["day"])
    elif kind == "mt_add_time":
        p = parse_match(f"{date.fromisoformat(st['day']):%d/%m/%Y} {text}", today)
        st.update({"at": p["at"], "place": p["place"]})
        await add_opponent(message, entry, lang, st)
    elif kind == "mt_add_opp":
        await add_save(message, entry, lang, st, text)
    elif kind == "mt_when":
        x = await call(S.matches.get, team, user, st["id"])
        p = parse_match(text, today) or parse_match(f"{date.fromisoformat(x['day']):%d/%m/%Y} {text}", today)
        if not p:
            await again("mt.ask_when", "7/12 19h @Sân A")
            return
        changes = {"day": p["day"]}
        if p["at"]:
            changes["at"] = p["at"]
        if p["place"]:
            changes["place"] = p["place"]
        await call(S.matches.update, team, user, st["id"], changes)
        await show_match(message, entry, lang, st["id"], t(lang, "mt.updated") + "\n\n", edit_in_place=False)
    elif kind == "mt_opp":
        await call(S.matches.update, team, user, st["id"], {"opponent": None if text == "-" else text})
        await show_match(message, entry, lang, st["id"], t(lang, "mt.updated") + "\n\n", edit_in_place=False)
    elif kind == "mt_note":
        await call(S.matches.update, team, user, st["id"], {"note": None if text == "-" else text})
        await show_match(message, entry, lang, st["id"], t(lang, "mt.updated") + "\n\n", edit_in_place=False)
    elif kind == "mt_reason":
        x = await call(S.matches.cancel, team, user, st["id"], text)
        await announce_cancel(entry, x)
        await show_match(message, entry, lang, st["id"], t(lang, "mt.cancelled") + "\n\n", edit_in_place=False)
    elif kind == "mt_mday":
        if not text.isdigit() or not 1 <= int(text) <= 31:
            await again("mt.ask_mday", "15")
            return
        await sched_time(message, lang, {"every": "monthly", "mday": int(text)})
    elif kind == "mt_sched":
        p = parse_match(f"{today:%d/%m/%Y} {text}", today)
        await sched_save(message, entry, lang, st, p["at"], p["place"] or p["opponent"])
    elif kind == "ct_add":
        lines = [x for x in text.splitlines() if x.strip()]
        p = parse_contact(text) if len(lines) == 1 else None
        if not p:
            await contacts_preview(message, entry, lang, text)  # several teams: show them, then save
            return
        await contact_next(message, entry, lang, p)
    elif kind == "ct_new_phone":
        phone = parse_phone(text)
        if not phone:
            await ask_new_phone(message, lang, st["item"], invalid=True)
            return
        await contact_next(message, entry, lang, {**st["item"], "phone": phone}, asked_phone=True)
    elif kind == "ct_new_note":
        await contact_next(message, entry, lang, {**st["item"], "note": text}, asked_phone=True, asked_note=True)
    elif kind in ("ct_name", "ct_phone", "ct_note"):
        if kind == "ct_name":
            changes = {"name": text}
        elif kind == "ct_phone":
            phone = None if text == "-" else parse_phone(text)
            if text != "-" and not phone:
                await again("ct.ask_phone", "0912 345 678")
                return
            changes = {"phone": phone}
        else:
            changes = {"note": None if text == "-" else text}
        await call(S.contacts.update, team, user, st["id"], changes)
        c = await call(S.contacts.get, team, user, st["id"])
        text_, markup = contact_view(lang, c)
        await message.answer(t(lang, "ct.updated") + "\n\n" + text_, reply_markup=markup)
