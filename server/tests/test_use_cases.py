"""Use cases, run against the real SQLAlchemy adapters (SQLite) with a fixed clock and a recording notifier."""
from datetime import datetime

import pytest

from app.domain.entities import IncomingTransfer
from app.domain.errors import DomainError

from .fakes import make_services

SUPER, OWNER, MANAGER, MEMBER = 1, 100, 200, 300


def code(fn, *args, **kw):
    with pytest.raises(DomainError) as e:
        fn(*args, **kw)
    return e.value.code


@pytest.fixture
def world():
    return make_services(datetime(2026, 11, 17, 10, 0))


def setup_team(svc) -> int:
    for uid, name in ((OWNER, "Owner"), (MANAGER, "Mgr"), (MEMBER, "Mem")):
        svc.users.upsert_user(uid, name, None, None)
    req = svc.registration.request_team(OWNER, "FC Test")
    svc.registration.decide_team_request(SUPER, req["id"], True)
    return svc.registration.create_team(OWNER, "1:secret", 555, "fc_bot", None)["team_id"]


def test_registration_needs_approval(world):
    svc, _, _ = world
    assert code(svc.registration.create_team, OWNER, "1:x", 555, "fc_bot", None) == "not_approved"
    req = svc.registration.request_team(OWNER, "FC Test")
    assert req["admin_ids"] == [SUPER]
    assert code(svc.registration.request_team, OWNER, "again") == "request_pending"
    assert code(svc.registration.decide_team_request, OWNER, req["id"], True) == "not_super"
    svc.registration.decide_team_request(SUPER, req["id"], True)
    team = svc.registration.create_team(OWNER, "1:secret", 555, "fc_bot", None)
    assert team["name"] == "FC Test" and team["token"] == "1:secret"  # stored encrypted, returned decrypted
    assert code(svc.registration.create_team, SUPER, "2:y", 555, "fc_bot", None) == "bot_exists"
    assert svc.registration.active_bots()[0]["bot_id"] == 555
    assert svc.registration.my_teams(OWNER)[0]["role"] == "owner"
    assert svc.registration.all_teams(SUPER)[0]["members"] == 0


