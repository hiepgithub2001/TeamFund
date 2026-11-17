import hashlib
import secrets

from cryptography.fernet import Fernet

from ..config import settings

if not settings.token_enc_key:
    raise RuntimeError("TOKEN_ENC_KEY is not set. Run scripts/init-env.sh to create one.")

_fernet = Fernet(settings.token_enc_key.encode())

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I, easy to read off a screen


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def hash_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_code(length: int = 8) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
