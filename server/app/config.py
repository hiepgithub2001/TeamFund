import os


def _ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
    token_enc_key = os.getenv("TOKEN_ENC_KEY", "")
    public_url = os.getenv("PUBLIC_URL", "https://localhost").rstrip("/")
    timezone = os.getenv("TEAM_TIMEZONE", "Asia/Ho_Chi_Minh")
    default_lang = "en" if os.getenv("DEFAULT_LANG") == "en" else "vi"
    master_admin_ids = _ids(os.getenv("MASTER_ADMIN_IDS", ""))
    master_token = os.getenv("MASTER_BOT_TOKEN", "")
    master_secret = os.getenv("MASTER_WEBHOOK_SECRET", "change-me")
    # webhook: Telegram pushes updates to PUBLIC_URL. polling: we pull them (no HTTPS needed).
    telegram_mode = "polling" if os.getenv("TELEGRAM_MODE") == "polling" else "webhook"
    # Only for tests: point the bots at a fake Telegram server.
    telegram_api_root = os.getenv("TELEGRAM_API_ROOT") or None
    # SePay's user API, used to create each team's webhook. Tests point it at a fake.
    sepay_api_url = os.getenv("SEPAY_API_URL", "https://userapi.sepay.vn/v2")
    # The admin's Telegram app (my.telegram.org): lets the master bot create team bots through @BotFather.
    # The login itself is stored encrypted in the database (python -m app.telegram_login).
    telegram_api_id = int(os.getenv("TELEGRAM_API_ID") or 0)
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "")
    # Turn the background jobs off (tests).
    run_jobs = os.getenv("RUN_JOBS", "1") != "0"


settings = Settings()
