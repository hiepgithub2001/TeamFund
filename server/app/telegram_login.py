"""Log the master bot in to the admin's Telegram account, so it can create team bots through @BotFather.

Run inside the app container, in two steps (so it works without an interactive terminal):

  python -m app.telegram_login phone +84901234567   # Telegram sends a login code
  python -m app.telegram_login code 12345           # finishes the login
  python -m app.telegram_login password             # only if the account has 2FA (asks for it)
  python -m app.telegram_login status               # who is logged in
  python -m app.telegram_login logout               # ends the session on Telegram too

The session is stored encrypted (TOKEN_ENC_KEY) in the app_secrets table. It is a full login to that
account: anyone with it and the key can act as that account. `logout` revokes it on Telegram's side."""
import asyncio
import getpass
import json
import sys

from .config import settings
from .infrastructure.botfather import SESSION
from .infrastructure.crypto import FernetCipher
from .infrastructure.db import make_session_factory
from .infrastructure.secret_store import SecretStore

PENDING = "telegram_login_pending"


def _store() -> SecretStore:
    return SecretStore(make_session_factory(settings.database_url), FernetCipher(settings.token_enc_key))


def _client(session: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        sys.exit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
    return TelegramClient(StringSession(session), settings.telegram_api_id, settings.telegram_api_hash)


def _who(me) -> str:
    name = " ".join(x for x in (me.first_name, me.last_name) if x)
    return f"{name} (@{me.username})" if me.username else name


async def _finish(store: SecretStore, client) -> None:
    store.set(SESSION, client.session.save())
    store.delete(PENDING)
    print(f"Logged in as {_who(await client.get_me())}. The master bot can now create team bots.")


async def phone(store: SecretStore, number: str) -> None:
    client = _client()
    await client.connect()
    try:
        sent = await client.send_code_request(number)
        store.set(PENDING, json.dumps({"session": client.session.save(), "phone": number,
                                       "hash": sent.phone_code_hash}))
        print("Telegram sent a login code to the Telegram app (or by SMS). Next:\n"
              "  python -m app.telegram_login code <the code>")
    finally:
        await client.disconnect()


def _pending(store: SecretStore) -> dict:
    raw = store.get(PENDING)
    if not raw:
        sys.exit("No login in progress. Start with: python -m app.telegram_login phone <number>")
    return json.loads(raw)


async def code(store: SecretStore, value: str) -> None:
    from telethon import errors
    p = _pending(store)
    client = _client(p["session"])
    await client.connect()
    try:
        await client.sign_in(p["phone"], value.strip(), phone_code_hash=p["hash"])
        await _finish(store, client)
    except errors.SessionPasswordNeededError:
        store.set(PENDING, json.dumps({**p, "session": client.session.save()}))
        print("This account has a 2FA password. Next:\n  python -m app.telegram_login password")
    except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
        print(f"That code didn't work ({type(e).__name__}). Start again with `phone`.")
    finally:
        await client.disconnect()


async def password(store: SecretStore, value: str | None) -> None:
    p = _pending(store)
    client = _client(p["session"])
    await client.connect()
    try:
        await client.sign_in(password=value or getpass.getpass("2FA password: "))
        await _finish(store, client)
    finally:
        await client.disconnect()


async def status(store: SecretStore) -> None:
    session = store.get(SESSION)
    if not session:
        print("Not logged in.")
        return
    client = _client(session)
    await client.connect()
    try:
        print(f"Logged in as {_who(await client.get_me())}." if await client.is_user_authorized()
              else "The stored login no longer works. Log in again.")
    finally:
        await client.disconnect()


async def logout(store: SecretStore) -> None:
    session = store.get(SESSION)
    if session:
        client = _client(session)
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await client.disconnect()
    store.delete(SESSION)
    store.delete(PENDING)
    print("Logged out. The master bot can no longer create bots.")


def main(argv: list[str]) -> None:
    store = _store()
    cmd, args = (argv[0] if argv else "status"), argv[1:]
    if cmd == "phone" and args:
        asyncio.run(phone(store, args[0]))
    elif cmd == "code" and args:
        asyncio.run(code(store, args[0]))
    elif cmd == "password":
        asyncio.run(password(store, args[0] if args else None))
    elif cmd == "status":
        asyncio.run(status(store))
    elif cmd == "logout":
        asyncio.run(logout(store))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
