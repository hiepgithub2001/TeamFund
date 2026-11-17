"""TokenCipher port: Fernet encryption for the stored team bot tokens."""
from cryptography.fernet import Fernet


class FernetCipher:
    def __init__(self, key: str):
        if not key:
            raise RuntimeError("TOKEN_ENC_KEY is not set. Run scripts/init-env.sh to create one.")
        self._f = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._f.decrypt(value.encode()).decode()
