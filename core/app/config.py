import os


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
    internal_key = os.getenv("INTERNAL_KEY", "dev-internal-key")
    token_enc_key = os.getenv("TOKEN_ENC_KEY", "")
    gateway_url = os.getenv("GATEWAY_URL", "http://gateway:3000").rstrip("/")
    public_url = os.getenv("PUBLIC_URL", "https://localhost").rstrip("/")
    timezone = os.getenv("TEAM_TIMEZONE", "Asia/Ho_Chi_Minh")
    default_lang = os.getenv("DEFAULT_LANG", "vi")
    master_admin_ids = _ids(os.getenv("MASTER_ADMIN_IDS", ""))


settings = Settings()
