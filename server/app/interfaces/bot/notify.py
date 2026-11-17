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


async def deliver(ev: dict) -> bool:
    """Send the messages for one event. False if that team's bot isn't running."""
    entry = registry.by_team.get(int(ev["team_id"]))
    if entry is None:
        log.warning("event %s for team %s: bot not running", ev.get("type"), ev.get("team_id"))
        return False
    bot, G, name = entry.bot, entry.team["lang"], entry.username
    group = ev.get("group_chat_id") or entry.team.get("group_chat_id")
    kind = ev["type"]

    if kind == "payment_confirmed":
        members = esc(", ".join(ev["members"]))
        if ev.get("payer_tg_id"):
            L = await lang_for(ev["payer_tg_id"], G)
            await _send(bot, ev["payer_tg_id"], t(L, "pay.receipt", amount=money(ev["amount"], L), members=members))
        if group:
            await _send(bot, group, t(G, "group.paid", members=members, amount=money(ev["amount"], G)))
    elif kind == "unmatched_payment":
        for admin_id in ev["admins"]:
            text, markup = unmatched_card(await lang_for(admin_id, G), ev)
            await _send(bot, admin_id, text, markup)
    elif kind == "bills_created" and group:
        await _send(bot, group, t(G, "group.bills_created", period=ev["period_label"],
                                  fees=fees_text(G, ev.get("fees", []), ev["fee"]), due=ev["due"])
                    + (("\n" + t(G, "late.rule", fee=money(ev["late_fee"], G))) if ev.get("late_fee") else ""),
                    pay_keyboard(G, name))
    elif kind == "reminder" and group:
        head = t(G, "group.overdue" if ev.get("overdue") else "group.reminder", period=ev["period_label"],
                 due=ev["due"])
        if ev.get("overdue") and ev.get("late_fee"):
            head += "\n" + t(G, "late.rule", fee=money(ev["late_fee"], G))
        await _send(bot, group, f"{head}\n{owes_lines(G, ev['unpaid'])}", pay_keyboard(G, name))
    elif kind == "month_report" and group:
        head = t(G, "group.report", period=ev["period_label"], collected=money(ev["collected"], G),
                 spent=money(ev["spent"], G), balance=money(ev["balance"], G), paid=ev["paid_count"])
        tail = (t(G, "group.report_unpaid") + "\n" + owes_lines(G, ev["unpaid"])) if ev["unpaid"] \
            else t(G, "group.report_all_paid")
        await _send(bot, group, f"{head}\n\n{tail}", pay_keyboard(G, name) if ev["unpaid"] else None)
    elif kind not in ("bills_created", "reminder", "month_report"):
        log.warning("unknown event %s", kind)
    return True