def test_full_flow(world):
    svc, clock, notifier = world
    tid = setup_team(svc)
    teams, members, money = svc.teams, svc.members, svc.money

    # roles
    assert code(teams.update_settings, tid, MEMBER, fee=1) == "not_manager"
    assert code(members.list_members, tid, MEMBER) == "not_manager"  # the roster holds phone numbers
    mr = teams.request_manager(tid, MANAGER)
    assert mr["owner_tg_id"] == OWNER
    assert code(teams.request_manager, tid, MANAGER) == "request_pending"
    assert code(teams.decide_manager_request, tid, MANAGER, mr["id"], True) == "not_owner"
    teams.decide_manager_request(tid, OWNER, mr["id"], True)
    assert teams.role(tid, MANAGER)["role"] == "manager"
    assert code(teams.set_bank, tid, MANAGER, "MBBank", "0123", "x") == "not_owner"

    # setup
    teams.set_bank(tid, OWNER, "MBBank", "0123456", "nguyen van a")
    assert code(teams.set_bank, tid, OWNER, "MBBank", "12-34", "NGUYEN") == "invalid"
    key = teams.new_sepay_key(tid, OWNER)["api_key"]
    teams.update_settings(tid, MANAGER, fee=200000, due_day=5)
    assert code(teams.update_settings, tid, MANAGER, due_day=31) == "invalid"
    an, binh, cuong = (m["id"] for m in members.add_members(
        tid, MANAGER, [{"name": "An"}, {"name": "Bình"}, {"name": "Cường"}]))

    unpaid = money.unpaid(tid)
    assert [u["name"] for u in unpaid] == ["An", "Bình", "Cường"] and all(u["total"] == 200000 for u in unpaid)
    assert unpaid[0]["bills"][0]["period_label"] == "11/2026"
    assert len(money.unpaid(tid)[0]["bills"]) == 1  # idempotent

    # a fee change reprices this month's unpaid bills
    teams.update_settings(tid, MANAGER, fee=250000)
    assert money.unpaid(tid)[0]["total"] == 250000

    # pay for two people; the transfer content has the code split by the bank
    intent = money.create_intent(tid, MEMBER, [an, binh])
    assert intent["amount"] == 500000 and intent["code"].startswith("TF") and "qr.sepay.vn" in intent["qr_url"]
    transfer = IncomingTransfer("9001", 500000, f"NGUYEN VAN X chuyen tien {intent['code'][:5]} {intent['code'][5:]}")
    assert code(money.record_transfer, tid, "wrong", transfer) == "unauthorized"
    assert money.record_transfer(tid, key, transfer) == "payment_confirmed"
    ev = notifier.events[-1]
    assert ev["members"] == ["An", "Bình"] and ev["payer_tg_id"] == MEMBER and ev["amount"] == 500000
    assert money.record_transfer(tid, key, transfer) is None  # the provider retried the same webhook

    st = money.status(tid)
    assert st["paid"] == ["An", "Bình"] and st["unpaid"] == [{"name": "Cường", "total": 250000}]
    assert code(money.create_intent, tid, MEMBER, [an]) == "nothing_to_pay"

    # a transfer with no code goes to the managers, who assign it
    assert money.record_transfer(tid, key, IncomingTransfer("9002", 250000, "tien quy")) == "unmatched_payment"
    ev = notifier.events[-1]
    assert ev["reason"] == "no_code" and set(ev["admins"]) == {OWNER, MANAGER}
    um = money.unmatched(tid, MANAGER)
    money.assign(tid, MANAGER, um[0]["payment_id"], [cuong])
    assert money.status(tid)["unpaid"] == []
    assert code(money.assign, tid, MANAGER, um[0]["payment_id"], [cuong]) == "already_resolved"
    assert money.record_transfer(tid, key, IncomingTransfer("9003", 1, "x", incoming=False)) is None

    # charges, split, cash, expense, fund
    assert money.charges(tid, MANAGER, "Áo đấu", 150000)["count"] == 3
    assert money.charges(tid, MANAGER, "Sân", 500000, "split", [an, binh, cuong])["each"] == 167000
    assert money.markpaid(tid, MANAGER, [an])["amount"] == 317000
    money.add_expense(tid, MANAGER, 600000, "Thuê sân")
    assert money.fund(tid) == {"income": 500000 + 250000 + 317000, "expense": 600000, "balance": 467000,
                               "unmatched": 0}
    assert len(money.history(tid)) == 4

    # members
    members.update_member(tid, MANAGER, binh, {"shirt_number": "10", "fee_override": 100000})
    assert members.get_member(tid, MANAGER, binh)["fee"] == 100000
    members.update_member(tid, MANAGER, binh, {"fee_override": None})
    assert members.get_member(tid, MANAGER, binh)["fee"] == 250000
    assert code(members.update_member, tid, MANAGER, binh, {"status": "gone"}) == "invalid"
    money.waive(tid, MANAGER, members.member_bills(tid, MANAGER, binh)["bills"][0]["id"])
    members.remove_member(tid, MANAGER, cuong)
    assert [m["name"] for m in members.list_members(tid, MANAGER)] == ["An", "Bình"]

    # admins and audit
    assert code(teams.remove_admin, tid, OWNER, OWNER) == "not_a_manager"
    teams.transfer_owner(tid, OWNER, MANAGER)
    assert teams.role(tid, OWNER)["role"] == "manager"
    teams.remove_admin(tid, MANAGER, OWNER)
    assert teams.role(tid, OWNER)["role"] is None
    assert len(teams.audit_log(tid, MANAGER)) > 5

    # export
    assert money.export(tid, MANAGER)[:2] == b"PK"
    assert code(money.export, tid, MEMBER) == "not_manager"


def test_manager_request_cooldown(world):
    svc, clock, _ = world
    tid = setup_team(svc)
    r = svc.teams.request_manager(tid, MEMBER)
    svc.teams.decide_manager_request(tid, OWNER, r["id"], False)
    assert code(svc.teams.request_manager, tid, MEMBER) == "cooldown"
    clock.advance(hours=25)
    assert svc.teams.request_manager(tid, MEMBER)["status"] == "pending"


