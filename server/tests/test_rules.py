"""Domain rules: pure functions, no database."""
from app.domain.entities import Member, Team
from app.domain.rules import (add_months, covers, months_between, effective_fee, find_payment_codes, is_last_day_of_month,
                              new_payment_code, period_label, should_remind, due_date, notify_date, late_days, schedule, terms, split_share, transfer_content,
                              valid_bank)


def team(fee=200000):
    return Team(name="T", bot_id=1, bot_username="b", bot_token_enc="x", webhook_secret="s", owner_tg_id=1, fee=fee)


def test_fees():
    assert effective_fee(team(), Member(team_id=1, name="An")) == 200000
    assert effective_fee(team(), Member(team_id=1, name="An", fee_override=100000)) == 100000
    assert effective_fee(team(), Member(team_id=1, name="An", fee_override=0)) == 0
    assert split_share(500000, 3) == 167000
    assert split_share(600000, 3) == 200000
    assert period_label("2026-11") == "11/2026"
    assert period_label("2026-11", 3) == "11/2026–01/2027"


def test_cycles():
    assert add_months("2026-11", 3) == "2027-02" and add_months("2026-01", -1) == "2025-12"
    assert months_between("2026-11", "2027-01") == 2
    assert [covers("2026-11", 3, p) for p in ("2026-10", "2026-11", "2027-01", "2027-02")] == [False, True, True, False]
    assert covers("2026-11", None, "2026-11") and not covers("2026-11", None, "2026-12")


def test_payment_codes():
    code = new_payment_code()
    assert len(code) == 10 and code.startswith("TF")
    assert find_payment_codes(f"NGUYEN VAN A chuyen tien {code[:4]}-{code[4:]} FT123") == [code]
    assert find_payment_codes("GIFT" + code)[-1] == code  # an earlier "TF" doesn't hide the real code
    assert find_payment_codes("no code here") == []


def test_transfer_content():
    # SePay only reports VietinBank transfers whose content starts with SEVQR
    for bank in ("VietinBank", "Vietinbank", "ICB", "CTG"):
        assert transfer_content(bank, "TFABCDEFGH") == "SEVQR TFABCDEFGH"
    assert transfer_content("MBBank", "TFABCDEFGH") == "TFABCDEFGH"
    assert find_payment_codes("CT DEN:440T26B0 SEVQR TFABCDEFGH chuyen khoan") == ["TFABCDEFGH"]


def test_reminder_schedule():
    from datetime import date
    t = team()
    assert schedule(t) == (1, 4)  # an old team: announced on the 1st, due on its old due day (5)
    t.notify_day, t.due_days = 15, 5
    assert (notify_date(terms(t), "2026-11"), due_date(terms(t), "2026-11")) == (date(2026, 11, 15), date(2026, 11, 20))
    t.notify_day, t.due_days = 25, 10
    assert due_date(terms(t), "2026-11") == date(2026, 12, 5), "a deadline can run into next month"
    t.notify_day, t.due_days = 15, 5
    n, d = notify_date(terms(t), "2026-11"), due_date(terms(t), "2026-11")
    assert [x for x in range(10, 31) if should_remind(date(2026, 11, x), n, d)] == [17, 20, 23, 26, 29]
    t.notify_day, t.due_days = 15, 2  # 3 days before the due date is before the announcement: skipped
    assert not should_remind(date(2026, 11, 14), notify_date(terms(t), "2026-11"), due_date(terms(t), "2026-11"))
    from app.domain.entities import FeePlan
    t.late_fee = 1000
    plan = FeePlan(team_id=1, amount=600000, months=3)
    assert terms(t, plan) == terms(t), "a plan with nothing of its own follows the default"
    plan.notify_day, plan.due_days, plan.late_fee = 1, 10, 0
    assert (terms(t, plan).notify_day, terms(t, plan).due_days, terms(t, plan).late_fee) == (1, 10, 0)
    assert late_days(date(2026, 11, 20), date(2026, 11, 23), None) == 3
    assert late_days(date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 22)) == 1, "only since it was turned on"
    assert late_days(date(2026, 11, 20), date(2026, 11, 20), None) == 0
    assert is_last_day_of_month(2026, 2, 28) and not is_last_day_of_month(2026, 3, 30)


def test_bank_validation():
    assert valid_bank("MBBank", "0123456789", "NGUYEN VAN A")
    assert not valid_bank("MBBank", "0123-456", "NGUYEN VAN A")


def test_bot_usernames():
    from app.domain.rules import bot_usernames
    names = bot_usernames("FC 37 IT")
    assert names[:2] == ["fc_37_it_fund_bot", "fc_37_it_tf_bot"] and names[2] == "fc_37_it_fund2_bot"
    assert bot_usernames("Đội Bóng Hà Nội")[0] == "doi_bong_ha_noi_fund_bot"
    assert bot_usernames("37 Stars")[0] == "team_37_stars_fund_bot"
    assert bot_usernames("⚽⚽")[0] == "team_fund_bot"
    import re
    for n in bot_usernames("A very very long team name that goes on and on"):
        assert 5 <= len(n) <= 32 and re.fullmatch(r"[a-z][a-z0-9_]*bot", n), n
