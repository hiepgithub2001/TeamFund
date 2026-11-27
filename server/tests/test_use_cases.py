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
    # paid late with the expired QR anyway: the code matches, but a manager confirms whose it is
    svc.jobs.clean_up()
    assert svc.money.record_transfer(tid, key, IncomingTransfer("tx-late-qr", 200000, other["content"])) == "unmatched_payment"
    ev = notifier.events[-1]
    binh = page["unpaid"][1]["member_id"]
    assert ev["reason"] == "expired" and ev["qr_for"] == ["Bình"] and ev["member_ids"] == [binh] and ev["qr_amount"] == 200000
    assert [u["name"] for u in web.page(token)["unpaid"]] == ["Bình"]  # not paid until a manager says so
    item = next(u for u in svc.money.unmatched(tid, OWNER) if u["reason"] == "expired")
    assert item["qr_for"] == ["Bình"] and item["member_ids"] == [binh]
    assert svc.money.assign(tid, OWNER, item["payment_id"], item["member_ids"])["members"] == ["Bình"]
    assert web.page(token)["unpaid"] == []
    # the same old QR paid a second time: nothing left to pay
    assert svc.money.record_transfer(tid, key, IncomingTransfer("tx-twice", 200000, other["content"])) == "unmatched_payment"
    assert notifier.events[-1]["reason"] == "already_paid"

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
    assert owed["Cường"]["bills"][0]["period_label"] == "10/2026–12/2026", "standard quarter, Oct–Dec"
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
    assert [b["period_label"] for b in cu["bills"]] == ["01/2027–03/2027"]

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
    assert code(svc.money.markpaid_amount, tid, MEMBER, cuong, 1000, True) == "not_manager"
    assert code(svc.money.markpaid_amount, tid, OWNER, cuong, 0, True) == "cash_amount"
    assert code(svc.money.markpaid_amount, tid, OWNER, cuong, 600000 + 50_000_001, True) == "cash_amount"
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    assert owed == {"Bình": 400000, "Cường": 600000}
    assert svc.money.fund(tid)["income"] == 500000, "the fund grows by what came in only"
    s = svc.money.status(tid)
    assert s["paid"] == ["An"], "settled counts as paid; Bình still owes part of the quarter"
    # paid more than owed: the quarter is paid and the extra stays in the fund
    r = svc.money.markpaid_amount(tid, OWNER, cuong, 700000, settle=True)
    assert (r["amount"], r["extra"], r["let_go"], r["still_owed"]) == (700000, 100000, 0, 0)
    assert svc.money.fund(tid)["income"] == 1200000
    # December: An's settled quarter still covers him; Bình's remainder is the only thing he owes
    clock.local = clock.local.replace(month=12)
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"Bình": 400000}
    svc.money.markpaid(tid, OWNER, [binh])
    assert svc.money.fund(tid)["income"] == 1600000


def test_schedule_and_late_fee():
    """Announce on the 15th, due 5 days later; 1,000đ per day past the due date (even days before it was set)."""
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
    svc.teams.update_settings(tid, OWNER, late_fee=1000)  # turned on on the 22nd: still counts from the 20th
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"An": 202000, "Bình": 202000}
    clock.local = clock.local.replace(day=25)
    assert svc.money.unpaid(tid)[0]["total"] == 202000, "looking doesn't change fines; the daily job does"
    svc.jobs.late_fees()
    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert owed["An"]["total"] == 205000
    late = [b for b in owed["An"]["bills"] if b["kind"] == "late"][0]
    assert (late["days"], late["amount"]) == (5, 5000)
    s = svc.money.status(tid)
    assert [(f["due"], f["late_fee"], f["count"]) for f in s["fees"]] == [("20/11", 1000, 2)]

    # An makes a QR on the 25th and pays it on the 26th: the extra day's fine isn't asked for
    intent = svc.money.create_intent(tid, MEMBER, [an])
    assert intent["amount"] == 205000
    clock.local = clock.local.replace(day=26)
    svc.jobs.late_fees()  # the next day's run: An's fine grows to 6,000đ
    assert svc.money.record_transfer(tid, key, IncomingTransfer("tx-late", 205000, intent["content"])) == \
        "payment_confirmed"
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    assert "An" not in owed and owed["Bình"] == 206000
    clock.local = clock.local.replace(day=28)
    svc.jobs.late_fees()
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"Bình": 208000}, "An's fine stopped"

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
    assert (fees[600000]["due"], fees[600000]["late_fee"]) == ("11/10", 2000), "the quarter's own due date"

    svc.jobs.open_month()  # the 1st: only the quarterly plan is announced
    posts = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(posts) == 1 and [f["amount"] for f in posts[0]["fees"]] == [600000]
    clock.local = clock.local.replace(day=15)
    svc.jobs.open_month()
    svc.jobs.open_month()
    posts = [e for e in notifier.events if e["type"] == "bills_created"]
    assert len(posts) == 2 and [f["amount"] for f in posts[1]["fees"]] == [200000], "each package once"

    clock.local = clock.local.replace(day=23)
    svc.jobs.late_fees()
    owed = {g["name"]: g["total"] for g in svc.money.unpaid(tid)}
    # Cường: due 11/10 under the plan, 43 days x 2k; An: due 20/11, 3 days x 1k
    assert owed == {"An": 203000, "Cường": 686000}
    # the plan goes back to the default's terms: due 20/11, 1k/day from the 1st -> 3 days
    svc.plans.set_terms(tid, OWNER, q["id"], inherit_schedule=True, inherit_late=True)
    assert {g["name"]: g["total"] for g in svc.money.unpaid(tid)} == {"An": 203000, "Cường": 634000}


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