def test_jobs(world):
    svc, clock, notifier = world
    tid = setup_team(svc)
    svc.teams.update_settings(tid, OWNER, fee=200000, due_day=5)
    svc.members.add_members(tid, OWNER, [{"name": "An"}])
    svc.jobs.open_month()
    assert notifier.events == []  # no group linked yet

    svc.teams.link_group(tid, OWNER, -1001)
    clock.local = clock.local.replace(month=12, day=1)
    svc.jobs.open_month()
    svc.jobs.open_month()
    opened = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(opened) == 1 and opened[0]["period_label"] == "12/2026"  # announced once

    clock.local = clock.local.replace(day=2)  # 3 days before the due day
    svc.jobs.remind()
    svc.jobs.remind()
    reminders = [e for e in notifier.events if e["type"] == "reminder"]
    assert len(reminders) == 1 and not reminders[0]["overdue"]
    # An owes November's and December's fee
    assert reminders[0]["unpaid"] == [{"name": "An", "total": 400000}]

    clock.local = clock.local.replace(day=31, hour=20)
    svc.jobs.month_report()
    report = [e for e in notifier.events if e["type"] == "month_report"][0]
    assert report["period_label"] == "12/2026" and report["balance"] == 0

    svc.jobs.clean_up()


def test_sepay_webhook_sync():
    from .fakes import FakeSepay
    sepay = FakeSepay()
    svc, _, _ = make_services(datetime(2026, 11, 17, 10, 0), sepay=sepay)
    tid = setup_team(svc)
    teams, url = svc.teams, f"https://example.test/sepay/{tid}"

    assert code(teams.sync_sepay, tid, OWNER) == "bank_not_set"
    teams.set_bank(tid, OWNER, "VietinBank", "101003630278", "tran le hiep")
    assert code(teams.sync_sepay, tid, OWNER) == "sepay_token_missing"
    assert code(teams.set_sepay_token, tid, OWNER, "short") == "invalid"
    assert code(teams.set_sepay_token, tid, OWNER, "W" * 40) == "sepay_token_invalid"
    assert code(teams.set_sepay_token, tid, MANAGER, FakeSepay.TOKEN) in ("not_owner", "not_manager")
    teams.set_sepay_token(tid, OWNER, FakeSepay.TOKEN)
    assert teams.sepay_info(tid, OWNER)["token_set"]

    # a webhook made by hand for this team is adopted, not duplicated
    sepay.hooks["manual"] = {"url": url, "bank": "acc-1", "key": "old"}
    r = teams.sync_sepay(tid, OWNER)
    assert r["webhook_id"] == "manual" and not r["created"] and list(sepay.hooks) == ["manual"]
    from app.domain.rules import hash_secret
    key = sepay.hooks["manual"]["key"]
    with svc.teams.d.uow() as uow:
        assert uow.teams.get(tid).sepay_key_hash == hash_secret(key)  # SePay and the server share the new key

    # a new bank account that isn't linked in SePay is refused, and nothing changes
    teams.set_bank(tid, OWNER, "MBBank", "999888777", "tran le hiep")
    assert code(teams.sync_sepay, tid, OWNER) == "sepay_bank_missing"
    assert sepay.hooks["manual"]["key"] == key
    # once it's linked, the same webhook is pointed at it
    sepay.accounts.append(type(sepay.accounts[0])("acc-2", "999 888 777", "MBBank", "MB", "TRAN LE HIEP", True))
    r = teams.sync_sepay(tid, OWNER)
    assert r["webhook_id"] == "manual" and sepay.hooks["manual"]["bank"] == "acc-2" and len(sepay.hooks) == 1

    # deleted in SePay: a new one is created
    sepay.hooks.clear()
    r = teams.sync_sepay(tid, OWNER)
    assert r["created"] and list(sepay.hooks) == [r["webhook_id"]]
    # SePay won't change it in place: replaced, still one webhook
    sepay.fail_update = "invalid"
    old = r["webhook_id"]
    r = teams.sync_sepay(tid, OWNER)
    assert r["created"] and r["webhook_id"] != old and list(sepay.hooks) == [r["webhook_id"]]
    # SePay down: a clear error, and the stored key is untouched
    sepay.fail_update = "unavailable"
    assert code(teams.sync_sepay, tid, OWNER) == "sepay_unavailable"
