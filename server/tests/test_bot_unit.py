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


def test_webhook_errors_from_a_restart_are_not_problems(monkeypatch):
    """A 502 while the app restarts (a deploy) is retried by Telegram: shown, not flagged. One while it runs is."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.interfaces.bot import ops, registry
    now = datetime.now(timezone.utc)
    infos = {}

    class Bot:
        def __init__(self, name):
            self.name = name

        async def get_webhook_info(self):
            return infos[self.name]
    monkeypatch.setattr(ops.settings, "telegram_mode", "webhook")
    monkeypatch.setattr(ops, "STARTED", now.timestamp() - 3600)  # up for an hour
    monkeypatch.setitem(registry.master, "bot", None)
    monkeypatch.setattr(registry, "by_bot", {1: SimpleNamespace(username="deploy_bot", bot=Bot("a")),
                                             2: SimpleNamespace(username="broken_bot", bot=Bot("b"))})
    infos["a"] = SimpleNamespace(url="https://x/tg/1", pending_update_count=0, last_error_message="502 Bad Gateway",
                                 last_error_date=datetime.fromtimestamp(now.timestamp() - 3700, timezone.utc))
    infos["b"] = SimpleNamespace(url="https://x/tg/2", pending_update_count=3, last_error_message="502 Bad Gateway",
                                 last_error_date=datetime.fromtimestamp(now.timestamp() - 600, timezone.utc))
    rows = {r["name"]: r for r in asyncio.run(ops.webhooks())}
    assert rows["@deploy_bot"]["ok"] and rows["@deploy_bot"]["error"] is None and rows["@deploy_bot"]["old_error_at"]
    assert not rows["@broken_bot"]["ok"] and rows["@broken_bot"]["error"] == "502 Bad Gateway"


def test_tip_command_parsing():
    from aiogram.types import Update
    from app.interfaces.bot.tips import command_of

    def upd(text):
        return Update.model_validate({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "chat": {"id": 5, "type": "private"}, "text": text}})
    assert command_of(upd("/history")) == ("history", "")
    assert command_of(upd("/history@fc37it_bot 10/2026")) == ("history", "10/2026")
    assert command_of(upd("/matches\n7/12 19h FC Moreh")) == ("matches", "7/12 19h FC Moreh")
    assert command_of(upd("hello")) == (None, "")


def test_every_tip_exists_in_both_languages():
    import json
    from pathlib import Path
    d = Path(__file__).resolve().parents[1] / "app/interfaces/bot/locales"
    en, vi = (json.loads((d / f).read_text()) for f in ("en.json", "vi.json"))
    tips = {k for k in en if k.startswith(("tip.", "mtip."))}
    assert len(tips) >= 25 and tips == {k for k in vi if k.startswith(("tip.", "mtip."))}


def test_a_bot_that_fails_at_startup_is_retried(monkeypatch):
    import asyncio
    from app.interfaces.bot import ops, registry
    tries = []

    async def register(t):
        tries.append(1)
        if len(tries) < 3:
            raise OSError("Connection reset by peer")
        registry.by_team[int(t["team_id"])] = object()
        registry.down.pop(int(t["team_id"]), None)

    async def no_teams(*a, **k):
        return [{"team_id": 77, "bot_username": "wq_bot"}]
    monkeypatch.setattr(registry, "register_team", register)
    monkeypatch.setattr(registry, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(registry, "call", no_teams)
    from types import SimpleNamespace
    monkeypatch.setattr(registry, "S", SimpleNamespace(registration=SimpleNamespace(active_bots=None)))
    monkeypatch.setattr(registry.settings, "master_token", "")
    monkeypatch.setattr(registry.settings, "telegram_mode", "polling")

    async def run():
        await registry.start()
        assert 77 in registry.down
        assert any(b["name"] == "@wq_bot" and not b["ok"] for b in await ops.webhooks()), "/ops shows it"
        for _ in range(50):
            if 77 in registry.by_team:
                break
            await asyncio.sleep(0.01)
    try:
        asyncio.run(run())
        assert len(tries) == 3 and 77 in registry.by_team and 77 not in registry.down
    finally:
        registry.by_team.pop(77, None)
        registry.down.pop(77, None)



def test_ops_warns_when_the_sepay_plan_is_nearly_used():
    from datetime import timedelta
    from app.interfaces.bot import ops
    r = {"db": True, "teams": [], "disk": {"pct": 10, "used_gb": 1, "total_gb": 10}, "mem": None, "backup": None,
         "bots": [], "errors": [], "uptime": timedelta(hours=1),
         "sepay": [{"teams": ["FC37it", "KCU Prime"], "quota": 50, "used": 41, "pct": 82, "near": True,
                    "since": "01/11", "resets": "01/12", "error": None}]}
    assert ops.problems("en", r) == ["SePay plan of FC37it, KCU Prime nearly used up: 41 / 50 (82%), restarts 01/12. "
                                     "Upgrade on my.sepay.vn, or payments stop confirming by themselves."]
    assert "⚠️ FC37it, KCU Prime: 41 / 50 transactions (82%) since 01/11, restarts 01/12" in ops.report("en", r)
    r["sepay"][0].update(used=10, pct=20, near=False)
    assert ops.problems("en", r) == []
