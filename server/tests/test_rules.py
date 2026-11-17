"""Domain rules: pure functions, no database."""
from app.domain.entities import Member, Team
from app.domain.rules import (due_day_in_month, effective_fee, find_payment_codes, is_last_day_of_month,
                              new_payment_code, period_label, should_remind, split_share, valid_bank)


def team(fee=200000):
    return Team(name="T", bot_id=1, bot_username="b", bot_token_enc="x", webhook_secret="s", owner_tg_id=1, fee=fee)


def test_fees():
    assert effective_fee(team(), Member(team_id=1, name="An")) == 200000
    assert effective_fee(team(), Member(team_id=1, name="An", fee_override=100000)) == 100000
    assert effective_fee(team(), Member(team_id=1, name="An", fee_override=0)) == 0
    assert split_share(500000, 3) == 167000
    assert split_share(600000, 3) == 200000
    assert period_label("2026-11") == "11/2026"


def test_payment_codes():
    code = new_payment_code()
    assert len(code) == 10 and code.startswith("TF")
    assert find_payment_codes(f"NGUYEN VAN A chuyen tien {code[:4]}-{code[4:]} FT123") == [code]
    assert find_payment_codes("GIFT" + code)[-1] == code  # an earlier "TF" doesn't hide the real code
    assert find_payment_codes("no code here") == []


def test_reminder_schedule():
    assert [d for d in range(1, 16) if should_remind(d, 5)] == [2, 5, 8, 11, 14]
    assert due_day_in_month(31, 2026, 2) == 28
    assert is_last_day_of_month(2026, 2, 28) and not is_last_day_of_month(2026, 3, 30)


def test_bank_validation():
    assert valid_bank("MBBank", "0123456789", "NGUYEN VAN A")
    assert not valid_bank("MBBank", "0123-456", "NGUYEN VAN A")
