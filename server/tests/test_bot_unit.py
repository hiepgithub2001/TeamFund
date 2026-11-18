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
    assert t("vi", "plans.schedule", notify=15, days=5) == "📅 thông báo ngày 15, hạn +5 ngày"
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
    for text in ("Pitch 15-10-2026", "Pitch 15/10/2026", "Pitch 15/10/26", "Pitch 15-10-26", "Pitch 15/10", "Pitch 15-10"):
        assert split_date(text, today) == ("Pitch", date(2026, 10, 15), True), text
    assert split_date("Pitch 20-12", today) == ("Pitch", date(2026, 12, 20), True)  # this year; the use case refuses the future
    assert split_date("Pitch", today) == ("Pitch", None, True)
    assert split_date("Pitch 31-02-2026", today)[2] is False
    assert split_date("Pitch 15/10-2026", today)[1] is None, "mixed separators aren't a date"
    assert split_date("Áo số 10", today) == ("Áo số 10", None, True)
    assert split_date("Opening 10/2026", today) == ("Opening", date(2026, 10, 1), True)  # month: its 1st day
    assert split_date("Opening 9-2025", today) == ("Opening", date(2025, 9, 1), True)
    assert split_date("Opening 13/2026", today)[2] is False


def test_amount_forms():
    """Every form /money shows."""
    from app.interfaces.bot.fmt import parse_amount
    cases = {"450000": 450000, "450.000": 450000, "450,000đ": 450000, "450k": 450000, "450K": 450000,
             "450 nghìn": 450000, "1tr": 1000000, "1m": 1000000, "1M": 1000000, "1 triệu": 1000000,
             "1.5tr": 1500000, "1,5tr": 1500000, "1tr5": 1500000, "1.5M": 1500000, "5M": 5000000,
             "2tr250": 2250000, "200.000 đồng": 200000, "0": None, "abc": None, "1kk": None}
    for raw, want in cases.items():
        assert parse_amount(raw) == want, raw



def test_parse_periods():
    from app.interfaces.bot.fmt import parse_periods
    q = lambda x: parse_periods(x, 3, "2026-10")  # noqa: E731
    assert q("2026Q4 - 2027Q2") == ("2026-10", "2027-06")
    assert q("2026q4-2027q2") == ("2026-10", "2027-06")
    assert q("Q1/2027 đến Q1/2027") == ("2027-01", "2027-03")
    assert q("2027Q1") == ("2027-01", None)
    assert q("2027Q1 -") == ("2027-01", None)
    assert q("- 2027Q1") == ("2026-10", "2027-03")
    for bad in ("", "-", "2027Q5", "2027Q2 - 2027Q1", "11/2026", "2027", "hello 2027Q1"):
        assert q(bad) is None, bad
    assert parse_periods("2026H2 - 2027H1", 6, "2026-07") == ("2026-07", "2027-06")
    assert parse_periods("2027", 12, "2026-01") == ("2027-01", None)
    assert parse_periods("2026 - 2027", 12, "2026-01") == ("2026-01", "2027-12")
    assert parse_periods("11/2026 - 01/27", 1, "2026-11") == ("2026-11", "2027-01")


def test_charge_picker_buttons():
    """A charge picker starts with everyone; ⬜ Clear all lets a manager pick a few, and the confirm button says
    what the choice costs."""
    from app.interfaces.bot.team import picker_keyboard
    st = {"kind": "split", "options": [{"id": i, "label": n} for i, n in enumerate(["An", "Bình", "Cường"])],
          "selected": [0, 1, 2], "data": {"title": "Áo", "amount": 15000}}
    texts = lambda: [b.text for row in picker_keyboard("vi", st).inline_keyboard for b in row]  # noqa: E731
    assert "⬜ Bỏ chọn hết" in texts() and "✔️ Xác nhận (3) · 5.000đ/người" in texts()
    st["selected"] = [1]
    assert "☑️ Tất cả" in texts() and "✔️ Xác nhận (1) · 15.000đ/người" in texts()
    st["kind"], st["selected"] = "charge", [0, 2]
    assert "✔️ Xác nhận (2) · tổng 30.000đ" in texts()


def test_charge_one_screen():
    """/charge: nobody ticked at first; pick people (or 👥 Whole team), then the button says how."""
    from app.interfaces.bot.team import picker_keyboard
    st = {"kind": "charge", "options": [{"id": i, "label": n} for i, n in enumerate(["An", "Bình", "Cường"])],
          "selected": [], "data": {"title": "Testing", "amount": 15000, "choose": True}}
    texts = lambda: [b.text for row in picker_keyboard("vi", st).inline_keyboard for b in row]  # noqa: E731
    assert texts()[-4:] == ["👥 Cả đội (3 người)", "👤 Mỗi người đóng 15.000đ", "➗ Chia đều 15.000đ", "Huỷ"]
    st["selected"] = [0, 2]
    assert texts()[-3:-1] == ["👤 Mỗi người đóng 15.000đ · tổng 30.000đ", "➗ Chia đều 15.000đ · 8.000đ/người"]


