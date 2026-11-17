"""Message builders and small Telegram helpers shared by the bots and core events."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from ..domain.errors import DomainError
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


def status_text(lang: str, s: dict) -> str:
    if not s["fee"]:
        return t(lang, "status.no_fee")
    parts = [t(lang, "status.title", period=s["period_label"], fee=money(s["fee"], lang), due=s["due_day"])]
    if s["paid"]:
        parts.append(t(lang, "status.paid", n=len(s["paid"]), names=", ".join(esc(n) for n in s["paid"])))
    if s["unpaid"]:
        parts.append(t(lang, "status.unpaid", n=len(s["unpaid"]), total=money(s["total_unpaid"], lang))
                     + "\n" + owes_lines(lang, s["unpaid"]))
    else:
        parts.append(t(lang, "status.all_paid"))
    return "\n\n".join(parts)


def bill_label(lang: str, b: dict) -> str:
    if b["kind"] == "monthly":
        return t(lang, "bill.monthly", period=b["period_label"])
    return f"{b['title']} ({b['period_label']})"


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
