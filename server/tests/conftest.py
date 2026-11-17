"""Test settings, applied before any app module is imported."""
import os
import tempfile

from cryptography.fernet import Fernet

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_db.name}",
    "TOKEN_ENC_KEY": Fernet.generate_key().decode(),
    "MASTER_ADMIN_IDS": "1",
    "PUBLIC_URL": "https://example.test",
})