def test_standard_quarters():
    """Quarters are Jan–Mar … Oct–Dec; months already paid aren't billed again, the rest is priced pro rata."""
    from app.domain.rules import cycle_bill, cycle_start
    assert [cycle_start(p, 3) for p in ("2026-10", "2026-11", "2026-12", "2027-01")] == ["2026-10"] * 3 + ["2027-01"]
    assert cycle_start("2026-08", 6) == "2026-07" and cycle_start("2026-08", 12) == "2026-01"
    assert cycle_bill(600000, 3, "2026-10") == (3, 600000) and cycle_bill(600000, 3, "2026-11") == (2, 400000)
    svc, clock, _ = make_services(datetime(2026, 10, 3, 10, 0))
    tid = setup_team(svc)
    svc.teams.update_settings(tid, OWNER, fee=200000)
    an, = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}])]
    svc.money.unpaid(tid)
    svc.money.markpaid(tid, OWNER, [an])  # October paid, monthly
    clock.local = clock.local.replace(month=11)
    svc.plans.save_plan(tid, OWNER, 600000, 3, [an])
    bills = svc.money.unpaid(tid)[0]["bills"]
    assert [(b["period_label"], b["amount"]) for b in bills] == [("11/2026–12/2026", 400000)], "Nov–Dec only"
    clock.local = clock.local.replace(year=2027, month=1)
    assert [b["period_label"] for b in svc.money.unpaid(tid)[0]["bills"]][-1] == "01/2027–03/2027"


def test_plan_window():
    """A plan applies from the period a member joins it, for good, unless a manager picks other whole
    periods: from next period (a newcomer), or from one period to another."""
    from app.domain.rules import cycle_bill, period_name
    assert cycle_bill(600000, 3, "2026-10", until="2026-11") == (2, 400000)
    assert [period_name("2026-11", n) for n in (1, 3, 6, 12)] == ["11/2026", "2026Q4", "2026H2", "2026"]
    svc, clock, _ = make_services(datetime(2026, 11, 20, 10, 0))
    tid = setup_team(svc)
    names = ["An", "Bình", "Cường"]
    ids = dict(zip(names, [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": n} for n in names])]))
    p = svc.plans.save_plan(tid, OWNER, 600000, 3, list(ids.values()))
    assert {m["name"]: (m["from"], m["until"]) for m in p["members"]} == {n: ("2026-10", None) for n in names}

    svc.plans.set_window(tid, OWNER, p["id"], [ids["Bình"]], "2027-01", None)  # a newcomer: from next quarter
    svc.plans.set_window(tid, OWNER, p["id"], [ids["Cường"]], "2026-10", "2027-03")  # two quarters only
    for start, until in (("2026-11", None), ("2026-10", "2026-11"), ("2027-01", "2026-12"), ("11/2026", None)):
        assert code(svc.plans.set_window, tid, OWNER, p["id"], [ids["An"]], start, until) == "invalid", (start, until)

    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    fee = lambda n: [(b["period_label"], b["amount"]) for b in owed[n]["bills"] if b["kind"] == "monthly"]  # noqa: E731
    assert fee("An") == [("10/2026–12/2026", 600000)]
    assert "Bình" not in owed, "nothing this quarter"
    assert fee("Cường") == [("10/2026–12/2026", 600000)]

    clock.local = clock.local.replace(year=2027, month=1, day=2)
    svc.jobs.open_month()
    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert fee("Bình") == [("01/2027–03/2027", 600000)]
    assert fee("Cường")[-1] == ("01/2027–03/2027", 600000)
    clock.local = clock.local.replace(month=4)
    svc.jobs.open_month()
    owed = {g["name"]: g for g in svc.money.unpaid(tid)}
    assert fee("An")[-1] == ("04/2027–06/2027", 600000) and fee("Bình")[-1] == ("04/2027–06/2027", 600000)
    assert fee("Cường")[-1] == ("01/2027–03/2027", 600000), "Cường's plan ended after 2027Q1"


def test_undo_plan_window():
    svc, clock, _ = make_services(datetime(2026, 11, 20, 10, 0))
    tid = setup_team(svc)
    an, = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}])]
    p = svc.plans.save_plan(tid, OWNER, 600000, 3, [an])
    svc.plans.set_window(tid, OWNER, p["id"], [an], "2027-01", None)
    assert svc.money.unpaid(tid) == []
    entry = next(v for v in svc.undo.recent(tid, OWNER) if v["action"] == "plan.window")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert svc.money.unpaid(tid)[0]["total"] == 600000
    assert svc.plans.get_plan(tid, OWNER, p["id"])["members"][0]["from"] == "2026-10"


def test_moving_package_after_a_later_start():
    """Khánh Huy's case: his 600k quarter is pushed to next quarter (this quarter's bill and its fine go),
    then he moves to a 200k package for this quarter only: he's billed 200k for this quarter."""
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    kh, = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "Khánh Huy"}])]
    big = svc.plans.save_plan(tid, OWNER, 600000, 3, [kh])
    svc.plans.set_terms(tid, OWNER, big["id"], late_fee=1000)
    assert svc.money.unpaid(tid)[0]["total"] > 600000, "the quarter plus its fine"
    svc.plans.set_window(tid, OWNER, big["id"], [kh], "2027-01", None)
    assert svc.money.unpaid(tid) == [], "nothing this quarter, the fine went with the bill"
    small = svc.plans.save_plan(tid, OWNER, 200000, 3, [kh])
    svc.plans.set_window(tid, OWNER, small["id"], [kh], "2026-10", "2026-12")
    bills = svc.money.unpaid(tid)[0]["bills"]
    assert [(b["kind"], b["period_label"], b["amount"]) for b in bills] == [("monthly", "10/2026–12/2026", 200000)]


