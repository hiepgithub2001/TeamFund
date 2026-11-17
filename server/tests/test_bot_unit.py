import re

from app.interfaces.bot.fmt import amount_and_title, money, parse_amount
from app.interfaces.bot.i18n import DICTS, t
from app.interfaces.bot.team import parse_member_lines


def test_parse_amount():
    assert parse_amount("200000") == 200000
    assert parse_amount("200.000") == 200000
    assert parse_amount("200,000đ") == 200000
    assert parse_amount("200k") == 200000
    assert parse_amount("1.5tr") == 1500000
    assert parse_amount("2m") == 2000000
    assert parse_amount("abc") is None
    assert parse_amount("0") is None
    assert amount_and_title("500k Tiền sân") == (500000, "Tiền sân")
    assert amount_and_title("500k") == (None, None)


def test_money():
    assert money(200000, "vi") == "200.000đ"
    assert money(200000, "en") == "200,000đ"


def test_languages_match():
    assert sorted(DICTS["vi"]) == sorted(DICTS["en"])
    ph = lambda s: sorted(re.findall(r"\{\w+\}", s))  # noqa: E731
    for k in DICTS["en"]:
        assert ph(DICTS["vi"][k]) == ph(DICTS["en"][k]), k
    assert t("vi", "setdue.done", day=5) == "✅ Đã đặt hạn đóng là ngày 5."
    DICTS["en"]["test.own_args"] = "{key} {lang}"  # placeholders may be named like t()'s own arguments
    try:
        assert t("en", "test.own_args", key="abc", lang="x") == "abc x"
    finally:
        del DICTS["en"]["test.own_args"]


def test_member_lines():
    assert parse_member_lines("An | 10\nBình\n\nCường | 7 | 0901") == [
        {"name": "An", "shirt_number": "10", "phone": None},
        {"name": "Bình", "shirt_number": None, "phone": None},
        {"name": "Cường", "shirt_number": "7", "phone": "0901"},
    ]


def test_split_date():
    from datetime import date
    from app.interfaces.bot.fmt import split_date
    today = date(2026, 11, 18)
    assert split_date("Pitch 15-10-2026", today) == ("Pitch", date(2026, 10, 15), True)
    assert split_date("Pitch 15/10/2026", today) == ("Pitch", date(2026, 10, 15), True)
    assert split_date("Pitch 15.10.26", today) == ("Pitch", date(2026, 10, 15), True)
    assert split_date("Pitch 15/10", today) == ("Pitch", date(2026, 10, 15), True)
    assert split_date("Pitch 20-12", today) == ("Pitch", date(2025, 12, 20), True)  # no year: not in the future
    assert split_date("Pitch", today) == ("Pitch", None, True)
    assert split_date("Pitch 31-02-2026", today)[2] is False
    assert split_date("Áo số 10", today) == ("Áo số 10", None, True)
