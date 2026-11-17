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


def link_bank(svc, tid, xid="acc-1", va=None) -> dict:
    """What an owner does: /sepaytoken, then pick an account in /setbank."""
    from .fakes import FakeSepay
    svc.teams.set_sepay_token(tid, OWNER, FakeSepay.TOKEN)
    return svc.teams.choose_bank(tid, OWNER, xid, va)


def sepay_key(svc, tid) -> str:
    """The key SePay sends with each webhook call: the one /setbank gave the team's webhook."""
    with svc.teams.d.uow() as uow:
        hook_id = uow.teams.get(tid).sepay_webhook_id
    return svc.teams.d.sepay.hooks[hook_id]["key"]


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
    assert code(teams.choose_bank, tid, MANAGER, "acc-1") == "not_owner"

    # setup: the bank account comes from the owner's SePay, never typed
    assert code(teams.bank_options, tid, OWNER) == "sepay_token_missing"
    link_bank(svc, tid)
    assert code(teams.choose_bank, tid, OWNER, "not-in-sepay") == "not_found"
    key = sepay_key(svc, tid)
    teams.update_settings(tid, MANAGER, fee=200000, notify_day=1, due_days=4)
    assert code(teams.update_settings, tid, MANAGER, notify_day=31) == "invalid"
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
    assert money.fund(tid) == {"income": 500000 + 250000 + 317000, "other_income": 0, "expense": 600000, "balance": 467000,
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
    svc.teams.update_settings(tid, OWNER, fee=200000, notify_day=1, due_days=4)  # due on the 5th
    svc.members.add_members(tid, OWNER, [{"name": "An"}])
    svc.jobs.open_month()
    assert notifier.events == []  # nobody follows the bot yet

    svc.teams.follow(tid, MEMBER)
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
    from app.domain.rules import hash_secret

    from .fakes import FakeSepay
    sepay = FakeSepay()
    svc, _, _ = make_services(datetime(2026, 11, 17, 10, 0), sepay=sepay)
    tid = setup_team(svc)
    teams, url = svc.teams, f"https://example.test/sepay/{tid}"

    assert code(teams.bank_options, tid, OWNER) == "sepay_token_missing"
    assert code(teams.choose_bank, tid, OWNER, "acc-1") == "sepay_token_missing"
    assert code(teams.set_sepay_token, tid, OWNER, "short") == "invalid"
    assert code(teams.set_sepay_token, tid, OWNER, "W" * 40) == "sepay_token_invalid"
    assert code(teams.set_sepay_token, tid, MANAGER, FakeSepay.TOKEN) in ("not_owner", "not_manager")
    teams.set_sepay_token(tid, OWNER, FakeSepay.TOKEN)
    assert teams.sepay_info(tid, OWNER)["token_set"]
    assert code(teams.bank_options, tid, MANAGER) in ("not_owner", "not_manager")
    opts = teams.bank_options(tid, OWNER)
    assert [(o["bank"], o["account"], o["va"], o["current"]) for o in opts] == [("VietinBank", "101003630278", None, False)]

    # picking adopts a webhook made by hand for this team, not a second one
    sepay.hooks["manual"] = {"url": url, "bank": "acc-1", "key": "old"}
    seen = []
    r = teams.choose_bank(tid, OWNER, "acc-1", progress=lambda step, info: seen.append((step, info.get("action"))))
    assert r["webhook_id"] == "manual" and r["action"] == "adopted" and r["changed"] and list(sepay.hooks) == ["manual"]
    # every action is reported as it happens, in order
    assert seen == [("start:accounts", None), ("accounts", None), ("account", None), ("key", None),
                    ("start:webhook", None), ("webhook", "adopted"), ("start:save", None), ("saved", None)]
    assert [x["step"] for x in r["steps"]] == [step for step, _ in seen if not step.startswith("start:")]
    assert [a["action"] for a in teams.audit_log(tid, OWNER)[:2]] == ["team.sepay_webhook", "team.bank"]
    info = teams.team_info(tid, OWNER)
    assert (info["bank_name"], info["bank_account"], info["bank_holder"]) == ("VietinBank", "101003630278", "TRAN LE HIEP")
    key = sepay.hooks["manual"]["key"]
    with svc.teams.d.uow() as uow:
        assert uow.teams.get(tid).sepay_key_hash == hash_secret(key)  # SePay and the server share the new key
    assert teams.bank_options(tid, OWNER)[0]["current"]

    # another account linked later: picking it points the same webhook at it
    sepay.accounts.append(type(sepay.accounts[0])("acc-2", "999 888 777", "MBBank", "MB", "TRAN LE HIEP", True))
    r = teams.choose_bank(tid, OWNER, "acc-2")
    assert r["webhook_id"] == "manual" and sepay.hooks["manual"]["bank"] == "acc-2" and len(sepay.hooks) == 1
    # an account unlinked in SePay can't be picked, and nothing changes
    sepay.accounts.pop()
    assert code(teams.choose_bank, tid, OWNER, "acc-2") == "not_found"
    assert teams.team_info(tid, OWNER)["bank_account"] == "999888777"
    teams.choose_bank(tid, OWNER, "acc-1")

    # picking the same account again repairs the webhook: deleted in SePay -> a new one
    sepay.hooks.clear()
    r = teams.choose_bank(tid, OWNER, "acc-1")
    assert r["action"] == "created" and not r["changed"] and list(sepay.hooks) == [r["webhook_id"]]
    # SePay won't change it in place: replaced, still one webhook
    sepay.fail_update = "invalid"
    old = r["webhook_id"]
    r = teams.choose_bank(tid, OWNER, "acc-1")
    assert r["action"] == "replaced" and r["webhook_id"] != old and list(sepay.hooks) == [r["webhook_id"]]
    # SePay down: a clear error, and the team keeps the key SePay already has
    before = sepay_key(svc, tid)
    sepay.fail_update = "unavailable"
    assert code(teams.choose_bank, tid, OWNER, "acc-1") == "sepay_unavailable"
    with svc.teams.d.uow() as uow:
        assert uow.teams.get(tid).sepay_key_hash == hash_secret(before)


def test_web_pay_page(world):
    svc, clock, notifier = world
    tid = setup_team(svc)
    teams, web = svc.teams, svc.web
    assert code(web.page, "nope") == "not_found"
    assert code(teams.web_link, tid, MEMBER) in ("not_manager", "not_owner")
    url = teams.web_link(tid, OWNER)["url"]
    token = url.rstrip("/").rsplit("/", 1)[1]
    assert url == f"https://example.test/p/{token}/" and len(token) >= 16
    assert teams.web_link(tid, OWNER)["url"] == url  # stable until reset

    page = web.page(token)
    assert page["team"] == "FC Test" and not page["has_bank"] and page["unpaid"] == []
    link_bank(svc, tid)
    teams.update_settings(tid, OWNER, fee=200000)
    svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"}])
    page = web.page(token)
    assert [u["name"] for u in page["unpaid"]] == ["An", "Bình"] and page["total_unpaid"] == 400000
    assert "phone" not in page["unpaid"][0] and "id" not in page["unpaid"][0]["bills"][0]  # names and amounts only

    an = page["unpaid"][0]["member_id"]
    for bad in ([], "1", [True], ["1"], list(range(51))):
        assert code(web.create_intent, token, bad) == "invalid"
    intent = web.create_intent(token, [an, an])
    assert intent["amount"] == 200000 and intent["members"] == ["An"] and intent["content"].startswith("SEVQR TF")
    assert web.intent_status(token, intent["code"])["status"] == "pending"
    assert code(web.intent_status, token, "TFNOTREAL1") == "not_found"

    key = sepay_key(svc, tid)
    svc.money.record_transfer(tid, key, IncomingTransfer("tx-web", 200000, f"x {intent['content']} y"))
    st = web.intent_status(token, intent["code"])
    assert st["status"] == "paid" and st["members"] == ["An"]
    ev = notifier.events[-1]
    assert ev["type"] == "payment_confirmed" and ev["payer_tg_id"] is None  # no Telegram payer: group post only
    assert [u["name"] for u in web.page(token)["unpaid"]] == ["Bình"]

    # the read-only parts: this month, the fund, history, the roster
    svc.members.update_member(tid, OWNER, an, {"phone": "0901234567", "note": "private", "shirt_number": "10"})
    svc.money.add_expense(tid, OWNER, 50000, "Sân bóng")
    svc.money.record_transfer(tid, key, IncomingTransfer("tx-stranger", 30000, "NGUYEN VAN X CHUYEN TIEN FT2611"))
    view = web.page(token)
    assert view["paid"] == ["An"] and view["fund"] == {"income": 230000, "other_income": 0, "expense": 50000, "balance": 180000}
    assert view["months"][0] == {"period": "2026-11", "period_label": "11/2026", "collected": 230000, "spent": 50000}
    assert len(view["months"]) == 1  # no records before this month
    hist = {(h["type"], h["kind"], h["title"], h["amount"]) for h in view["history"]}
    assert hist == {("in", "sepay", "An", 200000), ("in", "unassigned", "", 30000), ("out", "expense", "Sân bóng", 50000)}
    roster = {m["name"]: m for m in view["members"]}
    assert roster["An"] == {"id": an, "name": "An", "nickname": None, "shirt_number": "10", "status": "active", "fee": 200000, "months": 1, "owes": 0}
    assert roster["Bình"]["owes"] == 200000
    blob = repr(view)
    assert "0901234567" not in blob and "private" not in blob and "NGUYEN VAN X" not in blob and "FT2611" not in blob

    other = web.create_intent(token, [page["unpaid"][1]["member_id"]])
    clock.advance(hours=25)
    assert web.intent_status(token, other["code"])["status"] == "expired"

    new_url = teams.web_link(tid, OWNER, reset=True)["url"]
    assert new_url != url and code(web.page, token) == "not_found"


def test_va_only_bank_uses_the_virtual_account():
    """BIDV personal: SePay never reports transfers to the main number, so the QR must use a VA."""
    from app.application.ports import SepayBankAccount, SepayVirtualAccount

    from .fakes import FakeSepay
    sepay = FakeSepay()
    sepay.accounts = [SepayBankAccount("bidv-1", "5101630799", "BIDV", "BIDV", "TRAN LE HIEP", True, va_only=True)]
    svc, _, _ = make_services(datetime(2026, 11, 17, 10, 0), sepay=sepay)
    tid = setup_team(svc)
    teams = svc.teams
    teams.set_sepay_token(tid, OWNER, FakeSepay.TOKEN)
    opts = teams.bank_options(tid, OWNER)
    assert len(opts) == 1 and opts[0]["needs_va"] and opts[0]["va"] is None  # BIDV without a VA yet
    assert code(teams.choose_bank, tid, OWNER, "bidv-1") == "sepay_va_missing" and not sepay.hooks

    sepay.vas["bidv-1"] = [SepayVirtualAccount("OLD1", False, True), SepayVirtualAccount("96247TLH2203", True, True)]
    assert [o["va"] for o in teams.bank_options(tid, OWNER)] == ["96247TLH2203"]  # one choice per active VA
    assert code(teams.choose_bank, tid, OWNER, "bidv-1", "OLD1") == "not_found"   # an inactive VA can't be picked
    r = teams.choose_bank(tid, OWNER, "bidv-1", "96247TLH2203")
    assert r["va"] == "96247TLH2203" and r["account"] == "5101630799"
    info = teams.team_info(tid, OWNER)
    assert info["pay_account"] == "96247TLH2203" and info["bank_account"] == "5101630799"

    teams.update_settings(tid, OWNER, fee=2000)
    m = svc.members.add_members(tid, OWNER, [{"name": "Khoa"}])[0]
    svc.money.unpaid(tid)  # /start: makes this month's bills
    intent = svc.money.create_intent(tid, MEMBER, [m["id"]])
    assert intent["bank_account"] == "96247TLH2203" and "acc=96247TLH2203" in intent["qr_url"]
    assert intent["content"].startswith("TF")  # BIDV needs no keyword
    token = teams.web_link(tid, OWNER)["url"].rstrip("/").rsplit("/", 1)[1]
    assert svc.web.page(token)["bank_account"] == "96247TLH2203"

    # moving to a bank that takes main-account transfers drops the VA
    sepay.accounts.append(SepayBankAccount("acc-1", "101003630278", "VietinBank", "ICB", "TRAN LE HIEP", True))
    teams.choose_bank(tid, OWNER, "acc-1")
    assert teams.team_info(tid, OWNER)["pay_account"] == "101003630278"


def test_fee_plans():
    """150k monthly for some, 300k per quarter for others: one bill per cycle, the quarter covers three months."""
    svc, clock, _ = make_services(datetime(2026, 11, 3, 10, 0))
    tid = setup_team(svc)
    svc.teams.update_settings(tid, OWNER, fee=200000)
    ids = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": n} for n in ("An", "Bình", "Cường", "Dũng")])]
    an, binh, cuong, dung = ids
    assert code(svc.plans.save_plan, tid, MEMBER, 300000, 3, [cuong]) == "not_manager"
    assert code(svc.plans.save_plan, tid, OWNER, 300000, 2, [cuong]) == "invalid"
    monthly = svc.plans.save_plan(tid, OWNER, 150000, 1, [an, binh])
    quarter = svc.plans.save_plan(tid, OWNER, 300000, 3, [cuong])
    assert [m["name"] for m in quarter["members"]] == ["Cường"]
    # same amount and cycle joins the existing plan
    assert svc.plans.save_plan(tid, OWNER, 300000, 3, [dung])["id"] == quarter["id"]
    view = svc.plans.list_plans(tid, OWNER)
    assert [(p["amount"], p["months"], len(p["members"])) for p in view["plans"]] == [(150000, 1, 2), (300000, 3, 2)]
    assert view["default_members"] == []

    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert owed["An"]["total"] == 150000 and owed["Cường"]["total"] == 300000
    assert owed["Cường"]["bills"][0]["period_label"] == "11/2026–01/2027"
    s = svc.money.status(tid)
    assert {(f["amount"], f["months"], f["count"]) for f in s["fees"]} == {(150000, 1, 2), (300000, 3, 2)}
    svc.money.markpaid(tid, OWNER, [an, cuong])

    # December: An is billed again, Cường's quarter still covers him (and counts as paid), Dũng still owes
    clock.local = clock.local.replace(month=12, day=2)
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    assert owed == {"An": 150000, "Bình": 300000, "Dũng": 300000}  # Bình: Nov + Dec
    s = svc.money.status(tid)
    assert s["paid"] == ["Cường"]
    # February: a new quarter for Cường
    clock.local = clock.local.replace(year=2027, month=2)
    cu = next(g for g in svc.money.unpaid(tid) if g["name"] == "Cường")
    assert [b["period_label"] for b in cu["bills"]] == ["02/2027–04/2027"]

    # changing a plan reprices this month's unpaid bills; members left out go back to the team fee
    svc.plans.save_plan(tid, OWNER, 350000, 3, [cuong], plan_id=quarter["id"])
    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert owed["Cường"]["bills"][-1]["amount"] == 350000
    assert svc.members.get_member(tid, OWNER, dung)["fee"] == 200000
    # an own monthly fee takes a member off their plan; deleting a plan moves everyone to the team fee
    assert svc.members.update_member(tid, OWNER, binh, {"fee_override": 120000})["on_plan"] is False
    gone = svc.plans.delete_plan(tid, OWNER, monthly["id"])
    assert gone["members"] == ["An"]
    assert [p["amount"] for p in svc.plans.list_plans(tid, OWNER)["plans"]] == [350000]


def test_other_income_and_backdating(world):
    svc, clock, _ = world
    tid = setup_team(svc)
    from datetime import date
    assert code(svc.money.add_income, tid, MEMBER, 2000000, "Thưởng vô địch") == "not_manager"
    r = svc.money.add_income(tid, OWNER, 2000000, "Thưởng vô địch")
    assert r["balance"] == 2000000
    old = svc.money.add_income(tid, OWNER, 1500000, "Số dư đầu kỳ", on=date(2026, 9, 30))
    assert old["at"].startswith("30/09/2026")
    svc.money.add_expense(tid, OWNER, 300000, "Sân bóng", on=date(2026, 10, 15))
    assert code(svc.money.add_expense, tid, OWNER, 1, "x", on=date(2026, 11, 18)) == "bad_date"
    assert code(svc.money.add_income, tid, OWNER, 1, "x", on=date(2020, 1, 1)) == "bad_date"
    f = svc.money.fund(tid)
    assert (f["income"], f["other_income"], f["expense"], f["balance"]) == (3500000, 3500000, 300000, 3200000)
    with svc.money.d.uow() as uow:
        team = uow.teams.get(tid)
        from app.application import billing
        assert billing.month_totals(uow, svc.money.d, team, "2026-10") == {"collected": 0, "spent": 300000}
        assert billing.month_totals(uow, svc.money.d, team, "2026-09")["collected"] == 1500000
    h = svc.money.history(tid)
    assert h[0]["source"] == "other" and h[0]["title"] == "Thưởng vô địch"
    oct_ = svc.money.history(tid, period="2026-10")
    assert [(i["type"], i["title"]) for i in oct_] == [("out", "Sân bóng")]
    token = svc.teams.web_link(tid, OWNER)["url"].rstrip("/").rsplit("/", 1)[-1]
    m = svc.web.month(token, "2026-09")
    assert (m["collected"], m["spent"], [i["title"] for i in m["items"]]) == (1500000, 0, ["Số dư đầu kỳ"])
    assert code(svc.web.month, token, "2026-13") == "invalid"
    assert code(svc.web.month, "nope", "2026-09") == "not_found"
    months = [x["period"] for x in svc.web.page(token)["months"]]
    assert months == ["2026-11", "2026-10", "2026-09"], "back to the backdated opening balance"


def test_make_bot_through_botfather():
    from .fakes import FakeBotFactory
    factory = FakeBotFactory()
    svc, _, _ = make_services(datetime(2026, 11, 17, 10, 0), bot_factory=factory)
    svc.users.upsert_user(OWNER, "Owner", None, None)
    assert svc.registration.can_make_bot() is True
    assert code(svc.registration.make_bot, OWNER) == "not_approved"
    req = svc.registration.request_team(OWNER, "FC 37 IT")
    svc.registration.decide_team_request(SUPER, req["id"], True)
    factory.taken.add("fc_37_it_fund_bot")
    made = svc.registration.make_bot(OWNER)
    assert made["username"] == "fc_37_it_tf_bot" and factory.calls[0][0] == "FC 37 IT · Team Fund"
    team = svc.registration.create_team(OWNER, made["token"], 701, made["username"], None)
    assert team["name"] == "FC 37 IT"
    assert code(svc.registration.make_bot, OWNER) == "not_approved", "the request is used up"
    factory.fail = "limit"
    req = svc.registration.request_team(OWNER, "Second")
    svc.registration.decide_team_request(SUPER, req["id"], True)
    assert code(svc.registration.make_bot, OWNER) == "botfather_limit"
    assert make_services(datetime(2026, 11, 17), bot_factory=FakeBotFactory(ready=False))[0].registration.can_make_bot() is False


def test_admin_makes_a_bot_by_name():
    from .fakes import FakeBotFactory
    svc, _, _ = make_services(datetime(2026, 11, 17, 10, 0), admin_ids=(SUPER,), bot_factory=FakeBotFactory())
    assert code(svc.registration.make_bot, SUPER) == "not_approved", "an admin still has to give a name"
    made = svc.registration.make_bot(SUPER, "  Đội Bóng Hà Nội ")
    assert made["team_name"] == "Đội Bóng Hà Nội" and made["username"] == "doi_bong_ha_noi_fund_bot"
    assert svc.registration.create_team(SUPER, made["token"], 801, made["username"], made["team_name"])["name"] \
        == "Đội Bóng Hà Nội"
    assert code(svc.registration.make_bot, OWNER, "Any") == "not_approved", "others need an approved request"


def test_cash_less_than_owed():
    """Joined mid-quarter: owes 600k, pays 300k. Settle closes the bill at 300k; or the rest stays owed."""
    svc, clock, _ = make_services(datetime(2026, 11, 3, 10, 0))
    tid = setup_team(svc)
    an, binh, cuong = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": n} for n in ("An", "Bình", "Cường")])]
    svc.plans.save_plan(tid, OWNER, 600000, 3, [an, binh, cuong])
    svc.money.unpaid(tid)
    assert svc.money.owed_by(tid, OWNER, an)["total"] == 600000
    r = svc.money.markpaid_amount(tid, OWNER, an, 300000, settle=True)
    assert (r["amount"], r["let_go"], r["still_owed"]) == (300000, 300000, 0)
    r = svc.money.markpaid_amount(tid, OWNER, binh, 200000, settle=False)
    assert (r["amount"], r["let_go"], r["still_owed"]) == (200000, 0, 400000)
    assert code(svc.money.markpaid_amount, tid, OWNER, cuong, 700000, True) == "more_than_owed"
    assert code(svc.money.markpaid_amount, tid, MEMBER, cuong, 1000, True) == "not_manager"
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    assert owed == {"Bình": 400000, "Cường": 600000}
    assert svc.money.fund(tid)["income"] == 500000, "the fund grows by what came in only"
    s = svc.money.status(tid)
    assert s["paid"] == ["An"], "settled counts as paid; Bình still owes part of the quarter"
    # December: An's settled quarter still covers him; Bình's remainder is the only thing he owes
    clock.local = clock.local.replace(month=12)
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"Bình": 400000, "Cường": 600000}
    svc.money.markpaid(tid, OWNER, [binh])
    assert svc.money.fund(tid)["income"] == 900000