def test_matches():
    """Matches one by one, a weekly calendar filling 8 weeks, the next 5 still on, cancelling and restoring."""
    from datetime import date
    from app.domain.rules import schedule_dates
    from app.domain.entities import MatchSchedule
    svc, clock, notifier = make_services(datetime(2026, 11, 18, 10, 0))  # a Wednesday
    tid = setup_team(svc)
    m = svc.matches
    assert code(m.add, tid, MEMBER, [{"day": date(2026, 11, 22)}]) == "not_manager"
    m.add(tid, OWNER, [{"day": date(2026, 11, 22), "at": "19:00", "opponent": "FC Tăng High", "place": "Sân A"},
                       {"day": date(2026, 11, 29)}])
    assert code(m.add, tid, OWNER, [{"day": date(2026, 11, 22), "at": "25:00"}]) == "invalid"
    s = m.add_schedule(tid, OWNER, "weekly", weekday=6, at="19:00", place="Sân Mỹ Đình")
    v = m.show(tid, MEMBER)
    assert not v["can_edit"] and len(v["matches"]) == 5 and v["more"]
    days = [x["day"] for x in v["matches"]]
    assert days == ["2026-11-22", "2026-11-29", "2026-12-06", "2026-12-13", "2026-12-20"], "no second match a day"
    assert len([x for x in m.show(tid, OWNER, limit=200)["matches"] if x["schedule_id"] == s["id"]]) == 6

    first = v["matches"][0]["id"]
    m.cancel(tid, OWNER, first, "Mưa")
    v = m.show(tid, OWNER)
    on = [x for x in v["matches"] if x["status"] == "scheduled"]
    assert len(on) == 5 and v["matches"][0]["status"] == "cancelled" and v["matches"][0]["reason"] == "Mưa"
    assert code(m.cancel, tid, OWNER, first) == "already_resolved"
    m.restore(tid, OWNER, first)
    assert m.show(tid, OWNER)["matches"][0]["status"] == "scheduled"
    assert m.cancel_on(tid, OWNER, date(2026, 11, 29), "Đối huỷ")["status"] == "cancelled"
    m.add(tid, OWNER, [{"day": date(2026, 11, 22), "at": "07:00"}])
    assert code(m.cancel_on, tid, OWNER, date(2026, 11, 22)) == "match_ambiguous"

    dec6 = next(x for x in m.show(tid, OWNER)["matches"] if x["day"] == "2026-12-06")
    upd = m.update(tid, OWNER, dec6["id"], {"opponent": "FC Moreh", "note": "áo trắng"})
    assert (upd["opponent"], upd["note"]) == ("FC Moreh", "áo trắng")
    stopped = m.stop_schedule(tid, OWNER, s["id"])
    assert stopped["dropped"] == 5, "the empty calendar dates go, the one with an opponent stays"
    assert m.suggest_days(tid, OWNER)[0] == "2026-11-21", "no calendar: the next weekend days"

    # monthly and every-2-weeks calendars
    last_sat = MatchSchedule(team_id=tid, every="monthly", weekday=5, nth=-1)
    assert schedule_dates(last_sat, date(2026, 11, 1), date(2027, 1, 31)) == \
        [date(2026, 11, 28), date(2026, 12, 26), date(2027, 1, 30)]
    fixed = MatchSchedule(team_id=tid, every="monthly", mday=31)
    assert schedule_dates(fixed, date(2026, 11, 1), date(2026, 12, 31)) == [date(2026, 11, 30), date(2026, 12, 31)]
    two = MatchSchedule(team_id=tid, every="biweekly", anchor=date(2026, 11, 7))
    assert schedule_dates(two, date(2026, 11, 18), date(2026, 12, 20)) == \
        [date(2026, 11, 21), date(2026, 12, 5), date(2026, 12, 19)]

    # reminders: managers 7 days before an open date, followers the evening before
    svc.teams.follow(tid, MEMBER)
    m.add(tid, OWNER, [{"day": date(2026, 11, 25)}, {"day": date(2026, 11, 19), "opponent": "FC Moreh"}])
    svc.jobs.matches()
    assert [e["type"] for e in notifier.events if e["type"].startswith("match_")] == ["match_no_opponent"]
    clock.local = clock.local.replace(hour=18)
    svc.jobs.matches()
    svc.jobs.matches()
    kinds = [e["type"] for e in notifier.events if e["type"].startswith("match_")]
    assert kinds == ["match_no_opponent", "match_tomorrow"], "once a day each"

    # undo
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "match.add")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert not [x for x in m.show(tid, OWNER, 50)["matches"] if x["day"] == "2026-11-25"]


def test_contacts():
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    c = svc.contacts
    items = [{"name": "FC Moreh", "phone": "0367000000", "note": "fb: Dũng Minh"}, {"name": "FC Sóc Sơn"}]
    assert code(c.save_many, tid, MEMBER, items) == "not_manager"
    assert [p["update"] for p in c.preview(tid, OWNER, items)] == [False, False]
    assert c.save_many(tid, OWNER, items) == {"added": 2, "updated": 0}
    assert [p["update"] for p in c.preview(tid, OWNER, [{"name": "fc soc son", "phone": "0584000000"}])] == [True]
    assert c.save_many(tid, OWNER, [{"name": "FC Soc Son", "phone": "0584000000"}]) == {"added": 0, "updated": 1}
    assert c.save_many(tid, OWNER, [{"name": "FCSocSon", "note": "Anh Sơn"}]) == {"added": 0, "updated": 1}, \
        "spaces don't make a new team"
    c.save_many(tid, OWNER, [{"name": "FC Soc Son"}])
    assert code(c.save_many, tid, OWNER, [{"name": "X", "phone": "12345"}]) == "invalid"
    found = c.search(tid, OWNER, "soc son")
    assert len(found) == 1 and found[0]["phone"] == "0584000000"
    assert [x["name"] for x in c.search(tid, OWNER, "0584")] == ["FC Soc Son"]
    assert c.update(tid, OWNER, found[0]["id"], {"name": "FC Sóc Sơn"})["name"] == "FC Sóc Sơn"
    assert c.update(tid, OWNER, found[0]["id"], {"note": None})["note"] is None
    assert [x["name"] for x in c.search(tid, OWNER, "dung minh")] == ["FC Moreh"]
    from datetime import date
    svc.matches.add(tid, OWNER, [{"day": date(2026, 11, 8), "opponent": "fc moreh"},
                                 {"day": date(2026, 12, 6), "opponent": "FC Moreh"}])
    moreh = next(x for x in c.search(tid, OWNER) if x["name"] == "FC Moreh")
    info = c.get(tid, OWNER, moreh["id"])
    assert (info["played"], info["last"], info["next"]) == (1, "2026-11-08", "2026-12-06")
    mid = svc.matches.show(tid, OWNER)["matches"][0]["id"]
    got = svc.matches.get(tid, OWNER, mid)
    assert (got["contact_id"], got["contact_phone"]) == (moreh["id"], "0367000000")
    assert c.get(tid, MEMBER, moreh["id"])["phone"] == "0367000000", "everyone can look a contact up"
    assert code(c.delete, tid, MEMBER, moreh["id"]) == "not_manager"
    c.delete(tid, OWNER, moreh["id"])
    assert [x["name"] for x in c.search(tid, OWNER)] == ["FC Sóc Sơn"]