def test_expired_qr_card():
    from app.interfaces.bot.team import unmatched_card
    p = {"payment_id": 7, "amount": 200000, "content": "TFABCD1234", "reason": "expired", "at": "18/11 10:00",
         "qr_for": ["Bình"], "qr_made": "17/11 09:00", "qr_amount": 200000, "member_ids": [3]}
    text, markup = unmatched_card("en", p)
    assert "the QR had expired" in text and "QR for: <b>Bình</b>" in text
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["u:q:7", "u:a:7", "u:d:7"]
    assert "Bình" in markup.inline_keyboard[0][0].text
    _, plain = unmatched_card("en", {**p, "reason": "no_code", "member_ids": None})
    assert [b.callback_data for row in plain.inline_keyboard for b in row] == ["u:a:7", "u:d:7"]


def test_ops_problems_and_report():
    from datetime import timedelta, datetime, timezone
    from app.interfaces.bot import ops
    team = {"team_id": 1, "name": "FC Test", "bot_id": 5, "bot_username": "fc_bot", "has_bank": True,
            "unmatched": 0, "unmatched_amount": 0, "unmatched_oldest_days": None, "unmatched_stale": False,
            "last_transfer": "18/11/2026 10:00", "last_transfer_days": 0, "qr_made_7d": 4, "qr_paid_7d": 3,
            "qr_open": 1, "sepay_quiet": False}
    fine = {"db": True, "teams": [team], "disk": {"pct": 41, "used_gb": 16, "total_gb": 39},
            "mem": {"used_gb": 1.4, "total_gb": 3.8},
            "backup": {"name": "teamfund-x.sql.gz", "age": timedelta(hours=5), "size_kb": 24,
                       "at": datetime(2026, 11, 18, 12, 5, tzinfo=timezone.utc)},
            "bots": [{"name": "@fc_bot", "ok": True, "pending": 0, "error": None, "error_at": None}],
            "errors": [], "uptime": timedelta(hours=2)}
    assert ops.problems("en", fine) == []
    text = ops.report("en", fine)
    assert "All good" in text and "41%" in text and "19:05 18/11" in text and "<b>FC Test</b>" in text

    bad = {**fine, "db": False, "disk": {**fine["disk"], "pct": 85},
           "backup": {**fine["backup"], "age": timedelta(hours=30)},
           "bots": [{"name": "@fc_bot", "ok": False, "pending": 40, "error": "Connection refused", "error_at": None}],
           "errors": [(0, "bots: update failed")],
           "teams": [{**team, "unmatched": 2, "unmatched_amount": 300000, "unmatched_oldest_days": 5,
                      "unmatched_stale": True, "sepay_quiet": True}]}
    issues = ops.problems("en", bad)
    assert len(issues) == 7, issues
    joined = "\n".join(issues)
    for bit in ("database isn't answering", "85%", "30 h old", "Connection refused", "update failed",
                "300,000đ", "Check the webhook"):
        assert bit in joined, bit
    assert ops.problems("en", {**fine, "backup": {"missing": True}}) == ["No backup files in ~/backups."]
    assert ops.problems("en", {**fine, "backup": None}) == []  # not configured: not checked


def test_error_log_counts_errors_only():
    import logging
    from app.interfaces.bot.ops import ErrorLog
    h = ErrorLog()
    log = logging.getLogger("ops-test")
    log.addHandler(h)
    log.warning("just a warning")
    log.error("boom\nsecond line")
    log.removeHandler(h)
    assert [m for _, m in h.since(60)] == ["ops-test: boom"]


def test_daily_check_messages_admins_only_when_needed(monkeypatch):
    import asyncio
    from datetime import timedelta
    from app.interfaces.bot import ops, registry
    sent = []

    class Bot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    async def lang_for(_id, fallback):
        return "en"
    fine = {"db": True, "teams": [], "disk": {"pct": 40, "used_gb": 1, "total_gb": 2}, "mem": None,
            "backup": None, "bots": [], "errors": [], "uptime": timedelta(hours=1)}
    state = {"r": fine}

    async def collect(_actor):
        return state["r"]
    monkeypatch.setattr(ops, "collect", collect)
    monkeypatch.setattr(ops, "lang_for", lang_for)
    monkeypatch.setattr(ops.settings, "master_admin_ids", [1, 2])
    monkeypatch.setitem(registry.master, "bot", Bot())
    asyncio.run(ops.daily_check())
    assert sent == []  # all well: silence
    state["r"] = {**fine, "disk": {"pct": 91, "used_gb": 1, "total_gb": 2}}
    asyncio.run(ops.daily_check())
    assert [c for c, _ in sent] == [1, 2] and "Daily check: 1 to look at" in sent[0][1] and "91%" in sent[0][1]