def test_schedule_and_late_fee():
    """Announce on the 15th, due 5 days later; 1,000đ per overdue day, only from when it's turned on."""
    from datetime import date
    svc, clock, notifier = make_services(datetime(2026, 11, 10, 10, 0))
    tid = setup_team(svc)
    svc.teams.follow(tid, MEMBER)
    key = sepay_key_for_fake(svc, tid)
    an, binh = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"}])]
    r = svc.teams.update_settings(tid, OWNER, fee=200000, notify_day=15, due_days=5)
    assert (r["notify_day"], r["due_days"], r["due"]) == (15, 5, "20/11")
    assert code(svc.teams.update_settings, tid, OWNER, notify_day=29) == "invalid"
    assert code(svc.teams.update_settings, tid, OWNER, late_fee=-1) == "invalid"
    svc.jobs.open_month()
    assert not [e for e in notifier.events if e["type"] == "bills_created"], "not before the 15th"
    clock.local = clock.local.replace(day=15)
    svc.jobs.open_month()
    opened = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(opened) == 1 and opened[0]["fees"][0]["due"] == "20/11"

    clock.local = clock.local.replace(day=22)
    svc.teams.update_settings(tid, OWNER, late_fee=1000)  # turned on on the 22nd: counts from the 22nd
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"An": 200000, "Bình": 200000}
    clock.local = clock.local.replace(day=25)
    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert owed["An"]["total"] == 203000
    late = [b for b in owed["An"]["bills"] if b["kind"] == "late"][0]
    assert (late["days"], late["amount"]) == (3, 3000)
    s = svc.money.status(tid)
    assert [(f["due"], f["late_fee"], f["count"]) for f in s["fees"]] == [("20/11", 1000, 2)]

    # An makes a QR on the 25th and pays it on the 26th: the extra day's fine isn't asked for
    intent = svc.money.create_intent(tid, MEMBER, [an])
    assert intent["amount"] == 203000
    clock.local = clock.local.replace(day=26)
    svc.money.unpaid(tid)  # the late fee grows to 4,000đ
    assert svc.money.record_transfer(tid, key, IncomingTransfer("tx-late", 203000, intent["content"])) == \
        "payment_confirmed"
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    assert "An" not in owed and owed["Bình"] == 204000
    clock.local = clock.local.replace(day=28)
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"Bình": 206000}, "An's fine stopped"

    # waiving the fee waives its fine; turning the late fee off waives the fines left
    bills = svc.members.member_bills(tid, OWNER, binh)["bills"]
    svc.money.waive(tid, OWNER, next(b["id"] for b in bills if b["kind"] == "monthly"))
    assert svc.money.unpaid(tid) == []
    svc.teams.update_settings(tid, OWNER, late_fee=0)
    assert svc.teams.team_info(tid, OWNER)["late_fee"] == 0