def test_pay_page_shows_matches_and_contacts():
    """The team chose to show contacts, phones included, on the pay page, next to the match calendar."""
    from datetime import date
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    svc.contacts.save_many(tid, OWNER, [{"name": "FC Moreh", "phone": "0912345678", "note": "fb: Dũng"}])
    svc.matches.add(tid, OWNER, [{"day": date(2026, 11, 22), "at": "19:00", "opponent": "FC Moreh"}])
    token = svc.teams.web_link(tid, OWNER)["url"].rstrip("/").rsplit("/", 1)[-1]
    page = svc.web.page(token)
    assert [(m["day"], m["opponent"]) for m in page["matches"]] == [("2026-11-22", "FC Moreh")]
    assert page["contacts"] == [{"name": "FC Moreh", "phone": "0912345678", "note": "fb: Dũng", "played": 0,
                                 "cancelled": 0, "last": None, "next": "2026-11-22"}]


def test_match_money():
    """After a match, a manager records what we paid the opponent or what they paid us: it's in the fund on
    the match day, shows on the match, and undo takes both back."""
    from datetime import date
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    past, ahead = [x["id"] for x in svc.matches.add(tid, OWNER, [{"day": date(2026, 11, 15), "opponent": "FC Moreh"},
                                                                {"day": date(2026, 11, 22)}])]
    assert code(svc.matches.record_money, tid, MEMBER, past, "paid", 300000) == "not_manager"
    r = svc.matches.record_money(tid, OWNER, past, "paid", 300000, "tiền sân")
    assert (r["title"], r["balance"], r["match"]["paid"]) == ("Trận 15/11 vs FC Moreh: tiền sân", -300000, 300000)
    r = svc.matches.record_money(tid, OWNER, past, "received", 500000)
    assert (r["title"], r["balance"], r["match"]["received"]) == ("Trận 15/11 vs FC Moreh", 200000, 500000)
    r = svc.matches.record_money(tid, OWNER, ahead, "paid", 100000, "cọc sân")  # a deposit for a match ahead
    assert (r["title"], r["at"][:5], r["backdated"]) == ("Trận 22/11: cọc sân", "18/11", False)
    svc.undo.undo(tid, OWNER, next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "match.money")["id"])
    nov = svc.money.history(tid, 10, "2026-11")
    assert sorted((x["amount"], x["at"][:5]) for x in nov) == [(300000, "15/11"), (500000, "15/11")], nov
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "match.money")
    svc.undo.undo(tid, OWNER, entry["id"])
    got = svc.matches.get(tid, OWNER, past)
    assert (got["paid"], got["received"]) == (300000, None), "undo takes back the income and the match's total"
    assert svc.money.fund(tid)["balance"] == -300000


def test_default_package_cycle():
    """The default package can be paid per quarter (or 6 months, a year) like any plan; own fees stay monthly."""
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    an, binh = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"}])]
    svc.members.update_member(tid, OWNER, binh, {"fee_override": 100000})
    svc.teams.update_settings(tid, OWNER, fee=150000)
    assert [b["period_label"] for g in svc.money.unpaid(tid) for b in g["bills"]] == ["11/2026", "11/2026"]
    assert code(svc.teams.update_settings, tid, OWNER, fee_months=2) == "invalid"
    r = svc.teams.update_settings(tid, OWNER, fee_months=3)
    assert (r["fee"], r["fee_months"]) == (150000, 3)
    bills = {g["name"]: [(b["period_label"], b["amount"]) for b in g["bills"]] for g in svc.money.unpaid(tid)}
    assert bills == {"An": [("10/2026–12/2026", 150000)], "Bình": [("11/2026", 100000)]}, bills
    assert svc.plans.list_plans(tid, OWNER)["fee_months"] == 3
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "team.settings")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert {g["name"]: g["bills"][0]["period_label"] for g in svc.money.unpaid(tid)}["An"] == "11/2026"


def test_several_plans_without_overlap():
    """A member can be on several plans, one at a time: 600k for 2026Q4, then 200k from 2027Q1. An overlap is
    refused with the clashing plan named; months no plan covers fall back to the default package."""
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    kh, an = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "Khải"}, {"name": "An"}])]
    big = svc.plans.save_plan(tid, OWNER, 600000, 3, [kh])
    assert big["conflicts"] == []
    small = svc.plans.save_plan(tid, OWNER, 200000, 3, [kh, an])
    assert [c["member"] for c in small["conflicts"]] == ["Khải"], "Khải is on 600k for good: not added"
    assert [m["name"] for m in small["members"]] == ["An"]
    with pytest.raises(DomainError) as e:
        svc.plans.set_window(tid, OWNER, small["id"], [kh], "2026-10", None)
    assert e.value.code == "plan_overlap" and e.value.extra["names"] == "Khải"
    assert e.value.extra["clashes"][0]["amount"] == 600000

    svc.plans.set_window(tid, OWNER, big["id"], [kh], "2026-10", "2026-12")  # 600k: this quarter only
    svc.plans.set_window(tid, OWNER, small["id"], [kh], "2027-01", None)     # then 200k
    view = svc.plans.list_plans(tid, OWNER)
    on = {p["amount"]: [(m["name"], m["from"], m["until"]) for m in p["members"]] for p in view["plans"]}
    assert on == {200000: [("An", "2026-10", None), ("Khải", "2027-01", None)], 600000: [("Khải", "2026-10", "2026-12")]}
    assert code(svc.plans.set_window, tid, OWNER, big["id"], [kh], "2026-10", "2027-03") == "plan_overlap"
    card = next(m for m in svc.members.list_members(tid, OWNER) if m["id"] == kh)
    assert [(p["amount"], p["from"], p["until"], p["now"]) for p in card["plans"]] == [
        (600000, "2026-10", "2026-12", True), (200000, "2027-01", None, False)], "the card lists every package"

    fee = lambda name: [(b["period_label"], b["amount"]) for g in svc.money.unpaid(tid) if g["name"] == name  # noqa: E731
                        for b in g["bills"]]
    assert fee("Khải") == [("10/2026–12/2026", 600000)]
    clock.local = clock.local.replace(year=2027, month=1, day=2)
    svc.jobs.open_month()
    assert fee("Khải")[-1] == ("01/2027–03/2027", 200000)

    # off one plan, the other stays; a plan nobody is on goes
    svc.plans.remove_members(tid, OWNER, big["id"], [kh])
    assert [p["amount"] for p in svc.plans.list_plans(tid, OWNER)["plans"]] == [200000]
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "plan.save")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert sorted(p["amount"] for p in svc.plans.list_plans(tid, OWNER)["plans"]) == [200000, 600000]



