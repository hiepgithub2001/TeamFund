import os
import tempfile

from cryptography.fernet import Fernet

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_db.name}",
    "INTERNAL_KEY": "k",
    "TOKEN_ENC_KEY": Fernet.generate_key().decode(),
    "MASTER_ADMIN_IDS": "1",
    "PUBLIC_URL": "https://example.test",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import events  # noqa: E402
from app import scheduler  # noqa: E402

SUPER, OWNER, MANAGER, MEMBER = 1, 100, 200, 300
sent: list[dict] = []


@pytest.fixture(scope="module")
def c():
    events.emit = lambda e: sent.append(e) or True  # capture instead of calling the gateway
    import app.routers.sepay as sp
    sp.emit = events.emit
    scheduler.emit = events.emit
    with TestClient(app) as client:
        yield client


def call(c, method, path, actor=None, **kw):
    headers = {"X-Internal-Key": "k"}
    if actor:
        headers["X-Actor-Id"] = str(actor)
    return c.request(method, "/api" + path, headers=headers, **kw)


def ok(r):
    assert r.status_code < 300, r.text
    return r.json()


def code(r):
    return r.json()["detail"]["code"]


def test_internal_key_required(c):
    assert c.get("/api/teams/mine", headers={"X-Actor-Id": "1"}).status_code == 401


