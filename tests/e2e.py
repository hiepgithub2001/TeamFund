"""End-to-end test: the real server, with a fake Telegram Bot API.

    python tests/e2e.py          (VERBOSE=1 to see the server log)

Needs server/requirements.txt installed."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, web
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TG_PORT, PORT = 18081, 18000
MASTER_TOKEN = "999999:" + "A" * 35
TEAM_TOKEN = "555555:" + "B" * 35
SUPER, OWNER, MEMBER, GROUP = 1, 100, 300, -1001

calls: list[dict] = []
msg_id = 1000
update_id = 1


# ---- fake Telegram -------------------------------------------------------------------------

def _value(v):
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


async def fake_telegram(request: web.Request):
    global msg_id
    token, method = request.match_info["token"], request.match_info["method"]
    form = await request.post()
    params = {k: _value(v) if isinstance(v, str) else "<file>" for k, v in form.items()}
    calls.append({"token": token, "method": method, "params": params})
    bot_id = int(token.split(":")[0])
    result = True
    if method == "getMe":
        team = bot_id == 555555
        result = {"id": bot_id, "is_bot": True, "first_name": "FC Test Bot" if team else "Master",
                  "username": "fctest_bot" if team else "master_bot", "can_join_groups": True,
                  "can_read_all_group_messages": False, "supports_inline_queries": False}
    elif method in ("sendMessage", "sendPhoto", "sendDocument", "editMessageText", "editMessageReplyMarkup"):
        msg_id += 1
        chat = int(params.get("chat_id") or 0)
        result = {"message_id": msg_id, "date": 1, "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                  "text": params.get("text") or ""}
    return web.json_response({"ok": True, "result": result})


# ---- fake SePay user API --------------------------------------------------------------------

SEPAY_TOKEN = "S" * 40
sepay_hooks: dict[str, dict] = {}


async def fake_sepay(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {SEPAY_TOKEN}":
        return web.json_response({"status": "error", "error_code": "unauthorized"}, status=401)
    path, method = request.match_info["path"], request.method
    page = {"meta": {"pagination": {"current_page": 1, "last_page": 1, "has_more": False}}}
    if method != "GET" and not request.headers.get("X-Idempotency-Key"):
        return web.json_response({"status": "error", "error_code": "idempotency_key_required"}, status=400)
    if path == "bank-accounts":
        return web.json_response({"status": "success", "data": [
            {"id": "acc-1", "account_number": "0123456789", "bank_short_name": "MBBank", "bank_code": "MB",
             "account_holder_name": "NGUYEN VAN A", "active": "1"}], **page})
    if path == "webhooks" and method == "GET":
        return web.json_response({"status": "success", "data": [
            {"id": i, "webhook_url": h["url"]} for i, h in sepay_hooks.items()], **page})
    if path == "webhooks" and method == "POST":
        i = f"wh-{len(sepay_hooks) + 1}"
        sepay_hooks[i] = await request.json()
        return web.json_response({"status": "success", "data": {"id": i}}, status=201)
    if path.startswith("webhooks/") and method == "PUT":
        i = path.split("/", 1)[1]
        if i not in sepay_hooks:
            return web.json_response({"status": "error", "error_code": "not_found"}, status=404)
        sepay_hooks[i] = await request.json()
        return web.json_response({"status": "success", "data": {"id": i}})
    return web.json_response({"status": "error", "error_code": "not_found"}, status=404)


# ---- helpers --------------------------------------------------------------------------------

def user(uid):
    return {"id": uid, "is_bot": False, "first_name": f"User{uid}", "language_code": "en"}


def chat(cid):
    return {"id": cid, "type": "supergroup" if cid < 0 else "private", **({"title": "Team"} if cid < 0 else {})}


def text_update(frm, text, chat_id=None):
    global msg_id
    msg_id += 1
    m = re.match(r"^/\w+", text)
    msg = {"message_id": msg_id, "date": 1, "from": user(frm), "chat": chat(chat_id or frm), "text": text}
    if m:
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(m.group(0))}]
    return {"message": msg}


def cb_update(frm, data, message_id, chat_id=None):
    global msg_id
    msg_id += 1
    return {"callback_query": {"id": str(msg_id), "from": user(frm), "chat_instance": "x", "data": data,
                               "message": {"message_id": message_id, "date": 1, "chat": chat(chat_id or frm),
                                           "text": ""}}}


def sent(pred):
    return [c for c in calls if c["method"] in ("sendMessage", "sendPhoto", "editMessageText") and pred(c)]


def to(chat_id, pattern):
    return lambda c: str(c["params"].get("chat_id")) == str(chat_id) and re.search(
        pattern, c["params"].get("text") or c["params"].get("caption") or "")


def buttons(c):
    return [b.get("callback_data") for row in (c["params"].get("reply_markup") or {}).get("inline_keyboard", [])
            for b in row if b.get("callback_data")]


async def until(fn, label, timeout=15):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out: {label}")


async def wait_sent(pred, label):
    return await until(lambda: (sent(pred) or [None])[-1], label)


# ---- run --------------------------------------------------------------------------------

async def main():
    global update_id
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", fake_telegram)
    app.router.add_route("*", "/sepay-api/v2/{path:.+}", fake_sepay)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", TG_PORT).start()

    tmp = tempfile.mkdtemp(prefix="tf-e2e-")
    out = None if os.getenv("VERBOSE") else subprocess.DEVNULL
    procs = [subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT / "server", stdout=out, stderr=out,
        env={"PATH": os.environ["PATH"], "DATABASE_URL": f"sqlite:///{tmp}/e2e.db",
             "TOKEN_ENC_KEY": Fernet.generate_key().decode(), "PUBLIC_URL": "https://teamfund.test",
             "MASTER_ADMIN_IDS": str(SUPER), "DEFAULT_LANG": "en", "MASTER_BOT_TOKEN": MASTER_TOKEN,
             "MASTER_WEBHOOK_SECRET": "msecret", "TELEGRAM_API_ROOT": f"http://127.0.0.1:{TG_PORT}",
             "RUN_JOBS": "0", "SEPAY_API_URL": f"http://127.0.0.1:{TG_PORT}/sepay-api/v2"})]
    http = ClientSession()
    try:
        await until(lambda: any(c["method"] == "setWebhook" and str(c["params"].get("url", "")).endswith("/tg/master")
                                for c in calls), "master webhook")
        for _ in range(150):  # webhooks are set during startup, before the server listens
            try:
                async with http.get(f"http://127.0.0.1:{PORT}/health") as r:
                    if r.status == 200:
                        break
            except Exception:
                await asyncio.sleep(0.1)

        async def send(path, secret, update, expect=200):
            global update_id
            update_id += 1
            async with http.post(f"http://127.0.0.1:{PORT}{path}", json={"update_id": update_id, **update},
                                 headers={"X-Telegram-Bot-Api-Secret-Token": secret}) as r:
                assert r.status == expect, f"{path} -> {r.status}"

        M = lambda u: send("/tg/master", "msecret", u)  # noqa: E731

        # 1. team request -> admin approves -> /newteam with token
        await M(text_update(OWNER, "/start"))
        await M(text_update(OWNER, "/requestteam"))
        await M(text_update(OWNER, "FC Test"))
        card = await wait_sent(to(SUPER, "Team request"), "admin card")
        await M(cb_update(SUPER, next(b for b in buttons(card) if b.startswith("tr:a:")), 1))
        await wait_sent(to(OWNER, "approved"), "approved message")
        await M(text_update(OWNER, "/newteam"))
        await M(text_update(OWNER, TEAM_TOKEN))
        await wait_sent(to(OWNER, "is ready"), "team created")
        hook = next(c for c in calls if c["method"] == "setWebhook" and str(c["params"].get("url")).endswith("/tg/555555"))
        assert any(c["method"] == "deleteMessage" for c in calls), "token message deleted"
        secret = hook["params"]["secret_token"]
        T = lambda u: send("/tg/555555", secret, u)  # noqa: E731
        await send("/tg/555555", "nope", {}, expect=401)

        # 2. setup in the team bot
        await T(text_update(OWNER, "/setbank MBBank 0123456789 nguyen van a"))
        await wait_sent(to(OWNER, "Bank account saved"), "bank saved")
        await T(text_update(OWNER, "/setfee 200k"))
        await wait_sent(to(OWNER, "Monthly fee set to 200,000đ"), "fee set")
        await T(text_update(OWNER, "/add An | 10\nBình\nCường | 7 | 0901234567"))
        await wait_sent(to(OWNER, "Added 3"), "members added")
        await T(text_update(OWNER, "/linkgroup", GROUP))
        await wait_sent(to(GROUP, "linked"), "group linked")
        await T(text_update(OWNER, "/bank"))
        await wait_sent(to(OWNER, "Account: <code>0123456789</code>"), "bank shown")
        await T(text_update(OWNER, "/sepay"))
        await T(cb_update(OWNER, "s:gen", 1))
        await T(text_update(OWNER, "/bank"))
        await wait_sent(to(OWNER, "SePay: ✅ connected"), "bank shows sepay status")
        key_msg = await wait_sent(to(OWNER, "New SePay API key"), "sepay key")
        api_key = re.search(r"<code>([^<]+)</code>", key_msg["params"]["text"]).group(1)

        # non-managers are refused
        await T(text_update(MEMBER, "/setfee 1"))
        await wait_sent(to(MEMBER, "Only team managers"), "role check")

        # 3. a member pays for An and Bình
        await T(text_update(MEMBER, "/start"))
        await wait_sent(to(MEMBER, "Choose who you are paying for"), "pay menu")
        menu_id = msg_id
        for d in ("k:t:0", "k:t:1", "k:ok"):
            await T(cb_update(MEMBER, d, menu_id))
        qr = await until(lambda: next((c for c in calls if c["method"] == "sendPhoto"
                                       and str(c["params"].get("chat_id")) == str(MEMBER)), None), "qr photo")
        assert re.match(r"^https://qr\.sepay\.vn/img\?acc=0123456789&bank=MBBank&amount=400000&des=TF",
                        qr["params"]["photo"]), qr["params"]["photo"]
        code = re.search(r"<code>(TF[A-Z0-9]{8})</code>", qr["params"]["caption"]).group(1)
        assert "An, Bình" in qr["params"]["caption"]

        # 4. SePay confirms
        sepay_headers = {"Authorization": f"Apikey {api_key}"}
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 1, "gateway": "MBBank", "transferType": "in", "transferAmount": 400000,
                "content": f"CT {code} FT2611"}) as r:
            assert await r.json() == {"success": True}
        await wait_sent(to(MEMBER, r"Payment received: <b>400,000đ</b>"), "receipt")
        await wait_sent(to(GROUP, r"An, Bình</b> paid 400,000đ"), "group paid post")

        # 5. status, an unmatched transfer, assign it
        await T(text_update(MEMBER, "/status"))
        await wait_sent(lambda c: to(MEMBER, r"Paid \(2\)")(c) and "Cường: 200,000đ" in c["params"]["text"], "status")
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 2, "transferType": "in", "transferAmount": 200000, "content": "cuong dong quy"}):
            pass
        um = await wait_sent(to(OWNER, "needs checking"), "unmatched card")
        await T(cb_update(OWNER, next(b for b in buttons(um) if b.startswith("u:a:")), 5))
        await wait_sent(to(OWNER, "Which members is this"), "assign picker")
        pick_id = msg_id
        await T(cb_update(OWNER, "k:t:0", pick_id))
        await T(cb_update(OWNER, "k:ok", pick_id))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Assigned to Cường" in c["params"]["text"],
                        "assigned")

        # 6. manager request and approval
        await T(text_update(MEMBER, "/requestmanager"))
        mr = await wait_sent(to(OWNER, "Manager request"), "manager card")
        await T(cb_update(OWNER, next(b for b in buttons(mr) if b.startswith("mr:a:")), 9))
        await wait_sent(to(MEMBER, "You are now a manager"), "manager approved")
        assert any(c["method"] == "setMyCommands" and (c["params"].get("scope") or {}).get("chat_id") == MEMBER
                   for c in calls), "manager menu set"
        await T(text_update(MEMBER, "/expense 300000 Pitch rental"))
        await wait_sent(to(GROUP, "Expense"), "expense posted")

        # 7. fund, members screen, language, export
        await T(text_update(OWNER, "/fund"))
        await wait_sent(to(OWNER, "Balance: 300,000đ"), "fund")
        await T(text_update(OWNER, "/members"))
        lst = await wait_sent(to(OWNER, r"Members</b> \(3\)"), "members list")
        await T(cb_update(OWNER, buttons(lst)[0], 77))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Shirt number: 10" in c["params"]["text"],
                        "member detail")
        await T(cb_update(OWNER, "l:vi", 78))
        await T(text_update(OWNER, "/fund"))
        await wait_sent(to(OWNER, "Quỹ đội"), "vietnamese")
        await T(text_update(OWNER, "/export"))
        await until(lambda: any(c["method"] == "sendDocument" for c in calls), "export sent")

        # 8. /sepay with a SePay API token: the bot creates the webhook, then overwrites it
        await T(cb_update(OWNER, "l:en", 79))
        await T(text_update(OWNER, "/sepay"))
        await wait_sent(to(OWNER, "API Access"), "asks for SePay token")
        await T(text_update(OWNER, "W" * 40))
        await wait_sent(to(OWNER, "didn't accept the API token"), "bad SePay token refused")
        await T(text_update(OWNER, "/sepaytoken"))
        await wait_sent(lambda c: to(OWNER, "/sepaytoken YOUR_TOKEN")(c), "sepaytoken without a token explains")
        deletes = sum(c["method"] == "deleteMessage" for c in calls)
        await T(text_update(OWNER, f"/sepaytoken {SEPAY_TOKEN}"))
        await wait_sent(to(OWNER, "SePay connected"), "webhook created")
        assert sum(c["method"] == "deleteMessage" for c in calls) == deletes + 1, "token message deleted"
        assert len(sepay_hooks) == 1, sepay_hooks
        hook = sepay_hooks["wh-1"]
        assert hook["url"] == "https://teamfund.test/sepay/1" and hook["bank_account_xid"] == "acc-1"
        assert (hook["event_type"], hook["auth_type"], hook["content_type"]) == ("in", "apikey", "json")
        assert hook["skip_if_no_code"] is False
        new_key = hook["secret"]
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 50, "transferType": "in", "transferAmount": 1000, "content": "x"}) as r:
            assert r.status == 401, "the old key stops working"
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers={"Authorization": f"Apikey {new_key}"},
                             json={"id": 51, "transferType": "in", "transferAmount": 1000, "content": "x"}) as r:
            assert await r.json() == {"success": True}, "SePay's new key works"
        await T(text_update(OWNER, "/sepay"))
        await wait_sent(to(OWNER, "SePay updated"), "webhook updated")
        assert list(sepay_hooks) == ["wh-1"] and sepay_hooks["wh-1"]["secret"] != new_key

        print(f"e2e passed ({len(calls)} Telegram API calls)")
    finally:
        await http.close()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        await runner.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