def test_donation(world):
    """Anyone can make a QR for any multiple of 1.000đ; whatever arrives with its code goes to the fund,
    labelled with the donor, and everyone following the bot is thanked."""
    svc, clock, notifier = world
    tid = setup_team(svc)
    assert code(svc.money.donate, tid, MEMBER, 50000, "Khách") == "bank_not_set"
    link_bank(svc, tid)
    for bad in (0, 999, 50500, 1500.0, "50000", True, 50_001_000):
        assert code(svc.money.donate, tid, MEMBER, bad, "Khách") == "donation_amount", bad
    q = svc.money.donate(tid, MEMBER, 50000, "  Anh   Tuấn  ")
    assert q["amount"] == 50000 and q["members"] == ["Anh Tuấn"] and q["donation"] and "qr.sepay.vn" in q["qr_url"]
    key = sepay_key(svc, tid)
    assert svc.money.record_transfer(tid, key, IncomingTransfer("tx-dn", 60000, f"ck {q['content']}")) == "donation_received"
    ev = notifier.events[-1]
    assert ev["donor"] == "Anh Tuấn" and ev["amount"] == 60000 and ev["payer_tg_id"] == MEMBER
    assert svc.money.fund(tid)["balance"] == 60000  # what arrived, not what the QR said
    h = svc.money.history(tid)[0]
    assert (h["source"], h["title"], h["amount"]) == ("donation", "Anh Tuấn", 60000)

    # from the pay page: no name is fine
    token = svc.teams.web_link(tid, OWNER)["url"].rstrip("/").rsplit("/", 1)[1]
    assert code(svc.web.donate, token, 12345, None) == "donation_amount"
    w = svc.web.donate(token, 20000, "")
    assert w["members"] == ["Ẩn danh"] and w["donation"]
    r = svc.web.donate(token, 30000, "Hiệp", "  đá   giao lưu 20/11 ")
    assert r["reason"] == "đá giao lưu 20/11"
    assert code(svc.web.donate, token, 30000, "Hiệp", 5) == "invalid"
    svc.money.record_transfer(tid, key, IncomingTransfer("tx-dn-r", 30000, f"ck {r['content']}"))
    assert ("in", "donation", "Hiệp · đá giao lưu 20/11", 30000) in {(x["type"], x["kind"], x["title"], x["amount"])
                                                                   for x in svc.web.page(token)["history"]}
    assert svc.money.history(tid, 10)[0]["title"] == "Hiệp · đá giao lưu 20/11"
    svc.money.record_transfer(tid, key, IncomingTransfer("tx-dn2", 20000, w["content"]))
    st = svc.web.intent_status(token, w["code"])
    assert st["status"] == "paid" and st["donation"]
    assert ("in", "donation", "Ẩn danh", 20000) in {(x["type"], x["kind"], x["title"], x["amount"])
                                                     for x in svc.web.page(token)["history"]}


def test_edit_text(world):
    """Managers fix the text of expenses, income and donations; the history shows it; /undo puts it back.
    Payments of members' bills aren't offered: they are named after the bills."""
    svc, clock, _ = world
    tid = setup_team(svc)
    link_bank(svc, tid)
    an, = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}])]
    svc.teams.update_settings(tid, OWNER, fee=100000)
    svc.money.unpaid(tid)
    svc.money.markpaid(tid, OWNER, [an])
    svc.money.add_expense(tid, OWNER, 300000, "Sân")
    svc.money.add_income(tid, OWNER, 50000, "Thưởng")
    q = svc.money.donate(tid, MEMBER, 500000, None)
    svc.money.record_transfer(tid, sepay_key(svc, tid), IncomingTransfer("tx-ed", 500000, q["content"]))
    items = svc.money.editable(tid, OWNER)
    assert {(i["source"], i["title"]) for i in items} == {("expense", "Sân"), ("other", "Thưởng"),
                                                          ("donation", "Ẩn danh")}
    assert code(svc.money.editable, tid, MEMBER) == "not_manager"
    dn = next(i for i in items if i["source"] == "donation")
    cash = next(p for p in svc.money.history(tid) if p["source"] == "cash")
    assert cash
    r = svc.money.edit_text(tid, OWNER, dn["kind"], dn["id"], "  Hoài Sơn ·  500k from FC23 ")
    assert (r["old"], r["title"], r["amount"]) == ("Ẩn danh", "Hoài Sơn · 500k from FC23", 500000)
    assert svc.money.history(tid)[0]["title"] == "Hoài Sơn · 500k from FC23"
    ex = next(i for i in items if i["source"] == "expense")
    svc.money.edit_text(tid, OWNER, ex["kind"], ex["id"], "Sân bóng 15/11")
    assert "Sân bóng 15/11" in {h["title"] for h in svc.money.history(tid)}
    assert svc.money.fund(tid)["balance"] == 100000 + 50000 + 500000 - 300000, "only the text changes"
    for bad in ("", "   ", "x" * 201, None):
        assert code(svc.money.edit_text, tid, OWNER, ex["kind"], ex["id"], bad) == "invalid"
    assert code(svc.money.edit_text, tid, OWNER, "e", 9999, "x") == "not_found"
    assert code(svc.money.edit_text, tid, OWNER, "z", ex["id"], "x") == "invalid"
    assert code(svc.money.edit_text, tid, MEMBER, ex["kind"], ex["id"], "x") == "not_manager"
    # a fee payment can't be edited here
    fee_payment = [p for p in svc.money.editable(tid, OWNER, 50)]
    assert all(i["source"] != "cash" for i in fee_payment)
    # undo the donation edit
    act = next(a for a in svc.undo.recent(tid, OWNER) if a["action"] == "entry.edit" and "Hoài" in a["target"])
    svc.undo.undo(tid, OWNER, act["id"])
    assert "Ẩn danh" in {h["title"] for h in svc.money.history(tid)}
    # 5 by default, more on request (up to 50)
    for n in range(12):
        clock.advance(minutes=1)
        svc.money.add_expense(tid, OWNER, 1000, f"Nước {n}")
    assert [i["title"] for i in svc.money.editable(tid, OWNER)] == [f"Nước {n}" for n in range(11, 6, -1)]
    assert len(svc.money.editable(tid, OWNER, 10)) == 10 and len(svc.money.editable(tid, OWNER, 20)) == 15
    for bad in (0, 51, "5", True):
        assert code(svc.money.editable, tid, OWNER, bad) == "invalid"