def test_full_flow(c):
    for uid, name in ((OWNER, "Owner"), (MANAGER, "Mgr"), (MEMBER, "Mem")):
        ok(call(c, "POST", "/users/upsert", json={"tg_id": uid, "first_name": name}))

    # team registration needs the super admin's approval
    assert code(call(c, "POST", "/teams", OWNER, json={"token": "1:x", "bot_id": 555, "bot_username": "fc_bot"})) == "not_approved"
    req = ok(call(c, "POST", "/team-requests", OWNER, json={"team_name": "FC Test"}))
    assert req["admin_ids"] == [SUPER]
    assert code(call(c, "POST", "/team-requests", OWNER, json={"team_name": "again"})) == "request_pending"
    assert code(call(c, "POST", f"/team-requests/{req['id']}/decide", OWNER, json={"approve": True})) == "not_super"
    ok(call(c, "POST", f"/team-requests/{req['id']}/decide", SUPER, json={"approve": True}))
    team = ok(call(c, "POST", "/teams", OWNER, json={"token": "1:secret", "bot_id": 555, "bot_username": "fc_bot"}))
    assert team["name"] == "FC Test" and team["token"] == "1:secret"
    tid = team["team_id"]
    assert ok(call(c, "GET", "/internal/bots"))[0]["bot_id"] == 555
    t = f"/teams/{tid}"

    # roles
    assert code(call(c, "PATCH", t + "/settings", MEMBER, json={"fee": 1})) == "not_manager"
    mr = ok(call(c, "POST", t + "/manager-requests", MANAGER))
    assert mr["owner_tg_id"] == OWNER
    assert code(call(c, "POST", t + "/manager-requests", MANAGER)) == "request_pending"
    assert code(call(c, "POST", t + f"/manager-requests/{mr['id']}/decide", MANAGER, json={"approve": True})) == "not_owner"
    ok(call(c, "POST", t + f"/manager-requests/{mr['id']}/decide", OWNER, json={"approve": True}))
    assert ok(call(c, "GET", t + "/role", MANAGER))["role"] == "manager"
    assert code(call(c, "PUT", t + "/bank", MANAGER, json={"bank_name": "MBBank", "bank_account": "0123", "bank_holder": "x"})) == "not_owner"

    # setup
    ok(call(c, "PUT", t + "/bank", OWNER, json={"bank_name": "MBBank", "bank_account": "0123456", "bank_holder": "nguyen van a"}))
    key = ok(call(c, "POST", t + "/sepay-key", OWNER))["api_key"]
    ok(call(c, "PATCH", t + "/settings", MANAGER, json={"fee": 200000, "due_day": 5}))
    ms = ok(call(c, "POST", t + "/members", MANAGER, json={"members": [{"name": "An"}, {"name": "Bình"}, {"name": "Cường"}]}))
    an, binh, cuong = (m["id"] for m in ms)

    unpaid = ok(call(c, "GET", t + "/unpaid"))
    assert [u["name"] for u in unpaid] == ["An", "Bình", "Cường"] and all(u["total"] == 200000 for u in unpaid)
    ok(call(c, "GET", t + "/unpaid"))  # idempotent
    assert len(ok(call(c, "GET", t + "/unpaid"))[0]["bills"]) == 1

    # fee change reprices this month's unpaid bills
    ok(call(c, "PATCH", t + "/settings", MANAGER, json={"fee": 250000}))
    assert ok(call(c, "GET", t + "/unpaid"))[0]["total"] == 250000

    # pay for two people with SePay
    intent = ok(call(c, "POST", t + "/intents", MEMBER, json={"member_ids": [an, binh]}))
    assert intent["amount"] == 500000 and intent["code"].startswith("TF") and "qr.sepay.vn" in intent["qr_url"]
    payload = {"id": 9001, "transferType": "in", "transferAmount": 500000,
               "content": f"NGUYEN VAN X chuyen tien {intent['code'][:5]} {intent['code'][5:]}"}
    assert c.post(f"/sepay/{tid}", json=payload, headers={"Authorization": "Apikey wrong"}).status_code == 401
    r = c.post(f"/sepay/{tid}", json=payload, headers={"Authorization": f"Apikey {key}"})
    assert r.json() == {"success": True}
    ev = sent[-1]
    assert ev["type"] == "payment_confirmed" and ev["members"] == ["An", "Bình"] and ev["payer_tg_id"] == MEMBER
    n = len(sent)
    c.post(f"/sepay/{tid}", json=payload, headers={"Authorization": f"Apikey {key}"})  # SePay retry
    assert len(sent) == n

    st = ok(call(c, "GET", t + "/status"))
    assert st["paid"] == ["An", "Bình"] and st["unpaid"] == [{"name": "Cường", "total": 250000}]
    assert code(call(c, "POST", t + "/intents", MEMBER, json={"member_ids": [an]})) == "nothing_to_pay"

    # a transfer with no code goes to the managers, who assign it
    c.post(f"/sepay/{tid}", json={"id": 9002, "transferType": "in", "transferAmount": 250000, "content": "tien quy"},
           headers={"Authorization": f"Apikey {key}"})
    ev = sent[-1]
    assert ev["type"] == "unmatched_payment" and ev["reason"] == "no_code" and set(ev["admins"]) == {OWNER, MANAGER}
    um = ok(call(c, "GET", t + "/unmatched", MANAGER))
    assert len(um) == 1
    ok(call(c, "POST", t + f"/payments/{um[0]['payment_id']}/assign", MANAGER, json={"member_ids": [cuong]}))
    assert ok(call(c, "GET", t + "/status"))["unpaid"] == []

    # outgoing transfers are ignored
    n = len(sent)
    c.post(f"/sepay/{tid}", json={"id": 9003, "transferType": "out", "transferAmount": 1}, headers={"Authorization": f"Apikey {key}"})
    assert len(sent) == n

    # charges, split, cash, expense, fund
    ch = ok(call(c, "POST", t + "/charges", MANAGER, json={"title": "Áo đấu", "amount": 150000}))
    assert ch["count"] == 3
    sp = ok(call(c, "POST", t + "/charges", MANAGER, json={"title": "Sân", "amount": 500000, "kind": "split", "member_ids": [an, binh, cuong]}))
    assert sp["each"] == 167000
    cash = ok(call(c, "POST", t + "/markpaid", MANAGER, json={"member_ids": [an]}))
    assert cash["amount"] == 317000
    ok(call(c, "POST", t + "/expenses", MANAGER, json={"amount": 600000, "title": "Thuê sân"}))
    f = ok(call(c, "GET", t + "/fund"))
    assert f == {"income": 500000 + 250000 + 317000, "expense": 600000, "balance": 467000, "unmatched": 0}
    assert len(ok(call(c, "GET", t + "/history"))) == 4

    # member management
    ok(call(c, "PATCH", t + f"/members/{binh}", MANAGER, json={"shirt_number": "10", "fee_override": 100000}))
    assert ok(call(c, "GET", t + f"/members/{binh}"))["fee"] == 100000
    ok(call(c, "PATCH", t + f"/members/{binh}", MANAGER, json={"fee_override": None}))
    assert ok(call(c, "GET", t + f"/members/{binh}"))["fee"] == 250000
    bills = ok(call(c, "GET", t + f"/members/{binh}/bills"))["bills"]
    ok(call(c, "POST", t + f"/bills/{bills[0]['id']}/waive", MANAGER))
    ok(call(c, "DELETE", t + f"/members/{cuong}", MANAGER))
    assert [m["name"] for m in ok(call(c, "GET", t + "/members"))] == ["An", "Bình"]

    # admins
    assert code(call(c, "DELETE", t + f"/admins/{OWNER}", OWNER)) == "not_a_manager"
    ok(call(c, "POST", t + "/transfer-owner", OWNER, json={"tg_id": MANAGER}))
    assert ok(call(c, "GET", t + "/role", OWNER))["role"] == "manager"
    ok(call(c, "DELETE", t + f"/admins/{OWNER}", MANAGER))
    assert ok(call(c, "GET", t + "/role", OWNER))["role"] is None
    assert len(ok(call(c, "GET", t + "/audit", MANAGER))) > 5

    # export
    r = call(c, "GET", t + "/export", MANAGER)
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_scheduler_helpers():
    assert scheduler.should_remind(2, 5) and scheduler.should_remind(5, 5) and scheduler.should_remind(8, 5)
    assert not scheduler.should_remind(6, 5) and not scheduler.should_remind(1, 5)


def test_scheduler_jobs_run(c):
    n = len(sent)
    scheduler.job_billing()
    scheduler.job_cleanup()
    assert len(sent) == n  # no group linked, nothing announced
