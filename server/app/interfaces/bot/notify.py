"""Turns domain events (payment confirmed, unmatched transfer, scheduler jobs) into Telegram messages."""
import logging

from . import registry
from .fmt import esc, money
from .i18n import t
from .lang import lang_for
from .render import fees_text, owes_lines, pay_keyboard
from .team import unmatched_card

log = logging.getLogger("notify")


async def _send(bot, chat_id, text, reply_markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.warning("send to %s failed: %s", chat_id, e)


async def _tell(entry, ids, text, reply_markup=None, skip=None) -> None:
    """A team announcement, privately to each follower (people who opened the team bot)."""
    from .team import send_to
    for tg_id in ids or []:
        if tg_id != skip:
            await send_to(entry, tg_id, text, reply_markup=reply_markup)


async def deliver(ev: dict) -> bool:
    """Send the messages for one event. False if that team's bot isn't running."""
    entry = registry.by_team.get(int(ev["team_id"]))
    if entry is None:
        log.warning("event %s for team %s: bot not running", ev.get("type"), ev.get("team_id"))
        return False
    bot, G, name = entry.bot, entry.team["lang"], entry.username
    to = ev.get("to") or []
    kind = ev["type"]

    if kind == "payment_confirmed":
        members = esc(", ".join(ev["members"]))
        if ev.get("payer_tg_id"):
            L = await lang_for(ev["payer_tg_id"], G)
            await _send(bot, ev["payer_tg_id"], t(L, "pay.receipt", amount=money(ev["amount"], L), members=members))
        await _tell(entry, to, t(G, "group.paid", members=members, amount=money(ev["amount"], G)),
                    skip=ev.get("payer_tg_id"))
    elif kind == "unmatched_payment":
        for admin_id in ev["admins"]:
            text, markup = unmatched_card(await lang_for(admin_id, G), ev)
            await _send(bot, admin_id, text, markup)
    elif kind == "bills_created":
        await _tell(entry, to, t(G, "group.bills_created", period=ev["period_label"],
                                  fees=fees_text(G, ev.get("fees", []), ev["fee"])),
                    pay_keyboard(G, name))
    elif kind == "reminder":
        head = t(G, "group.overdue" if ev.get("overdue") else "group.reminder", period=ev["period_label"],
                 due=ev["due"])
        if ev.get("overdue") and ev.get("late_fee"):
            head += "\n" + t(G, "late.rule", fee=money(ev["late_fee"], G))
        await _tell(entry, to, f"{head}\n{owes_lines(G, ev['unpaid'])}", pay_keyboard(G, name))
    elif kind == "match_tomorrow":
        from .matches import match_line
        lines = "\n".join("• " + match_line(G, x) for x in ev["matches"])
        await _tell(entry, to, t(G, "mt.ev_tomorrow", matches=lines))
    elif kind == "match_no_opponent":
        from .matches import match_line
        for admin_id in to:
            L = await lang_for(admin_id, G)
            await _send(bot, admin_id, t(L, "mt.ev_open", matches="\n".join("• " + match_line(L, x) for x in ev["matches"])))
    elif kind not in ("bills_created", "reminder"):
        log.warning("unknown event %s", kind)
    return True