def sepay_key_for_fake(svc, tid):
    link_bank(svc, tid)
    return sepay_key(svc, tid)


def test_each_package_has_its_own_terms():
    """Default: announce 15th, due +5, 1k/day. The quarterly plan: announce the 1st, due +10, 2k/day."""
    svc, clock, notifier = make_services(datetime(2026, 11, 1, 10, 0))
    tid = setup_team(svc)
    svc.teams.follow(tid, MEMBER)
    an, cu = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Cường"}])]
    svc.teams.update_settings(tid, OWNER, fee=200000)
    q = svc.plans.save_plan(tid, OWNER, 600000, 3, [cu])
    d = svc.plans.set_terms(tid, OWNER, None, schedule=(15, 5), late_fee=1000)
    assert (d["notify_day"], d["due_days"], d["late_fee"]) == (15, 5, 1000)
    p = svc.plans.get_plan(tid, OWNER, q["id"])
    assert (p["notify_day"], p["late_fee"], p["own_schedule"], p["own_late"]) == (15, 1000, False, False)
    p = svc.plans.set_terms(tid, OWNER, q["id"], schedule=(1, 10), late_fee=2000)
    assert (p["notify_day"], p["due_days"], p["late_fee"], p["own_schedule"]) == (1, 10, 2000, True)
    assert code(svc.plans.set_terms, tid, MEMBER, q["id"], late_fee=1) == "not_manager"
    assert code(svc.plans.set_terms, tid, OWNER, q["id"], schedule=(30, 1)) == "invalid"

    fees = {f["amount"]: f for f in svc.money.status(tid)["fees"]}
    assert (fees[200000]["due"], fees[200000]["late_fee"]) == ("20/11", 1000)
    assert (fees[600000]["due"], fees[600000]["late_fee"]) == ("11/11", 2000)

    svc.jobs.open_month()  # the 1st: only the quarterly plan is announced
    posts = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(posts) == 1 and [f["amount"] for f in posts[0]["fees"]] == [600000]
    clock.local = clock.local.replace(day=15)
    svc.jobs.open_month()
    svc.jobs.open_month()
    posts = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(posts) == 2 and [f["amount"] for f in posts[1]["fees"]] == [200000], "each package once"

    clock.local = clock.local.replace(day=23)
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    # Cường: due 11/11 under the plan, 2k a day for 12 days; An: due 20/11, 1k a day for 3 days
    assert owed == {"An": 203000, "Cường": 624000}
    # the plan goes back to the default's terms: due 20/11, 1k/day from the 1st -> 3 days
    svc.plans.set_terms(tid, OWNER, q["id"], inherit_schedule=True, inherit_late=True)
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"An": 203000, "Cường": 603000}