def test_schedule_with_opponent():
    """A calendar against one team (Axon, Monday of week 1; FC KCU, Tuesday of week 2): every match it adds is
    against them. Changing its opponent, time or pitch moves the matches still ahead that nobody changed by hand."""
    from datetime import date
    svc, clock, _ = make_services(datetime(2026, 11, 27, 10, 0))
    tid = setup_team(svc)
    m = svc.matches
    ax = m.add_schedule(tid, OWNER, "monthly", weekday=0, nth=1, at="19:00", place="Sân A", opponent=" Axon ")
    kcu = m.add_schedule(tid, OWNER, "monthly", weekday=1, nth=2, opponent="FC KCU")
    assert ax["opponent"] == "Axon"
    got = {(x["day"], x["opponent"]) for x in m.show(tid, OWNER, 200)["matches"]}
    assert {("2026-12-07", "Axon"), ("2027-01-04", "Axon"), ("2026-12-08", "FC KCU"), ("2027-01-12", "FC KCU")} <= got
    assert [s["id"] for s in m.schedules_with(tid, OWNER, "axon")] == [ax["id"]], "accents and case ignored"
    assert code(m.schedules_with, tid, MEMBER, "Axon") == "not_manager"

    # one Axon match changed by hand keeps its own opponent
    jan = next(x for x in m.show(tid, OWNER, 200)["matches"] if x["day"] == "2027-01-04")
    m.update(tid, OWNER, jan["id"], {"opponent": "FC Moreh"})
    r = m.update_schedule(tid, OWNER, ax["id"], {"opponent": "Axon United", "at": "20:00"})
    assert r["opponent"] == "Axon United" and r["moved"] == 2
    ms = {x["day"]: x for x in m.show(tid, OWNER, 200)["matches"]}
    assert (ms["2026-12-07"]["opponent"], ms["2026-12-07"]["at"]) == ("Axon United", "20:00")
    assert (ms["2027-01-04"]["opponent"], ms["2027-01-04"]["at"]) == ("FC Moreh", "20:00")
    assert m.get_schedule(tid, OWNER, ax["id"])["next"][0]["day"] == "2026-12-07"
    for bad in ({}, {"day": 1}, {"at": "25:00"}, {"opponent": "x" * 101}):
        assert code(m.update_schedule, tid, OWNER, ax["id"], bad) == "invalid"
    assert code(m.update_schedule, tid, MEMBER, ax["id"], {"at": None}) == "not_manager"

    # undo puts the calendar and its matches back
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "schedule.update")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert m.get_schedule(tid, OWNER, ax["id"])["opponent"] == "Axon"
    assert {x["day"]: x["opponent"] for x in m.show(tid, OWNER, 200)["matches"]}["2026-12-07"] == "Axon"

    # the same days again: the calendar we have is updated, not doubled (what's given replaces, the rest stays)
    again = m.add_schedule(tid, OWNER, "monthly", weekday=0, nth=1, opponent="Axon B")
    assert again["updated"] and again["id"] == ax["id"] and again["moved"] == 1
    assert (again["opponent"], again["at"], again["place"]) == ("Axon B", "19:00", "Sân A")
    assert len(m.show(tid, OWNER)["schedules"]) == 2
    assert {x["day"]: x["opponent"] for x in m.show(tid, OWNER, 200)["matches"]}["2026-12-07"] == "Axon B"
    # sharing a day with another calendar is refused: every Monday meets Monday of week 1
    err = None
    try:
        m.add_schedule(tid, OWNER, "weekly", weekday=0, opponent="FC X")
    except DomainError as e:
        err = e
    assert err and err.code == "schedule_overlap" and err.extra["schedule"]["id"] == ax["id"]
    assert err.extra["days"][0] == "2026-12-07" and err.extra["count"] >= 12
    assert code(m.add_schedule, tid, OWNER, "monthly", mday=7, at="21:00") == "schedule_overlap", \
        "the 7th is a week-1 Monday sometimes, at another time"
    assert len(m.show(tid, OWNER)["schedules"]) == 2, "nothing created"
    # days with a match already are skipped and reported
    m.add(tid, OWNER, [{"day": date(2026, 12, 15), "opponent": "Giao hữu"}])
    ok = m.add_schedule(tid, OWNER, "monthly", mday=15)
    assert not ok["updated"] and ok["skipped"] == 1 and ok["skipped_days"] == ["2026-12-15"]
    m.stop_schedule(tid, OWNER, ok["id"])
    m.delete(tid, OWNER, next(x["id"] for x in m.show(tid, OWNER, 200)["matches"] if x["opponent"] == "Giao hữu"))

    # stopping drops the calendar's own untouched matches, keeps the hand-changed one
    st = m.stop_schedule(tid, OWNER, kcu["id"])
    assert st["dropped"] >= 2 and all(x["opponent"] != "FC KCU" for x in m.show(tid, OWNER, 200)["matches"])
    assert code(m.get_schedule, tid, OWNER, kcu["id"]) == "not_found"
    m.stop_schedule(tid, OWNER, ax["id"])
    assert [x["opponent"] for x in m.show(tid, OWNER, 200)["matches"]] == ["FC Moreh"]


