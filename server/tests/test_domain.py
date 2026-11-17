"""Business rules, tested on the domain layer directly (no HTTP, no Telegram). Settings: conftest.py."""
import pytest

from app import jobs
from app.db import Base, SessionLocal, engine
from app.domain import members, money, registration, teams, users
from app.domain.errors import DomainError

SUPER, OWNER, MANAGER, MEMBER = 1, 100, 200, 300


@pytest.fixture(scope="module")
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        yield s


def code(fn, *args, **kw):
    with pytest.raises(DomainError) as e:
        fn(*args, **kw)
    return e.value.code


def test_full_flow(db):
    for uid, name in ((OWNER, "Owner"), (MANAGER, "Mgr"), (MEMBER, "Mem")):
        users.upsert_user(db, uid, name, None, None)

    # registering a team needs the super admin's approval
    assert code(registration.create_team, db, OWNER, "1:x", 555, "fc_bot", None) == "not_approved"
    req = registration.request_team(db, OWNER, "FC Test")
    assert req["admin_ids"] == [SUPER]
    assert code(registration.request_team, db, OWNER, "again") == "request_pending"
    assert code(registration.decide_team_request, db, OWNER, req["id"], True) == "not_super"
    registration.decide_team_request(db, SUPER, req["id"], True)
    team = registration.create_team(db, OWNER, "1:secret", 555, "fc_bot", None)
    assert team["name"] == "FC Test" and team["token"] == "1:secret"
    tid = team["team_id"]
    assert registration.active_bots(db)[0]["bot_id"] == 555
    assert registration.my_teams(db, OWNER)[0]["role"] == "owner"

    # roles
    assert code(teams.update_settings, db, tid, MEMBER, fee=1) == "not_manager"
    assert code(members.list_members, db, tid, MEMBER) == "not_manager"  # the roster holds phone numbers
    mr = teams.request_manager(db, tid, MANAGER)
    assert mr["owner_tg_id"] == OWNER
    assert code(teams.request_manager, db, tid, MANAGER) == "request_pending"
    assert code(teams.decide_manager_request, db, tid, MANAGER, mr["id"], True) == "not_owner"
    teams.decide_manager_request(db, tid, OWNER, mr["id"], True)
    assert teams.role(db, tid, MANAGER)["role"] == "manager"
    assert code(teams.set_bank, db, tid, MANAGER, "MBBank", "0123", "x") == "not_owner"

    # setup
    teams.set_bank(db, tid, OWNER, "MBBank", "0123456", "nguyen van a")
    assert code(teams.set_bank, db, tid, OWNER, "MBBank", "12-34", "NGUYEN") == "invalid"
    key = teams.new_sepay_key(db, tid, OWNER)["api_key"]
    teams.update_settings(db, tid, MANAGER, fee=200000, due_day=5)
    assert code(teams.update_settings, db, tid, MANAGER, due_day=31) == "invalid"
    an, binh, cuong = (m["id"] for m in members.add_members(
        db, tid, MANAGER, [{"name": "An"}, {"name": "Bình"}, {"name": "Cường"}]))

    unpaid = money.unpaid(db, tid)
    assert [u["name"] for u in unpaid] == ["An", "Bình", "Cường"] and all(u["total"] == 200000 for u in unpaid)
    assert len(money.unpaid(db, tid)[0]["bills"]) == 1  # idempotent

    # a fee change reprices this month's unpaid bills
    teams.update_settings(db, tid, MANAGER, fee=250000)
    assert money.unpaid(db, tid)[0]["total"] == 250000

    # pay for two people through SePay
    intent = money.create_intent(db, tid, MEMBER, [an, binh])
    assert intent["amount"] == 500000 and intent["code"].startswith("TF") and "qr.sepay.vn" in intent["qr_url"]
    payload = {"id": 9001, "transferType": "in", "transferAmount": 500000,
               "content": f"NGUYEN VAN X chuyen tien {intent['code'][:5]} {intent['code'][5:]}"}
    assert code(money.record_sepay, db, tid, "wrong", payload) == "unauthorized"
    ev = money.record_sepay(db, tid, key, payload)
    assert ev["type"] == "payment_confirmed" and ev["members"] == ["An", "Bình"] and ev["payer_tg_id"] == MEMBER
    assert money.record_sepay(db, tid, key, payload) is None  # SePay retried the same webhook

    st = money.status(db, tid)
    assert st["paid"] == ["An", "Bình"] and st["unpaid"] == [{"name": "Cường", "total": 250000}]
    assert code(money.create_intent, db, tid, MEMBER, [an]) == "nothing_to_pay"

    # a transfer with no code goes to the managers, who assign it
    ev = money.record_sepay(db, tid, key, {"id": 9002, "transferType": "in", "transferAmount": 250000,
                                           "content": "tien quy"})
    assert ev["type"] == "unmatched_payment" and ev["reason"] == "no_code" and set(ev["admins"]) == {OWNER, MANAGER}
    um = money.unmatched(db, tid, MANAGER)
    assert len(um) == 1
    money.assign(db, tid, MANAGER, um[0]["payment_id"], [cuong])
    assert money.status(db, tid)["unpaid"] == []
    assert code(money.assign, db, tid, MANAGER, um[0]["payment_id"], [cuong]) == "already_resolved"

    # outgoing transfers are ignored
    assert money.record_sepay(db, tid, key, {"id": 9003, "transferType": "out", "transferAmount": 1}) is None

    # charges, split, cash, expense, fund
    assert money.charges(db, tid, MANAGER, "Áo đấu", 150000)["count"] == 3
    assert money.charges(db, tid, MANAGER, "Sân", 500000, "split", [an, binh, cuong])["each"] == 167000
    assert money.markpaid(db, tid, MANAGER, [an])["amount"] == 317000
    money.add_expense(db, tid, MANAGER, 600000, "Thuê sân")
    assert money.fund(db, tid) == {"income": 500000 + 250000 + 317000, "expense": 600000, "balance": 467000,
                                   "unmatched": 0}
    assert len(money.history(db, tid)) == 4

    # member management
    members.update_member(db, tid, MANAGER, binh, {"shirt_number": "10", "fee_override": 100000})
    assert members.get_member(db, tid, MANAGER, binh)["fee"] == 100000
    members.update_member(db, tid, MANAGER, binh, {"fee_override": None})
    assert members.get_member(db, tid, MANAGER, binh)["fee"] == 250000
    assert code(members.update_member, db, tid, MANAGER, binh, {"status": "gone"}) == "invalid"
    bills = members.member_bills(db, tid, MANAGER, binh)["bills"]
    money.waive(db, tid, MANAGER, bills[0]["id"])
    members.remove_member(db, tid, MANAGER, cuong)
    assert [m["name"] for m in members.list_members(db, tid, MANAGER)] == ["An", "Bình"]

    # admins
    assert code(teams.remove_admin, db, tid, OWNER, OWNER) == "not_a_manager"
    teams.transfer_owner(db, tid, OWNER, MANAGER)
    assert teams.role(db, tid, OWNER)["role"] == "manager"
    teams.remove_admin(db, tid, MANAGER, OWNER)
    assert teams.role(db, tid, OWNER)["role"] is None
    assert len(teams.audit_log(db, tid, MANAGER)) > 5

    # export
    assert money.export(db, tid, MANAGER)[:2] == b"PK"
    assert code(money.export, db, tid, MEMBER) == "not_manager"


def test_reminder_days():
    assert jobs.should_remind(2, 5) and jobs.should_remind(5, 5) and jobs.should_remind(8, 5)
    assert not jobs.should_remind(6, 5) and not jobs.should_remind(1, 5)


def test_jobs_run_without_a_group(db):
    sent = []
    jobs.emit = lambda e: sent.append(e) or True
    jobs.job_billing()
    jobs.job_reminders()
    jobs.job_cleanup()
    assert sent == []  # no group linked, nothing announced
