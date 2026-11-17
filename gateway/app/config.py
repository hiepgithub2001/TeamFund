import os

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode


def _ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


class Config:
    port = int(os.getenv("PORT", "3000"))
    core_url = os.getenv("CORE_URL", "http://core:8000").rstrip("/")
    internal_key = os.getenv("INTERNAL_KEY", "dev-internal-key")
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    master_token = os.getenv("MASTER_BOT_TOKEN", "")
    master_secret = os.getenv("MASTER_WEBHOOK_SECRET", "change-me")
    admin_ids = _ids(os.getenv("MASTER_ADMIN_IDS", ""))
    # webhook: Telegram pushes updates to PUBLIC_URL. polling: we pull them (no HTTPS needed).
    mode = "polling" if os.getenv("TELEGRAM_MODE") == "polling" else "webhook"
    # Only for tests: point the bots at a fake Telegram server.
    api_root = os.getenv("TELEGRAM_API_ROOT") or None
    default_lang = "en" if os.getenv("DEFAULT_LANG") == "en" else "vi"


cfg = Config()

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]


def is_super(tg_id) -> bool:
    return tg_id is not None and int(tg_id) in cfg.admin_ids


def make_bot(token: str) -> Bot:
    session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.api_root)) if cfg.api_root else AiohttpSession()
    return Bot(token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