def test_calendar_layering():
    """Every Monday at 19:00, and Axon on the Monday of week 3 at 19:00: allowed, and those Mondays are Axon's
    (the matches already there are taken over). Another time, or a second fixed opponent, is refused."""
    from datetime import date
    svc, clock, _ = make_services(datetime(2026, 11, 27, 10, 0))
    tid = setup_team(svc)
    m = svc.matches
    mon = m.add_schedule(tid, OWNER, "weekly", weekday=0, at="19:00", place="ĐHSP")
    assert code(m.add_schedule, tid, OWNER, "monthly", weekday=0, nth=3, at="20:00", opponent="Axon") == "schedule_overlap"
    ax = m.add_schedule(tid, OWNER, "monthly", weekday=0, nth=3, at="19:00", opponent="Axon")
    assert ax["took"] == 2 and ax["skipped"] == 0, ax  # 21/12 and 18/01 were already filled by every Monday
    assert ax["shared"][0]["schedule"]["id"] == mon["id"] and ax["shared"][0]["ours"]
    ms = {x["day"]: x for x in m.show(tid, OWNER, 200)["matches"]}
    assert (ms["2026-12-21"]["opponent"], ms["2026-12-21"]["place"], ms["2026-12-21"]["schedule_id"]) == ("Axon", "ĐHSP", ax["id"])
    assert ms["2026-12-14"]["opponent"] is None and ms["2026-12-14"]["schedule_id"] == mon["id"]
    assert len([d for d in ms if d.startswith("2026-12-21")]) == 1, "one match that day"
    # a second fixed opponent on those days is refused, even at the same time
    err = None
    try:
        m.add_schedule(tid, OWNER, "monthly", mday=21, at="19:00", opponent="KCU")
    except DomainError as e:
        err = e
    assert err and err.code == "schedule_overlap" and err.extra["schedule"]["id"] == ax["id"] and err.extra["why"] == "opponent"
    # stopping Axon's calendar gives those Mondays back to every Monday
    m.stop_schedule(tid, OWNER, ax["id"])
    ms = {x["day"]: x for x in m.show(tid, OWNER, 200)["matches"]}
    assert ms["2026-12-21"]["opponent"] is None and ms["2026-12-21"]["schedule_id"] == mon["id"]
    # the general calendar added after the specific one: the specific keeps its days
    m.stop_schedule(tid, OWNER, mon["id"])
    kcu = m.add_schedule(tid, OWNER, "monthly", weekday=1, nth=2, opponent="KCU")
    tue = m.add_schedule(tid, OWNER, "weekly", weekday=1)
    assert tue["skipped"] == 0 and not tue["shared"][0]["ours"]
    ms = {x["day"]: x for x in m.show(tid, OWNER, 200)["matches"]}
    assert ms["2026-12-08"]["opponent"] == "KCU" and ms["2026-12-15"]["schedule_id"] == tue["id"]
    # undo the general one: KCU's stay
    entry = next(x for x in svc.undo.recent(tid, OWNER) if x["action"] == "schedule.add")
    svc.undo.undo(tid, OWNER, entry["id"])
    assert kcu["id"] in [x["id"] for x in m.show(tid, OWNER)["schedules"]]


def test_two_teams_on_one_bank_account(world):
    """Two teams linked to the same bank account both hear every transfer. A transfer with the other team's
    code is that team's, and one with no code at all is ignored too: only this team's QR codes count here."""
    svc, clock, notifier = world
    a = setup_team(svc)
    req = svc.registration.request_team(OWNER, "FC Two")
    svc.registration.decide_team_request(SUPER, req["id"], True)
    b = svc.registration.create_team(OWNER, "2:secret", 556, "two_bot", None)["team_id"]
    link_bank(svc, a)
    link_bank(svc, b)
    ka, kb = sepay_key(svc, a), sepay_key(svc, b)
    q = svc.money.donate(a, MEMBER, 40000, "X")
    tx = IncomingTransfer("100517447", 40000, f"CT DEN:124 {q['content']} BankAPINotify")
    assert svc.money.record_transfer(a, ka, tx) == "donation_received"
    assert svc.money.record_transfer(b, kb, tx) is None, "the other team's code: not this team's money"
    assert svc.money.fund(b) == {"income": 0, "other_income": 0, "expense": 0, "balance": 0, "unmatched": 0}
    assert svc.money.record_transfer(b, kb, IncomingTransfer("100517448", 30000, "chuyen tien")) is None, \
        "a shared account: no code of this team, ignored"
    assert svc.money.unmatched(b, OWNER) == [] and svc.money.fund(b)["balance"] == 0


def test_match_fee():
    """Players of one match (past or ahead) pay a fee a manager sets for it: a bill per player titled after the
    match, in the match's month; nobody is charged twice; the card shows who paid; undo takes it back."""
    from datetime import date
    svc, clock, _ = make_services(datetime(2026, 11, 18, 10, 0))
    tid = setup_team(svc)
    link_bank(svc, tid)
    an, binh, cuong = [m["id"] for m in svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"},
                                                                             {"name": "Cường"}])]
    past, ahead = [x["id"] for x in svc.matches.add(tid, OWNER, [{"day": date(2026, 11, 15), "opponent": "FC Moreh"},
                                                                {"day": date(2026, 12, 6)}])]
    assert code(svc.matches.charge_players, tid, MEMBER, past, 50000, [an]) == "not_manager"
    for bad in (0, 1500, "50000", True):
        assert code(svc.matches.charge_players, tid, OWNER, past, bad, [an]) == "match_fee_amount"
    r = svc.matches.charge_players(tid, OWNER, past, 50000, [an, binh])
    assert r["charged"] == ["An", "Bình"] and r["title"] == "Trận 15/11 vs FC Moreh" and r["match"]["fee"] == 50000
    again = svc.matches.charge_players(tid, OWNER, past, 50000, [an, cuong])
    assert again["charged"] == ["Cường"] and again["skipped"] == 1, "An already charged for this match"
    owed = {g["name"]: [(b["title"], b["amount"]) for b in g["bills"] if b["kind"] == "charge"] for g in svc.money.unpaid(tid)}
    assert owed["An"] == [("Trận 15/11 vs FC Moreh", 50000)]

    fut = svc.matches.charge_players(tid, OWNER, ahead, 30000, [an])  # a match ahead: billed in its month
    assert fut["title"] == "Trận 06/12"
    an_bills = [b for g in svc.money.unpaid(tid) if g["name"] == "An" for b in g["bills"]]
    assert ("Trận 06/12", "12/2026") in {(b["title"], b["period_label"]) for b in an_bills}

    svc.money.markpaid(tid, OWNER, [binh])
    card = svc.matches.get(tid, OWNER, past)
    assert [(p["name"], p["paid"]) for p in card["players"]] == [("An", False), ("Bình", True), ("Cường", False)]
    act = next(a for a in svc.undo.recent(tid, OWNER) if a["action"] == "match.fee")
    svc.undo.undo(tid, OWNER, act["id"])
    assert not any(b["title"] == "Trận 06/12" for g in svc.money.unpaid(tid) for b in g["bills"]), "undo takes it back"