def test_followers():
    svc, _, notifier = make_services(datetime(2026, 11, 17, 10, 0))
    tid = setup_team(svc)
    assert svc.teams.follow(tid, MEMBER) is True and svc.teams.follow(tid, MEMBER) is False
    svc.teams.follow(tid, OWNER)
    assert svc.teams.followers(tid) == [MEMBER, OWNER]
    assert svc.teams.set_muted(tid, MEMBER)["muted"] is True  # /notify toggles
    assert svc.teams.followers(tid) == [OWNER]
    assert svc.teams.set_muted(tid, MEMBER)["muted"] is False
    assert svc.teams.team_info(tid, OWNER)["followers"] == 2


def test_undo():
    """Any recent action; puts its rows back; refuses when they changed since; managers only their own."""
    svc, clock, _ = make_services(datetime(2026, 11, 10, 10, 0))
    tid = setup_team(svc)
    an, binh = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"}])]
    svc.teams.update_settings(tid, OWNER, fee=200000)
    svc.money.unpaid(tid)  # fee bills for An and Bình
    svc.money.markpaid(tid, OWNER, [an])
    svc.money.add_expense(tid, OWNER, 300000, "Sân bóng")
    svc.money.add_income(tid, OWNER, 50000, "Tip")
    acts = svc.undo.recent(tid, OWNER)
    assert [a["action"] for a in acts] == ["income.add", "expense.add", "payment.cash", "team.settings", "member.add"]

    # one in the middle: the expense
    r = svc.undo.undo(tid, OWNER, acts[1]["id"])
    assert r["announce"] is True and svc.money.fund(tid)["expense"] == 0 and svc.money.fund(tid)["income"] == 250000
    assert code(svc.undo.undo, tid, OWNER, acts[1]["id"]) == "undo_gone"
    log = svc.teams.audit_log(tid, OWNER)
    assert log[0]["action"] == "undo" and "undo" not in [x["action"] for x in svc.undo.recent(tid, OWNER)], \
        "the undo itself is logged, and can't be undone"
    assert [a["action"] for a in svc.undo.recent(tid, OWNER)] == ["income.add", "payment.cash", "team.settings", "member.add"]

    # the cash payment: An owes again
    svc.undo.undo(tid, OWNER, acts[2]["id"])
    assert svc.money.fund(tid)["income"] == 50000
    assert {g["name"] for g in svc.money.unpaid(tid)} == {"An", "Bình"}

    # adding members can't be undone once they have fee bills (their rows are used)
    assert code(svc.undo.undo, tid, OWNER, acts[4]["id"]) == "undo_changed"

    # a charge whose bill was paid since can't be undone; nothing changes
    svc.money.charges(tid, OWNER, "Áo", 150000, "charge", [an, binh])
    charge = svc.undo.recent(tid, OWNER)[0]
    with svc.money.d.uow() as uow:  # An's charge gets paid another way (as SePay would)
        b = next(b for b in uow.bills.unpaid_of_member(an) if b.kind == "charge")
        b.status = "paid"
        uow.commit()
    assert code(svc.undo.undo, tid, OWNER, charge["id"]) == "undo_changed"
    assert sum(1 for g in svc.money.unpaid(tid) for b in g["bills"] if b["kind"] == "charge") == 1

    # a plan: undo puts the member back on the default package
    svc.plans.save_plan(tid, OWNER, 600000, 3, [binh])
    svc.undo.undo(tid, OWNER, svc.undo.recent(tid, OWNER)[0]["id"])
    assert svc.members.get_member(tid, OWNER, binh)["on_plan"] is False
    assert svc.plans.list_plans(tid, OWNER)["plans"] == []

    # managers see and undo only their own; actions that can't be undone are never offered
    svc.users.upsert_user(MANAGER, "Mgr", None, None)
    with svc.money.d.uow() as uow:
        from app.domain.entities import TeamAdmin
        uow.admins.add(TeamAdmin(team_id=tid, tg_id=MANAGER, role="manager", added_by=OWNER))
        uow.commit()
    svc.money.add_expense(tid, MANAGER, 20000, "Nước")
    mine = svc.undo.recent(tid, MANAGER)
    assert [a["target"] for a in mine] == ["Nước"]
    tip = next(a for a in svc.undo.recent(tid, OWNER) if a["target"] == "Tip")
    assert code(svc.undo.undo, tid, MANAGER, tip["id"]) == "undo_not_yours"
    svc.teams.web_link(tid, OWNER, reset=True)
    assert "team.web_link" not in [a["action"] for a in svc.undo.recent(tid, OWNER)]
    clock.advance(days=8)
    assert svc.undo.recent(tid, OWNER) == [] and code(svc.undo.undo, tid, OWNER, tip["id"]) == "undo_too_old"
