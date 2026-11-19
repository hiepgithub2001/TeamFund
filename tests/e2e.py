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
    if method == "getWebhookInfo":
        result = {"url": "https://example.test/tg/x", "has_custom_certificate": False, "pending_update_count": 0}
    elif method == "getMe":
        team = bot_id == 555555
        result = {"id": bot_id, "is_bot": True, "first_name": "FC Test Bot" if team else "Master",
                  "username": "fctest_bot" if team else "master_bot", "can_join_groups": True,
                  "can_read_all_group_messages": False, "supports_inline_queries": False}
    elif method in ("sendMessage", "sendPhoto", "sendDocument", "editMessageText", "editMessageReplyMarkup"):
        msg_id += 1
        chat = int(params.get("chat_id") or 0)
        result = {"message_id": msg_id, "date": 1, "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                  "text": params.get("text") or ""}
        calls[-1]["message_id"] = msg_id
    return web.json_response({"ok": True, "result": result})


# ---- fake SePay user API --------------------------------------------------------------------

SEPAY_TOKEN = "S" * 40
sepay_hooks: dict[str, dict] = {}
sepay_down = {"writes": False}
sepay_tx = {"n": 3}  # transactions SePay recorded this cycle (for the plan usage in /ops)  # make webhook writes fail, to see how the bot reports it


async def fake_sepay(request: web.Request):
    if request.headers.get("Authorization") != f"Bearer {SEPAY_TOKEN}":
        return web.json_response({"status": "error", "error_code": "unauthorized"}, status=401)
    path, method = request.match_info["path"], request.method
    page = {"meta": {"pagination": {"current_page": 1, "last_page": 1, "has_more": False}}}
    if method != "GET" and not request.headers.get("X-Idempotency-Key"):
        return web.json_response({"status": "error", "error_code": "idempotency_key_required"}, status=400)
    if method != "GET" and sepay_down["writes"]:
        return web.json_response({"status": "error", "error_code": "server_error"}, status=503)
    if path == "bank-accounts":
        return web.json_response({"status": "success", "data": [
            {"id": "acc-1", "account_number": "0123456789", "bank_short_name": "MBBank", "bank_code": "MB",
             "account_holder_name": "NGUYEN VAN A", "active": "1", "va_only": False}], **page})
    if path == "transactions":
        assert request.query.get("transaction_date_from"), "counted from the cycle's start"
        return web.json_response({"status": "success", "data": [], "meta": {"pagination": {
            "total": sepay_tx["n"], "current_page": 1, "last_page": 1, "has_more": False}}})
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
        # /ops: the system admin's one-screen health view; nobody else
        await M(text_update(SUPER, "/ops"))
        ops = await wait_sent(to(SUPER, "System health"), "ops report")
        assert "Database answering" in ops["params"]["text"] and "@fctest_bot" in ops["params"]["text"], ops["params"]["text"]
        assert "<b>FC Test</b>" in ops["params"]["text"]
        await M(text_update(OWNER, "/ops"))
        await wait_sent(to(OWNER, "system admin"), "ops refused to others")
        hook = next(c for c in calls if c["method"] == "setWebhook" and str(c["params"].get("url")).endswith("/tg/555555"))
        assert any(c["method"] == "deleteMessage" for c in calls), "token message deleted"
        secret = hook["params"]["secret_token"]
        T = lambda u: send("/tg/555555", secret, u)  # noqa: E731
        await send("/tg/555555", "nope", {}, expect=401)

        # 2. setup in the team bot: the bank account can only be picked from the owner's SePay
        await T(text_update(OWNER, "/setbank MBBank 0123456789 nguyen van a"))
        await wait_sent(to(OWNER, "can't be typed in"), "typed bank account refused")
        await wait_sent(to(OWNER, "/sepaytoken YOUR_TOKEN"), "setbank needs SePay first")
        await T(text_update(OWNER, "/sepaytoken " + "W" * 40))
        await wait_sent(to(OWNER, "didn't accept the API token"), "bad SePay token refused")
        deletes = sum(c["method"] == "deleteMessage" for c in calls)
        await T(text_update(OWNER, f"/sepaytoken {SEPAY_TOKEN}"))
        await wait_sent(to(OWNER, "SePay token saved"), "token saved")
        # the SePay plan: the count comes from SePay, the limit from the owner; /ops warns from 80%
        await M(text_update(SUPER, "/ops"))
        ops = await wait_sent(lambda c: to(SUPER, "System health")(c) and "SePay plan" in c["params"]["text"], "ops sepay")
        assert "FC Test: 3 transactions since" in ops["params"]["text"] and "/sepayplan" in ops["params"]["text"]
        await M(text_update(SUPER, "/sepayplan 50 15"))
        await wait_sent(to(SUPER, "SePay plan of FC Test: 50 transactions a cycle, restarting on day 15"), "plan saved")
        await T(text_update(OWNER, "/sepayplan 7"))  # not a team bot command: changes nothing (checked below)
        sepay_tx["n"] = 45
        await M(text_update(SUPER, "/ops"))
        ops = await wait_sent(lambda c: to(SUPER, "System health")(c) and "45 / 50" in c["params"]["text"], "ops near quota")
        assert "SePay plan of FC Test nearly used up: 45 / 50 (90%)" in ops["params"]["text"], ops["params"]["text"]
        sepay_tx["n"] = 3
        # the system admin sets it from the master bot too (the plan belongs to the SePay account)
        await M(text_update(SUPER, "/sepayplan"))
        await wait_sent(lambda c: to(SUPER, "SePay plans")(c) and "1. FC Test: 3 / 50 transactions since" in c["params"]["text"] and "restarts on day 15" in c["params"]["text"],
                        "plans listed in the master bot")
        await M(text_update(SUPER, "/sepayplan 200"))
        await wait_sent(to(SUPER, "SePay plan of FC Test: 200 transactions a cycle, restarting on day 1"), "plan set by admin")
        await M(text_update(OWNER, "/sepayplan 10"))
        await wait_sent(to(OWNER, "system admin"), "master /sepayplan is admin-only")
        await M(text_update(SUPER, "/nosuchthing"))
        await wait_sent(to(SUPER, "I don't know /nosuchthing here"), "unknown commands get an answer")
        assert sum(c["method"] == "deleteMessage" for c in calls) == deletes + 1, "token message deleted"
        picker = await wait_sent(lambda c: c["method"] == "editMessageText" and "Choose the team's bank account"
                                 in c["params"]["text"], "bank picker opens after the token")
        assert buttons(picker) == ["b:0", "b:x"], buttons(picker)
        loading = next(c for c in calls if c["method"] == "sendMessage" and "Reading your SePay" in c["params"]["text"])
        await T(cb_update(OWNER, "b:0", loading["message_id"]))
        done = await wait_sent(lambda c: c["method"] == "editMessageText" and "Done." in c["params"]["text"],
                               "bank picked")
        for line in ("✅ Read your SePay: 1 bank account", "✅ Found MBBank …6789", "✅ Made a new security key",
                     "✅ Created webhook “Team Fund - FC Test” → https://teamfund.test/sepay/1",
                     "✅ Saved the team's bank account", "pay into <code>0123456789</code>"):
            assert line in done["params"]["text"], (line, done["params"]["text"])
        assert any(c["method"] == "editMessageText" and "Working…" in c["params"]["text"]
                   and "Read your SePay" in c["params"]["text"] for c in calls), "steps shown while working"
        assert len(sepay_hooks) == 1, sepay_hooks
        hook = sepay_hooks["wh-1"]
        assert hook["url"] == "https://teamfund.test/sepay/1" and hook["bank_account_xid"] == "acc-1"
        assert (hook["event_type"], hook["auth_type"], hook["content_type"]) == ("in", "apikey", "json")
        assert hook["skip_if_no_code"] is False
        await T(text_update(OWNER, "/plans"))
        pl = await wait_sent(to(OWNER, "Fee plans"), "plans for the default fee")
        await T(cb_update(OWNER, "p:f", pl["message_id"]))
        await T(cb_update(OWNER, "p:fa", pl["message_id"]))
        await wait_sent(to(OWNER, "Send the default package's fee"), "default fee asked")
        await T(text_update(OWNER, "200k"))
        await wait_sent(to(OWNER, "Default package set to 200,000đ/month"), "fee set")
        await T(text_update(OWNER, "/add An | 10\nBình\nCường | 7 | 0901234567"))
        await wait_sent(to(OWNER, "Added 3"), "members added")
        await T(text_update(MEMBER, "/start"))
        await wait_sent(to(MEMBER, "Welcome to <b>FC Test</b>"), "a new follower is welcomed")
        await T(text_update(OWNER, "/bank"))
        await wait_sent(to(OWNER, "Account: <code>0123456789</code>"), "bank shown")
        await T(text_update(OWNER, "/bank"))
        await wait_sent(to(OWNER, "SePay: ✅ connected"), "bank shows sepay status")
        api_key = hook["secret"]  # what SePay sends with every webhook call

        # non-managers are refused
        await T(text_update(MEMBER, "/plans"))
        refused = await wait_sent(to(MEMBER, "Only team managers"), "role check")
        assert "💡" not in refused["params"]["text"], "no how-to tip on a refusal"

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
        await wait_sent(to(OWNER, r"An, Bình</b> paid 400,000đ"), "followers told who paid")
        assert not sent(to(MEMBER, r"An, Bình</b> paid 400,000đ")), "the payer only gets the receipt"

        # 4b. a donation: any multiple of 1,000đ, made by anyone; SePay confirms and everyone is thanked
        await T(text_update(MEMBER, "/donate"))
        ask = await wait_sent(to(MEMBER, "Give to the team fund"), "donation amount asked")
        assert "dn:50000" in buttons(ask)
        assert "/donate 50k" in ask["params"]["text"], "a bare command ends with its tip"
        await T(text_update(MEMBER, "1500"))
        await wait_sent(to(MEMBER, "multiple of 1,000đ"), "odd amount refused")
        await T(text_update(MEMBER, "30k"))
        dq = await until(lambda: next((c for c in calls if c["method"] == "sendPhoto"
                                       and "Donation:" in c["params"].get("caption", "")), None), "donation qr")
        assert "amount=30000" in dq["params"]["photo"]
        dcode = re.search(r"<code>(TF[A-Z0-9]{8})</code>", dq["params"]["caption"]).group(1)
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 41, "gateway": "MBBank", "transferType": "in", "transferAmount": 30000,
                "content": f"{dcode} ung ho"}) as r:
            assert await r.json() == {"success": True}
        await wait_sent(to(MEMBER, r"Received <b>30,000đ</b> for the fund"), "donor thanked")
        await wait_sent(to(OWNER, r"gave 30,000đ to the fund"), "followers told")

        # 5. status, an unmatched transfer, assign it
        await T(text_update(MEMBER, "/status"))
        st = await wait_sent(lambda c: to(MEMBER, r"Paid \(2\)")(c) and "Cường: 200,000đ" in c["params"]["text"], "status")
        assert st["params"]["text"].endswith("💡 Pay: /start · where the money went: /fund, /history"), "status tip"
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

        # 6. manager request and approval: a member who sends the owner's /requests by mistake gets a button
        await T(text_update(MEMBER, "/requests"))
        hint = await wait_sent(to(MEMBER, "review manager requests"), "requests explained to a member")
        assert buttons(hint) == ["mr:ask"]
        await T(cb_update(MEMBER, "mr:ask", 8))
        mr = await wait_sent(to(OWNER, "Manager request"), "manager card")
        await T(cb_update(OWNER, next(b for b in buttons(mr) if b.startswith("mr:a:")), 9))
        await wait_sent(to(MEMBER, "You are now a manager"), "manager approved")
        assert any(c["method"] == "setMyCommands" and (c["params"].get("scope") or {}).get("chat_id") == MEMBER
                   for c in calls), "manager menu set"
        await T(text_update(MEMBER, "/expense 300000 Pitch rental"))
        await wait_sent(to(OWNER, "Expense"), "expense announced to followers")

        # 7. fund, members screen, language, export
        await T(text_update(OWNER, "/fund"))
        await wait_sent(to(OWNER, "Balance: 330,000đ"), "fund")
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

        # 8a. SePay fails while writing the webhook: the chat names the step and the reason, nothing is saved
        sepay_down["writes"] = True
        await T(cb_update(OWNER, "l:en", 78))
        before = len(calls)
        await T(text_update(OWNER, "/setbank"))
        loading = await until(lambda: next((c for c in calls[before:] if c["method"] == "sendMessage"
                                            and "Reading your SePay" in c["params"]["text"]), None), "picker")
        await until(lambda: any(c["method"] == "editMessageText" and "Choose the team's bank account" in c["params"]["text"]
                                for c in calls[before:]), "picker shown")
        await T(cb_update(OWNER, "b:0", loading["message_id"]))
        failed = await wait_sent(lambda c: c["method"] == "editMessageText" and "❌" in c["params"]["text"], "failure shown")
        for part in ("✅ Read your SePay", "❌ Creating / overwriting webhook “Team Fund - FC Test”",
                     "Couldn't reach SePay", "Nothing was changed"):
            assert part in failed["params"]["text"], (part, failed["params"]["text"])
        assert any(c["method"] == "editMessageText" and "⏳ Creating / overwriting webhook" in c["params"]["text"]
                   for c in calls[before:]), "the webhook step is shown while it runs"
        sepay_down["writes"] = False
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 49, "transferType": "in", "transferAmount": 1000, "content": "x"}) as r:
            assert await r.json() == {"success": True}, "the failed attempt kept the working key"

        # 8. picking the same account again repairs the webhook with a fresh key; the old key stops working
        await T(cb_update(OWNER, "l:en", 79))
        await T(text_update(OWNER, "/sepaytoken"))
        await wait_sent(lambda c: to(OWNER, "/sepaytoken YOUR_TOKEN")(c), "sepaytoken without a token explains")
        before = len(calls)
        await T(text_update(OWNER, "/setbank"))
        picker = await until(lambda: next((c for c in calls[before:] if c["method"] == "editMessageText"
                                           and "Choose the team's bank account" in c["params"]["text"]), None),
                             "picker again")
        assert picker["params"]["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("✅ "), "current account marked"
        loading = next(c for c in calls[before:] if c["method"] == "sendMessage" and "Reading your SePay" in c["params"]["text"])
        await T(cb_update(OWNER, "b:0", loading["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Done." in c["params"]["text"]
                        and "✅ Updated webhook" in c["params"]["text"], "webhook repaired")
        assert list(sepay_hooks) == ["wh-1"]
        new_key = sepay_hooks["wh-1"]["secret"]
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers=sepay_headers, json={
                "id": 50, "transferType": "in", "transferAmount": 1000, "content": "x"}) as r:
            assert r.status == 401, "the old key stops working"
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1", headers={"Authorization": f"Apikey {new_key}"},
                             json={"id": 51, "transferType": "in", "transferAmount": 1000, "content": "x"}) as r:
            assert await r.json() == {"success": True}, "SePay's new key works"

        # 9. the pay page for people without Telegram
        await T(text_update(OWNER, "/add Dũng"))
        await wait_sent(to(OWNER, "Added 1"), "member for the web test")
        await T(text_update(OWNER, "/weblink"))
        link_msg = await wait_sent(to(OWNER, "Pay page"), "web link")
        page = re.search(r"https://teamfund\.test(/p/[\w-]+/)", link_msg["params"]["text"]).group(1)
        await T(text_update(555, "/weblink"))  # a plain member: sees the link, no button to change it
        seen = await wait_sent(lambda c: to(555, "Pay page")(c), "member sees the link")
        assert page in seen["params"]["text"] and "w:reset" not in buttons(seen)
        await T(text_update(555, "/notify"))  # keep the follower counts below as they were
        await wait_sent(to(555, "announcements are off"), "extra member muted")
        base = f"http://127.0.0.1:{PORT}{page}"
        async with http.get(base) as r:
            assert r.status == 200 and "app.js" in await r.text()
            assert r.headers["Referrer-Policy"] == "no-referrer" and "script-src 'self'" in r.headers["Content-Security-Policy"]
        for asset in ("app.js", "app.css", "sw.js", "icon-192.png", "icon.svg"):
            async with http.get(base + asset) as r:
                assert r.status == 200, asset
        async with http.get(base + "manifest.webmanifest") as r:
            man = json.loads(await r.text())
            assert man["start_url"] == "./" and man["display"] == "standalone" and "FC Test" in man["name"]
        async with http.get(base + "api/state") as r:
            raw = await r.text()
            st = json.loads(raw)
            assert [u["name"] for u in st["unpaid"]] == ["Dũng"] and st["bank_account"] == "0123456789"
            assert {"paid", "fund", "months", "history", "members"} <= set(st), set(st)
            assert "Cường" in [m["name"] for m in st["members"]] and st["fund"]["expense"] > 0
            assert "0901234567" not in raw, "phone numbers never reach the page"
        async with http.post(base + "api/pay", json={"member_ids": [st["unpaid"][0]["member_id"]]}) as r:
            intent = await r.json()
            assert r.status == 200 and intent["amount"] == 200000, intent
        async with http.get(base + f"api/pay/{intent['code']}") as r:
            assert (await r.json())["status"] == "pending"
        async with http.post(f"http://127.0.0.1:{PORT}/sepay/1",
                             headers={"Authorization": f"Apikey {sepay_hooks['wh-1']['secret']}"},
                             json={"id": 90, "transferType": "in", "transferAmount": 200000,
                                   "content": f"{intent['content']} chuyen tien"}) as r:
            assert await r.json() == {"success": True}
        async with http.get(base + f"api/pay/{intent['code']}") as r:
            assert (await r.json())["status"] == "paid", "web payment confirmed"
        await wait_sent(to(OWNER, r"Dũng</b> paid 200,000đ"), "followers told about a web payment")
        async with http.get(base + "api/state") as r:
            assert (await r.json())["unpaid"] == []
        async with http.get(base + "api/month/" + st["months"][0]["period"]) as r:
            mm = await r.json()
            assert r.status == 200 and any(i["title"] == "Dũng" for i in mm["items"]), mm
        async with http.get(base + "api/month/2026-13") as r:
            assert r.status == 422
        # a new link: the old one stops working
        await T(cb_update(OWNER, "w:reset", 90))
        await T(cb_update(OWNER, "w:reset!", 91))
        await wait_sent(to(OWNER, "New pay page link"), "link reset")
        async with http.get(base + "api/state") as r:
            assert r.status == 404

        # 10. fee plans: Cường and Dũng pay 300k per quarter, An 150k a month, Bình stays on the team fee
        await T(text_update(OWNER, "/plans"))
        plans = await wait_sent(to(OWNER, "Fee plans"), "plans screen")
        assert "Default: <b>200,000đ/month</b> · 4 people" in plans["params"]["text"]
        await T(cb_update(OWNER, "p:n", plans["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "how often" in c["params"]["text"], "cycles")
        await T(cb_update(OWNER, "p:c:3", plans["message_id"]))
        await wait_sent(to(OWNER, "amount per quarter"), "amount asked")
        await T(text_update(OWNER, "300k"))
        pick = await wait_sent(to(OWNER, "Choose who pays <b>300,000đ/quarter"), "plan member picker")
        for d in ("k:t:2", "k:t:3", "k:ok"):
            await T(cb_update(OWNER, d, pick["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Plan saved" in c["params"]["text"]
                        and "Members (2): Cường, Dũng" in c["params"]["text"], "quarter plan saved")
        await wait_sent(to(OWNER, "From which period does <b>300,000đ/quarter</b> apply to <b>Cường, Dũng</b>"),
                        "new members are asked how long")
        await T(text_update(OWNER, "/plans 150k month"))
        pick = await wait_sent(to(OWNER, "Choose who pays <b>150,000đ/month"), "quick plan picker")
        await T(cb_update(OWNER, "k:t:0", pick["message_id"]))
        await T(cb_update(OWNER, "k:ok", pick["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Members (1): An" in c["params"]["text"],
                        "monthly plan saved")
        await T(text_update(OWNER, "/plans 1 fortnight"))
        await wait_sent(to(OWNER, "quick create"), "bad cycle explained")
        await T(text_update(OWNER, "/status"))
        await wait_sent(lambda c: to(OWNER, "300,000đ/quarter × 2")(c) and "150,000đ/month × 1" in c["params"]["text"],
                        "status lists the fees")

        # 11. other income and backdated records
        await T(text_update(OWNER, "/income 2m Championship prize"))
        await wait_sent(to(OWNER, "Income recorded. Fund balance"), "income recorded")
        await wait_sent(to(MEMBER, "Income</b>: 2,000,000đ · Championship prize"), "income announced")
        assert not sent(to(OWNER, "Income</b>: 2,000,000đ")), "whoever records it isn't told again"
        await T(text_update(OWNER, "/expense 100k Balls 01/01"))
        await wait_sent(to(OWNER, r"Expense recorded on 01/01/\d{4}"), "backdated expense")
        await T(text_update(OWNER, "/income 5k Bad day 31/02"))
        await wait_sent(to(OWNER, "That date isn't valid"), "impossible date refused")
        # a bare command explains the format and takes the answer as a reply
        await T(text_update(OWNER, "/money"))
        await wait_sent(lambda c: to(OWNER, "Writing amounts")(c) and "1tr5" in c["params"]["text"]
                        and "15-10-26" in c["params"]["text"], "/money explains amounts and dates")
        await T(text_update(OWNER, "/expense 1tr5 Kit 03-02-26"))
        await wait_sent(to(OWNER, "Expense recorded on 03/02/2026"), "1tr5 and dd-mm-yy")
        await T(text_update(OWNER, "/income 300k Old balance 09/2026"))
        await wait_sent(to(OWNER, "Income recorded on 01/09/2026"), "mm/yyyy means the 1st")
        await T(text_update(OWNER, "/income"))
        guide = await wait_sent(to(OWNER, "Reply to this message as"), "income guide")
        assert "15-10-2026" in guide["params"]["text"] and "No date = now" in guide["params"]["text"]
        await T(text_update(OWNER, "500k Sponsor 02-01-2026"))
        await wait_sent(to(OWNER, "Income recorded on 02/01/2026"), "income from the reply, with its date")
        await T(text_update(OWNER, "/expense lots"))
        await wait_sent(lambda c: to(OWNER, "Not quite")(c) and "Reply to this message as" in c["params"]["text"],
                        "malformed expense shows the guide again")
        await T(text_update(OWNER, "120k Water"))
        await wait_sent(to(OWNER, r"Expense recorded\. Fund balance"), "expense from the reply, now")
        await T(text_update(OWNER, "/fund"))
        fund = await wait_sent(to(OWNER, "Team fund|Fund"), "fund")
        assert "other income" not in fund["params"]["text"].lower(), "one kind of income only"
        await T(text_update(OWNER, "/history"))
        await wait_sent(to(OWNER, r"Championship prize \(income\)"), "history labels income")
        await wait_sent(to(OWNER, r"Every transaction of one month: <code>/history \d\d/\d{4}</code>"), "history shows how to pick a month")
        await T(text_update(OWNER, "/expense 50k Water 03-02-2026"))
        await wait_sent(to(OWNER, "Expense recorded on 03/02/2026"), "dd-mm-yyyy date")
        await T(text_update(OWNER, "/history 02/2026"))
        await wait_sent(lambda c: to(OWNER, "Transactions in 02/2026</b> \\(2\\)")(c) and "Water" in c["params"]["text"],
                        "history of one month")
        await T(text_update(OWNER, "/history 13/2026"))
        await wait_sent(to(OWNER, "every transaction of that month"), "bad month explained")

        # 12. merged commands: default fee in /plans, split in /charge, add in /members
        await T(text_update(OWNER, "/plans"))
        plans = await wait_sent(to(OWNER, "Fee plans"), "plans again")
        assert "p:f" in buttons(plans)
        await T(cb_update(OWNER, "p:f", plans["message_id"]))
        await T(cb_update(OWNER, "p:fa", plans["message_id"]))
        await wait_sent(to(OWNER, "Send the default package's fee"), "default fee asked")
        await T(text_update(OWNER, "250k"))
        await wait_sent(to(OWNER, "Default package set to 250,000đ/month"), "default fee set from /plans")
        dp = await wait_sent(to(OWNER, "Default: <b>250,000đ/month</b>"), "plans shown with the new default")
        # the default package's cycle: quarterly, then back to monthly
        await T(cb_update(OWNER, "p:f", dp["message_id"]))
        await T(cb_update(OWNER, "p:fk", dp["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "How often is the default package paid" in c["params"]["text"],
                        "cycle asked")
        await T(cb_update(OWNER, "p:fkc:3", dp["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Default package</b>: 250,000đ/quarter" in c["params"]["text"],
                        "default package per quarter")
        await T(cb_update(OWNER, "p:fk", dp["message_id"]))
        await T(cb_update(OWNER, "p:fkc:1", dp["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Default package</b>: 250,000đ/month" in c["params"]["text"],
                        "back to monthly")
        await T(text_update(OWNER, "/charge 100k Jersey"))
        how = await wait_sent(to(OWNER, "Tap the names of who pays"), "charge: pick people, then how")
        assert buttons(how)[-4:] == ["k:a", "k:ok:1", "k:ok:2", "k:x"] and not any(
            b["text"].startswith("✅") for row in how["params"]["reply_markup"]["inline_keyboard"] for b in row), "nobody ticked"
        await T(cb_update(OWNER, "k:a", how["message_id"]))  # 👥 whole team
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Tap the names" in c["params"]["text"]
                        and any("Split 100,000đ ·" in b["text"] for row in c["params"]["reply_markup"]["inline_keyboard"]
                                for b in row), "whole team ticked, the split shows each share")
        await T(cb_update(OWNER, "k:ok:2", how["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Jersey" in c["params"]["text"]
                        and "Tap the names" not in c["params"]["text"], "split charged")
        await T(text_update(OWNER, "/members"))
        lst = await wait_sent(to(OWNER, r"Members</b>"), "members with add button")
        assert "m:add" in buttons(lst)
        await T(cb_update(OWNER, "m:add", lst["message_id"]))
        await wait_sent(to(OWNER, "one per line"), "add prompt from /members")
        await T(text_update(OWNER, "Giang"))
        await wait_sent(to(OWNER, "Added 1"), "member added from the button")

        # 13. cash that differs from what's owed: Giang owes 250k (the default fee) and pays 100k
        await T(text_update(OWNER, "/markpaid"))
        mp = await wait_sent(to(OWNER, "Who paid in cash"), "markpaid picker")
        labels = [b["text"] for row in mp["params"]["reply_markup"]["inline_keyboard"] for b in row]
        idx = next(i for i, x in enumerate(labels) if "Giang" in x)
        await T(cb_update(OWNER, f"k:t:{idx}", mp["message_id"]))
        await T(cb_update(OWNER, "k:ok", mp["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "<b>Giang</b> owes 250,000đ" in c["params"]["text"],
                        "how much was received")
        await T(cb_update(OWNER, "mp:other", mp["message_id"]))
        await wait_sent(to(OWNER, "Send the amount received from Giang"), "amount asked")
        await T(text_update(OWNER, "abc"))
        await wait_sent(to(OWNER, "That amount doesn't work"), "not an amount refused")
        await T(text_update(OWNER, "100k"))
        rest = await wait_sent(to(OWNER, "paid 100,000đ of 250,000đ"), "what about the rest")
        assert buttons(rest) == ["mp:settle", "mp:owe", "mp:x"]
        await T(cb_update(OWNER, "mp:settle", rest["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Cash recorded: Giang (100,000đ)" in c["params"]["text"]
                        and "150,000đ is let go" in c["params"]["text"], "settled at 100k")
        await wait_sent(to(MEMBER, r"Giang</b> paid 100,000đ"), "followers see the real amount")
        # /remind goes to followers; /notify turns them off
        await T(text_update(OWNER, "/remind"))
        await wait_sent(to(OWNER, "Sent the unpaid list to 1 people"), "remind sent to followers")
        await T(text_update(MEMBER, "/notify"))
        await wait_sent(to(MEMBER, "announcements are off"), "notify off")
        await T(text_update(OWNER, "/followers"))
        fl = await wait_sent(to(OWNER, "Opened the bot"), "managers see who opened the bot")
        assert "announcements off" in fl["params"]["text"], fl["params"]["text"]
        await T(text_update(MEMBER, "/followers"))
        await wait_sent(to(MEMBER, "manager"), "members can't")
        await T(text_update(OWNER, "/remind"))
        await wait_sent(to(OWNER, "Sent the unpaid list to 0 people"), "muted follower skipped")
        await T(text_update(MEMBER, "/notify"))
        await wait_sent(to(MEMBER, "announcements are on"), "notify on")

        # 14. each package's deadline and late fee, in /plans
        await T(text_update(OWNER, "/setdue 15 5"))
        await asyncio.sleep(0.3)  # /setdue is gone: no reply
        await T(text_update(OWNER, "/plans"))
        pl = await wait_sent(to(OWNER, "Fee plans"), "plans with terms")
        assert "📅 announced on day 1, due +5 days · ⏰ no late fee" in pl["params"]["text"], pl["params"]["text"]
        assert buttons(pl)[0] == "p:f"
        await T(cb_update(OWNER, "p:f", pl["message_id"]))
        dv = await wait_sent(lambda c: c["method"] == "editMessageText" and "Default package" in c["params"]["text"],
                             "default package view")
        assert buttons(dv)[:4] == ["p:fa", "p:fk", "p:ds:0", "p:dl:0"]
        await T(cb_update(OWNER, "p:ds:0", pl["message_id"]))
        await wait_sent(to(OWNER, "Send the <b>announcement day</b>"), "deadline asked")
        await T(text_update(OWNER, "15 5"))
        await wait_sent(lambda c: to(OWNER, "Saved")(c) and "announced on day 15, due +5 days" in c["params"]["text"],
                        "default deadline saved")
        await T(cb_update(OWNER, "p:dl:0", pl["message_id"]))
        await wait_sent(to(OWNER, "late fee per overdue day"), "late fee asked")
        await T(text_update(OWNER, "1k"))
        await wait_sent(lambda c: to(OWNER, "Saved")(c) and "late fee 1,000đ/day" in c["params"]["text"],
                        "default late fee saved")
        # the quarterly plan follows the default until it gets its own
        await T(text_update(OWNER, "/plans"))
        pl = await wait_sent(lambda c: to(OWNER, "Fee plans")(c) and "deadline and late fee as the default package" in c["params"]["text"],
                             "plans follow the default")
        plan_btn = next(b for b in buttons(pl) if b.startswith("p:v:"))
        pid = plan_btn.split(":")[2]
        await T(cb_update(OWNER, f"p:dl:{pid}", pl["message_id"]))
        await wait_sent(to(OWNER, "to use the default package"), "plan late fee asked, with the default option")
        await T(text_update(OWNER, "2k"))
        await wait_sent(lambda c: to(OWNER, "Saved")(c) and "late fee 2,000đ/day" in c["params"]["text"]
                        and "late fee 2,000đ/day (as default)" not in c["params"]["text"], "plan late fee saved")
        await T(text_update(OWNER, "/status"))
        await wait_sent(lambda c: to(OWNER, "due ")(c) and "late fee 2,000đ/day" in c["params"]["text"]
                        and "late fee 1,000đ/day" in c["params"]["text"], "status lists each package's terms")

        # 15. undo any recent action
        await T(text_update(OWNER, "/expense 10k Typo"))
        await wait_sent(to(OWNER, r"Expense recorded\. Fund balance"), "expense to undo")
        await T(text_update(OWNER, "/income 5k Keep"))
        await wait_sent(to(OWNER, r"Income recorded\. Fund balance"), "a later action")
        await T(text_update(OWNER, "/undo"))
        lst = await wait_sent(to(OWNER, "Pick an action to undo"), "undo list")
        labels = [b["text"] for row in lst["params"]["reply_markup"]["inline_keyboard"] for b in row]
        assert labels[0].startswith("Income · Keep") and labels[1].startswith("Expense · Typo"), labels
        typo = buttons(lst)[1]
        await T(cb_update(OWNER, typo, lst["message_id"]))  # the one in the middle
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Undo this action?" in c["params"]["text"]
                        and "Expense · Typo · 10,000đ" in c["params"]["text"], "confirm")
        await T(cb_update(OWNER, "un:y:" + typo[3:], lst["message_id"]))
        done = await wait_sent(lambda c: c["method"] == "editMessageText" and "Undone: Expense · Typo" in c["params"]["text"],
                               "undone")
        assert buttons(done) == ["un:l"]
        await wait_sent(to(MEMBER, "Undone: Expense · Typo · 10,000đ"), "followers told about the undo")
        await T(cb_update(OWNER, "un:y:" + typo[3:], lst["message_id"]))  # the same button again
        await until(lambda: any(c["method"] == "answerCallbackQuery" and "already undone" in (c["params"].get("text") or "")
                                for c in calls), "an undo can't happen twice")
        await T(cb_update(OWNER, "un:l", lst["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Pick an action to undo" in c["params"]["text"]
                        and "Typo" not in json.dumps(c["params"].get("reply_markup")), "the list without it")

        # 16. how long a package applies, in whole periods: Cường for 2027Q1–Q2, Dũng from next quarter, then this one only
        from datetime import date as _date
        today = _date.today()
        qn = lambda y, q: f"{y + (q - 1) // 4}Q{(q - 1) % 4 + 1}"  # noqa: E731
        cur_q = (today.month - 1) // 3 + 1
        this_q, next_q, after_q = qn(today.year, cur_q), qn(today.year, cur_q + 1), qn(today.year, cur_q + 2)
        await T(text_update(OWNER, "/plans"))
        plans = await wait_sent(to(OWNER, "Fee plans"), "plans for windows")
        quarter = next(b["callback_data"] for row in plans["params"]["reply_markup"]["inline_keyboard"] for b in row
                       if "quarter" in b["text"])
        await T(cb_update(OWNER, quarter, plans["message_id"]))
        view = await wait_sent(lambda c: c["method"] == "editMessageText" and "Members (2)" in c["params"]["text"],
                               "quarter plan view")
        win = next(d for d in buttons(view) if d.startswith("p:w:"))

        qm = lambda y, q: f"w:{{}}{y + (q - 1) // 4}{((q - 1) % 4) * 3 + 1:02d}"  # noqa: E731  (first month)

        async def set_window(index, *steps):
            await T(cb_update(OWNER, win, view["message_id"]))
            pick = await wait_sent(to(OWNER, "set the months it applies"), "window picker")
            await T(cb_update(OWNER, f"k:t:{index}", pick["message_id"]))
            mark = len(calls)
            await T(cb_update(OWNER, "k:ok", pick["message_id"]))
            menu = await until(lambda: next((c for c in calls[mark:] if c["method"] == "editMessageText"
                                             and "From which period" in c["params"]["text"]), None), "window menu")
            assert buttons(menu) == ["w:next", "w:cur", "w:only", "w:in", "w:x"], buttons(menu)
            for d in steps:
                mark = len(calls)
                if d.startswith("type:"):
                    await T(text_update(OWNER, d[5:]))
                    continue
                await T(cb_update(OWNER, d, pick["message_id"]))
                shown = await until(lambda: next((c for c in calls[mark:] if c["method"] == "editMessageText"), None), d)
                if d == "w:in":
                    assert "first period" in shown["params"]["text"]
                    guide = await wait_sent(to(OWNER, "Or type the range in quarters"), "typing guide")
                    assert guide["params"]["reply_markup"].get("force_reply"), guide["params"]
                    assert buttons(shown)[0] == qm(today.year, cur_q).format("s")
                    labels = [b["text"] for row in shown["params"]["reply_markup"]["inline_keyboard"] for b in row]
                    assert labels[:2] == [this_q, next_q], labels
                elif d.startswith("w:s"):
                    assert "last period" in shown["params"]["text"] and buttons(shown)[0] == "w:e0"

        def saved(text):
            return lambda c: "Months set: " in (c["params"].get("text") or "") and text in c["params"]["text"]

        end_of = lambda y, q: f"w:e{y + (q - 1) // 4}{((q - 1) % 4) * 3 + 3:02d}"  # noqa: E731
        await set_window(0, "w:in", qm(today.year, cur_q + 1).format("s"), end_of(today.year, cur_q + 2))
        await wait_sent(saved(f"Cường ({next_q}–{after_q})"), "two quarters")
        await set_window(1, "w:next")
        await wait_sent(saved(f"Dũng (from {next_q})"), "from next quarter")
        await set_window(1, "w:in", f"type:{this_q} - {next_q}")
        await wait_sent(saved(f"Dũng (until {next_q})"), "typed range")
        await set_window(0, "w:in", qm(today.year, cur_q + 1).format("s"), f"type:{after_q}")
        await wait_sent(lambda c: saved(f"Cường ({next_q}–{after_q})")(c) and c["method"] == "sendMessage", "typed end")
        await set_window(1, "w:only")
        await wait_sent(lambda c: saved(f"Dũng (until {this_q})")(c) and f"Months set: {this_q}." in c["params"]["text"],
                        "this quarter only")

        # 16b. a second package for An overlaps her monthly one: warned, not added, asked again
        await T(text_update(OWNER, "/plans 100k month"))
        pick = await wait_sent(to(OWNER, "Choose who pays <b>100,000đ/month"), "second package picker")
        await T(cb_update(OWNER, "k:t:0", pick["message_id"]))
        await T(cb_update(OWNER, "k:ok", pick["message_id"]))
        warn = await wait_sent(lambda c: to(OWNER, "Overlaps another package")(c) and "An: 150,000đ/month" in c["params"]["text"],
                               "overlap warned")
        menu = await wait_sent(to(OWNER, "From which period does <b>100,000đ/month</b> apply to <b>An</b>"), "asked for periods")
        await T(cb_update(OWNER, "w:next", menu["message_id"]))
        await until(lambda: len(sent(lambda c: to(OWNER, "Overlaps another package")(c))) >= 2, "still overlapping: warned again")
        await T(cb_update(OWNER, "w:x", menu["message_id"]))

        # 16b2. packages from An's member card: add a new one from next period, then remove it again
        await T(text_update(OWNER, "/members"))
        lst = await wait_sent(lambda c: to(OWNER, r"Members</b>")(c) and c["method"] == "sendMessage", "members list again")
        an = next(b["callback_data"] for row in lst["params"]["reply_markup"]["inline_keyboard"] for b in row
                  if b["text"].startswith("An"))
        aid = an.split(":")[2]
        await T(cb_update(OWNER, f"m:pk:{aid}", lst["message_id"]))
        await wait_sent(lambda c: "An's packages" in c["params"].get("text", "") and "150,000đ/month" in c["params"].get("text", ""),
                        "member packages")
        await T(cb_update(OWNER, f"m:pn:{aid}", lst["message_id"]))
        await wait_sent(to(OWNER, "Type the amount and cycle"), "new package asked")
        await T(text_update(OWNER, "90k month"))
        menu = await wait_sent(to(OWNER, "From which period does <b>90,000đ/month</b> apply to <b>An</b>"),
                               "periods for the new package")
        await T(cb_update(OWNER, "w:x", menu["message_id"]))
        # shorten the 150k one to this month, then the 90k one fits from next month
        plan150 = next(c for c in [await wait_sent(lambda c: "An's packages" in c["params"].get("text", ""), "packages")])
        pid150 = next(b["callback_data"] for row in plan150["params"]["reply_markup"]["inline_keyboard"] for b in row
                      if "150,000" in b["text"]).split(":")[3]
        await T(cb_update(OWNER, f"m:pw:{aid}:{pid150}", lst["message_id"]))
        await wait_sent(to(OWNER, "From which period does <b>150,000đ/month</b> apply to <b>An</b>"), "periods for 150k")
        await T(cb_update(OWNER, "w:only", lst["message_id"]))
        await wait_sent(lambda c: "Months set:" in c["params"].get("text", "") and "An's packages" in c["params"].get("text", ""),
                        "back on An's packages")
        await T(cb_update(OWNER, f"m:pa:{aid}", lst["message_id"]))
        add = await wait_sent(lambda c: "Add a package for <b>An</b>" in c["params"].get("text", ""), "pick a package")
        join = next(b["callback_data"] for row in add["params"]["reply_markup"]["inline_keyboard"] for b in row
                    if "90,000" in b["text"])
        await T(cb_update(OWNER, join, lst["message_id"]))
        await T(cb_update(OWNER, "w:next", lst["message_id"]))
        done = await wait_sent(lambda c: "Months set:" in c["params"].get("text", "") and "90,000đ/month" in c["params"].get("text", "")
                               and "An's packages" in c["params"].get("text", "") and "Overlaps" not in c["params"].get("text", ""),
                               "second package added from the card")
        pid90 = join.split(":")[3]
        await T(cb_update(OWNER, f"m:pd:{aid}:{pid90}", lst["message_id"]))
        await T(cb_update(OWNER, f"m:pdy:{aid}:{pid90}", lst["message_id"]))
        await wait_sent(lambda c: "Package removed" in c["params"].get("text", "") and "90,000" not in c["params"].get("text", ""),
                        "package removed from the card")

        # 16c. /guide: short how-to with the pay page link, and a hint for managers to forward it
        await T(text_update(OWNER, "/guide"))
        await wait_sent(lambda c: to(OWNER, "How to pay the team fund")(c) and "/p/" in c["params"]["text"]
                        and "Forward this message" in c["params"]["text"], "guide sent")

        # 17. the match calendar: paste lines, cancel with a reason (followers told), a weekly calendar, show more
        from datetime import timedelta as _td
        soon = today + _td(days=10)
        await T(text_update(OWNER, f"/matches\n26/10: Tăng High (600k)\n{soon:%d/%m/%Y} 19h FC Moreh @Pitch A\nhello"))
        prev = await wait_sent(to(OWNER, "Read <b>2</b> matches"), "paste preview")
        assert "Skipped (no date)" in prev["params"]["text"] and "FC Moreh · @Pitch A" in prev["params"]["text"]
        await T(cb_update(OWNER, "mt:ok", prev["message_id"]))
        await wait_sent(to(OWNER, "Saved 2 matches"), "matches saved")
        lst = await wait_sent(to(OWNER, "Upcoming matches"), "match list")
        assert "FC Moreh" in lst["params"]["text"] and "Tăng High" not in lst["params"]["text"], "past ones aren't upcoming"
        assert "mt:a" in buttons(lst)
        await T(text_update(OWNER, f"/matches cancel {soon:%d/%m} rain"))
        await wait_sent(to(OWNER, "Match cancelled. Everyone was told"), "cancelled")
        await wait_sent(lambda c: to(MEMBER, "Match cancelled")(c) and "Reason: rain" in c["params"]["text"],
                        "followers told about the cancellation")
        await T(text_update(OWNER, "/matches"))
        lst = await wait_sent(to(OWNER, "Upcoming matches"), "list again")
        assert "/matches cancel 30/11" in lst["params"]["text"], "managers get the manager tip"
        assert "<s>" in lst["params"]["text"] and "🚫 rain" in lst["params"]["text"]
        assert any(d.startswith("mt:v:") for d in buttons(lst)) and not any(d.startswith("mt:e:") for d in buttons(lst)), \
            "each match is its own button; no separate Edit step"
        await T(cb_update(OWNER, "mt:s", lst["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Repeating calendars" in c["params"]["text"], "calendars")
        for d in ("mt:sn", "mt:sf:weekly", "mt:sw:weekly:6"):
            await T(cb_update(OWNER, d, lst["message_id"]))
        await wait_sent(to(OWNER, "Send them, e.g."), "usual time asked")
        await T(text_update(OWNER, "19h @Pitch A"))
        await wait_sent(to(OWNER, "Repeating calendar set"), "calendar saved")
        lst = await wait_sent(lambda c: to(OWNER, "Upcoming matches")(c) and "every Sunday, 19:00 @Pitch A" in c["params"]["text"],
                              "calendar listed")
        assert lst["params"]["text"].count("• ") >= 5 and "mt:l:10" in buttons(lst)
        await T(cb_update(OWNER, "mt:l:10", lst["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "(next 10)" in c["params"]["text"], "show 10")
        # add one match step by step: a suggested day, the usual time and pitch, an opponent typed
        await T(cb_update(OWNER, "mt:a", lst["message_id"]))
        days = await wait_sent(to(OWNER, "Add a match"), "day buttons")
        await T(cb_update(OWNER, buttons(days)[0], days["message_id"]))
        tm = await wait_sent(to(OWNER, "time and pitch\\?"), "time step")
        assert "mt:at:d" in buttons(tm), "the calendar's usual time is offered"
        await T(cb_update(OWNER, "mt:at:d", tm["message_id"]))
        await wait_sent(to(OWNER, "Against which team"), "opponent step")
        await wait_sent(to(OWNER, "Or type the team's name"), "opponent prompt")
        await T(text_update(OWNER, "FC Moreh"))
        added = await wait_sent(to(OWNER, "Match added"), "added step by step")
        assert "FC Moreh · @Pitch A" in added["params"]["text"] and "19:00" in added["params"]["text"]
        assert any(d.startswith("ct:v:") for d in buttons(added)) is False  # not in contacts yet: offer to save
        assert any(d.startswith("mt:sc:") for d in buttons(added))

        # money after a played match: the 26/10 one, we paid 300k
        await T(cb_update(OWNER, "mt:p", lst["message_id"]))
        pastv = await wait_sent(lambda c: c["method"] == "editMessageText" and "Past matches" in c["params"]["text"],
                                "past matches")
        pm = next(d for d in buttons(pastv) if d.startswith("mt:v:"))
        await T(cb_update(OWNER, pm, pastv["message_id"]))
        card = await wait_sent(lambda c: c["method"] == "editMessageText" and "Tăng High" in c["params"]["text"]
                               and "mt:mp:" in json.dumps(c["params"].get("reply_markup")), "a played match's card")
        await T(cb_update(OWNER, pm.replace("mt:v:", "mt:mp:"), pastv["message_id"]))
        await wait_sent(to(OWNER, "Spending for"), "amount asked")
        await T(text_update(OWNER, "300k pitch"))
        await wait_sent(lambda c: to(OWNER, "Recorded 300,000đ spent")(c) and "Spent: 300,000đ" in c["params"]["text"],
                        "recorded on the match")
        await wait_sent(to(MEMBER, "Trận 26/10 vs Tăng High: pitch"), "followers see the expense")
        # the same played match: two players pay 40k each for it
        await T(cb_update(OWNER, pm.replace("mt:v:", "mt:f:"), pastv["message_id"]))
        await wait_sent(to(OWNER, "How much does each player pay"), "match fee asked")
        await T(text_update(OWNER, "40k"))
        fp = await wait_sent(to(OWNER, "Who plays this match"), "players picker")
        await T(cb_update(OWNER, "k:t:0", fp["message_id"]))
        await T(cb_update(OWNER, "k:t:1", fp["message_id"]))
        await T(cb_update(OWNER, "k:ok", fp["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "Charged 2 players 40,000đ each" in c["params"]["text"]
                        and "Match fee: 2 players, 0 paid" in c["params"]["text"], "players charged, shown on the card")
        await wait_sent(to(MEMBER, "Trận 26/10 vs Tăng High</b>: 40,000đ each"), "followers told")
        await T(cb_update(OWNER, pm.replace("mt:v:", "mt:f:"), pastv["message_id"]))
        again = await wait_sent(lambda c: to(OWNER, "How much does each player pay")(c)
                                and "mt:fa:" in json.dumps(c["params"].get("reply_markup")), "last amount one tap away")
        await T(cb_update(OWNER, next(d for d in buttons(again) if d.startswith("mt:fa:")), again["message_id"]))
        rest = await wait_sent(lambda c: to(OWNER, "Who plays this match")(c) and c is not fp, "only those not charged yet")
        assert len([b for b in buttons(rest) if b.startswith("k:t:")]) == len([b for b in buttons(fp) if b.startswith("k:t:")]) - 2
        await T(cb_update(OWNER, "k:x", rest["message_id"]))

        FAN = 777001  # someone who just opened the bot
        await T(text_update(FAN, "/matches"))
        mem = await wait_sent(to(FAN, "Upcoming matches"), "everyone sees the calendar")
        assert "mt:a" not in buttons(mem)

        # 18. contacts: paste, search, open one with its match history
        await T(text_update(OWNER, "/contacts\nFC Moreh (0912 345 678, fb: Dung)\nFC Soc Son"))
        prev = await wait_sent(to(OWNER, "Read <b>2</b> teams"), "contacts preview")
        assert "FC Moreh</b> · 📞 0912 345 678 · fb: Dung" in prev["params"]["text"]
        await T(cb_update(OWNER, "ct:ok", prev["message_id"]))
        await wait_sent(to(OWNER, "Saved: 2 new, 0 updated"), "contacts saved")
        await T(text_update(OWNER, "/contacts moreh"))
        found = await wait_sent(to(OWNER, '"moreh": 1 found'), "search")
        assert "• <b>FC Moreh</b> · 📞 0912 345 678" in found["params"]["text"], "phones show in the list"
        assert "💡" not in found["params"]["text"], "no tip when the command came with arguments"
        cid = next(d for d in buttons(found) if d.startswith("ct:v:"))
        await T(cb_update(OWNER, cid, found["message_id"]))
        card = await wait_sent(lambda c: c["method"] == "editMessageText" and "Played 0 · cancelled 1" in c["params"]["text"],
                               "contact with history")
        assert [d.split(":")[1] for d in buttons(card)] == ["en", "ph", "nt", "d", "l"], buttons(card)
        assert "📞 0912 345 678" in card["params"]["text"]
        await T(cb_update(OWNER, cid.replace("ct:v:", "ct:ph:"), found["message_id"]))
        await wait_sent(to(OWNER, "Send the phone number"), "phone asked")
        await T(text_update(OWNER, "+84 98 765 4321"))
        await wait_sent(lambda c: to(OWNER, "Updated")(c) and "📞 0987 654 321" in c["params"]["text"], "phone saved")
        await T(cb_update(OWNER, cid.replace("ct:v:", "ct:nt:"), found["message_id"]))
        await wait_sent(to(OWNER, "Send the note"), "note asked")
        await T(text_update(OWNER, "zalo: Dung, sân Mỹ Đình"))
        await wait_sent(lambda c: to(OWNER, "Updated")(c) and "📝 zalo: Dung, sân Mỹ Đình" in c["params"]["text"], "note saved")
        # ➕ one team: the name saves it; phone and note are optional buttons, then Done
        await T(cb_update(OWNER, "ct:l", found["message_id"]))
        await T(cb_update(OWNER, "ct:a", found["message_id"]))
        await wait_sent(to(OWNER, "Send the team's name"), "name asked")
        await T(text_update(OWNER, "FC WQ"))
        new = await wait_sent(to(OWNER, "Saved to contacts"), "saved at once")
        wq = next(d.split(":")[2] for d in buttons(new) if d.startswith("ct:aph:"))
        assert buttons(new) == [f"ct:aph:{wq}", f"ct:ant:{wq}", f"ct:ok1:{wq}"], buttons(new)
        await T(cb_update(OWNER, f"ct:aph:{wq}", new["message_id"]))
        await wait_sent(to(OWNER, "Send the phone number"), "phone asked only when tapped")
        await T(text_update(OWNER, "0911 222 333"))
        upd = await wait_sent(lambda c: to(OWNER, "Updated")(c) and "📞 0911 222 333" in c["params"]["text"], "phone added")
        assert f"ct:ok1:{wq}" in buttons(upd), "still the short card"
        await T(cb_update(OWNER, f"ct:ok1:{wq}", upd["message_id"]))
        await wait_sent(lambda c: c["method"] == "editMessageText" and "<b>FC WQ</b> saved to contacts" in c["params"]["text"], "done")

        await T(text_update(FAN, "/contacts"))
        fan = await wait_sent(to(FAN, "Opposing teams"), "everyone can look contacts up")
        assert "ct:a" not in buttons(fan), "only managers add"
        await T(cb_update(FAN, next(d for d in buttons(fan) if d.startswith("ct:v:")), fan["message_id"]))
        card = await wait_sent(lambda c: c["method"] == "editMessageText" and str(c["params"].get("chat_id")) == str(FAN)
                               and "📇" in c["params"]["text"], "a member opens a contact")
        assert buttons(card) == ["ct:l"], "read-only card"
        await T(text_update(FAN, "/contacts\nFC Spam (0900 000 000)"))
        await wait_sent(to(FAN, "manager"), "members can't add")

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