def test_who_opened_the_bot(world):
    svc, _, _ = world
    tid = setup_team(svc)
    svc.members.add_members(tid, OWNER, [{"name": "An"}, {"name": "Bình"}])
    svc.users.upsert_user(MEMBER, "Khải", "Trần", "khai")
    svc.teams.follow(tid, OWNER)
    svc.teams.follow(tid, MEMBER)
    svc.teams.set_muted(tid, MEMBER, True)
    r = svc.teams.bot_users(tid, OWNER)
    assert (len(r["people"]), r["on"], r["members"]) == (2, 1, 2)
    owner, member = r["people"]
    assert owner["role"] == "owner" and not owner["muted"]
    assert member["name"] == "Khải Trần" and member["username"] == "khai" and member["role"] is None and member["muted"]
    assert code(svc.teams.bot_users, tid, MEMBER) == "not_manager"


def test_ops_team_checks(world):
    """What /ops and the daily check read from the database: stale unmatched money, and a team whose QR codes
    get made but SePay never reports a transfer (a broken webhook)."""
    svc, clock, _ = world
    tid = setup_team(svc)
    svc.teams.update_settings(tid, OWNER, fee=200000)
    link_bank(svc, tid)
    key = sepay_key(svc, tid)
    an = svc.members.add_members(tid, OWNER, [{"name": "An"}])[0]["id"]
    assert code(svc.ops.teams, OWNER) == "not_super"
    assert svc.ops.db_ok()

    row = svc.ops.teams(SUPER)[0]
    assert (row["unmatched"], row["unmatched_stale"], row["sepay_quiet"], row["last_transfer"]) == (0, False, False, None)

    svc.money.unpaid(tid)  # this month's bills
    for _ in range(3):  # QR codes made, nothing ever arrives
        svc.money.create_intent(tid, OWNER, [an])
    row = svc.ops.teams(None)[0]
    assert row["qr_made_7d"] == 3 and row["qr_paid_7d"] == 0 and row["qr_open"] == 3 and row["sepay_quiet"]

    # a transfer without a code arrives: SePay is reaching us, but someone must assign it
    svc.money.record_transfer(tid, key, IncomingTransfer("tx-ops", 50000, "chuyen tien"))
    row = svc.ops.teams(None)[0]
    assert not row["sepay_quiet"] and row["last_transfer_days"] == 0
    assert (row["unmatched"], row["unmatched_amount"], row["unmatched_stale"]) == (1, 50000, False)
    clock.advance(days=4)
    row = svc.ops.teams(None)[0]
    assert row["unmatched_stale"] and row["unmatched_oldest_days"] == 4 and row["qr_open"] == 0



def test_sepay_cycle():
    from datetime import date
    from app.domain.rules import sepay_cycle
    assert sepay_cycle(date(2026, 11, 19), None) == (date(2026, 11, 1), date(2026, 12, 1))
    assert sepay_cycle(date(2026, 11, 19), 15) == (date(2026, 11, 15), date(2026, 12, 15))
    assert sepay_cycle(date(2026, 11, 10), 15) == (date(2026, 10, 15), date(2026, 11, 15))
    assert sepay_cycle(date(2026, 1, 5), 15) == (date(2025, 12, 15), date(2026, 1, 15))
    assert sepay_cycle(date(2026, 2, 28), 31) == (date(2026, 2, 28), date(2026, 3, 31)), "short months: their last day"


def test_sepay_plan_usage(world):
    """The system admin tells the plan (master bot); /ops counts this cycle's transactions once per SePay account."""
    from datetime import date
    from .fakes import FakeSepay
    svc, clock, _ = world
    tid = setup_team(svc)
    assert svc.ops.sepay_usage(SUPER) == [], "no token, nothing to read"
    link_bank(svc, tid)
    FakeSepay.transactions = [date(2026, 10, 25)] + [date(2026, 11, 2)] * 41
    try:
        assert code(svc.ops.sepay_usage, OWNER) == "not_super"
        assert code(svc.ops.set_sepay_plan, OWNER, 0, 50) == "not_super", "the team owner can't"
        assert code(svc.ops.set_sepay_plan, SUPER, 1, 50) == "invalid"
        assert code(svc.ops.set_sepay_plan, SUPER, 0, 50, 32) == "invalid"
        row = svc.ops.sepay_usage(SUPER)[0]
        assert (row["used"], row["quota"], row["near"]) == (41, None, False), "a count without a limit"
        assert svc.ops.set_sepay_plan(SUPER, 0, 50)["day"] == 1
        row = svc.ops.sepay_usage(None)[0]
        assert (row["used"], row["pct"], row["near"], row["resets"]) == (41, 82, True, "01/12")
        svc.ops.set_sepay_plan(SUPER, 0, 50, 21)  # cycle from 21/10: the October one counts too
        assert svc.ops.sepay_usage(None)[0]["used"] == 42
        svc.ops.set_sepay_plan(SUPER, 0, None)
        assert svc.ops.sepay_usage(None)[0]["quota"] is None
    finally:
        FakeSepay.transactions = []
