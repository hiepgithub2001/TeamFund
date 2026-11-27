"""Message builders and small Telegram helpers shared by the bots and core events."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from ...domain.errors import DomainError
from .fmt import esc, money, strip_tags
from .i18n import has, t

log = logging.getLogger("render")


def kb(*rows) -> InlineKeyboardMarkup:
    """kb([("Label", "data"), ...], [...]) -> inline keyboard. Empty rows are skipped."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows if row])


def pay_keyboard(lang: str, bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(lang, "btn.pay_now"), url=f"https://t.me/{bot_username}?start=pay")]])


def owes_lines(lang: str, rows: list[dict]) -> str:
    return "\n".join(t(lang, "line.owes", name=esc(u["name"]), amount=money(u["total"], lang)) for u in rows)


def fee_text(lang: str, amount: int, months: int = 1) -> str:
    """"300.000đ/quý"."""
    return f"{money(amount, lang)}/{t(lang, f'cycle.{months}')}"


def _terms(lang: str, f: dict) -> str:
    """" · hạn 20/11 · phạt trễ 1.000đ/ngày" for one package."""
    out = t(lang, "fees.due", due=f["due"]) if f.get("due") else ""
    if f.get("late_fee"):
        out += t(lang, "fees.late", fee=money(f["late_fee"], lang))
    return out


def fees_text(lang: str, fees: list[dict], default_fee: int = 0) -> str:
    """What people pay and when it's due: "200.000đ mỗi người · hạn 20/11" for one monthly fee,
    else one line per package with its head count, due date and late fee."""
    if not fees:
        return t(lang, "fees.each", fee=money(default_fee, lang))
    if len(fees) == 1 and fees[0]["months"] == 1:
        return t(lang, "fees.each", fee=money(fees[0]["amount"], lang)) + _terms(lang, fees[0])
    return "".join("\n• " + t(lang, "fees.item", fee=fee_text(lang, f["amount"], f["months"]), n=f["count"])
                   + _terms(lang, f) for f in fees)


def status_text(lang: str, s: dict) -> str:
    if not s.get("has_fees", s["fee"]):
        return t(lang, "status.no_fee")
    parts = [t(lang, "status.title", period=s["period_label"], fees=fees_text(lang, s.get("fees", []), s["fee"]))]
    if s["paid"]:
        parts.append(t(lang, "status.paid", n=len(s["paid"]), names=", ".join(esc(n) for n in s["paid"])))
    if s["unpaid"]:
        parts.append(t(lang, "status.unpaid", n=len(s["unpaid"]), total=money(s["total_unpaid"], lang))
                     + "\n" + owes_lines(lang, s["unpaid"]))
    else:
        parts.append(t(lang, "status.all_paid"))
    return "\n\n".join(parts)


def bill_label(lang: str, b: dict) -> str:
    if b["kind"] == "late":
        if b.get("of"):  # a charge's fine
            return t(lang, "bill.late_charge", days=b.get("days", 0), title=b["of"])
        return t(lang, "bill.late", days=b.get("days", 0), period=b["period_label"])
    if b["kind"] == "monthly":
        months = b.get("months") or 1
        if months > 1:
            return t(lang, "bill.fee", cycle=t(lang, f"cycle.{months}"), period=b["period_label"])
        return t(lang, "bill.monthly", period=b["period_label"])
    return f"{b['title']} ({b['period_label']})" + (" · " + t(lang, "bill.due", due=b["due"]) if b.get("due") else "")


def error_text(lang: str, err: Exception) -> str:
    if isinstance(err, DomainError) and has(f"err.{err.code}"):
        return t(lang, f"err.{err.code}", **err.extra)
    return t(lang, "err.generic")


async def report_error(update: Update, lang: str, err: Exception) -> None:
    """Tell whoever triggered the update what went wrong, in their language."""
    if not isinstance(err, DomainError):
        log.exception("handler failed", exc_info=err)
    text = error_text(lang, err)
    try:
        if update.callback_query:
            await update.callback_query.answer(strip_tags(text), show_alert=True)
        elif update.message:
            await update.message.answer(text)
    except Exception as e:
        log.error("could not report error: %s", e)


def chat_of(ev: Message | CallbackQuery):
    return ev.chat if isinstance(ev, Message) else ev.message.chat


def is_private(ev: Message | CallbackQuery) -> bool:
    return chat_of(ev).type == "private"


async def say(ev: Message | CallbackQuery, text: str, **kw) -> Message:
    """Send a message to the chat this message or button came from."""
    return await ev.bot.send_message(chat_of(ev).id, text, **kw)


async def edit(cq: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message a button is on, ignoring Telegram's "message is not modified"."""
    try:
        await cq.bot.edit_message_text(text, chat_id=cq.message.chat.id, message_id=cq.message.message_id,
                                       reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
