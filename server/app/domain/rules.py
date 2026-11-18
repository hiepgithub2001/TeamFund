"""Pure business rules. No I/O, no framework: easy to test and to read."""
import calendar
from datetime import date, timedelta
import hashlib
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass

from .entities import MANAGER, OWNER, FeePlan, Member, Team

# ---- roles ------------------------------------------------------------------------------------

def can_manage(role: str | None) -> bool:
    return role in (OWNER, MANAGER)


def is_owner(role: str | None) -> bool:
    return role == OWNER


# ---- fees and bills ---------------------------------------------------------------------------

MAX_FEE = 100_000_000
MAX_DONATION = 50_000_000  # one donation QR: any multiple of 1.000đ up to this
DONOR_NAME_MAX = 60


def donation_ok(amount) -> bool:
    return isinstance(amount, int) and not isinstance(amount, bool) and 1000 <= amount <= MAX_DONATION \
        and amount % 1000 == 0
MAX_AMOUNT = 1_000_000_000


def effective_fee(team: Team, member: Member) -> int:
    return member.fee_override if member.fee_override is not None else (team.fee or 0)


CYCLES = (1, 3, 6, 12)  # months per fee bill: monthly, quarterly, half-yearly, yearly


def member_fee(team: Team, member: Member, plan: FeePlan | None) -> tuple[int, int]:
    """(amount, months) a member is billed: their plan, else their own monthly fee, else the team's."""
    if plan is not None:
        return plan.amount, plan.months
    if member.fee_override is not None:
        return member.fee_override, 1  # a member's own fee is monthly
    return team.fee or 0, team.fee_months or 1  # the default package, on its own cycle


def add_months(period: str, n: int) -> str:
    y, m = map(int, period.split("-"))
    y, m = divmod(y * 12 + m - 1 + n, 12)
    return f"{y:04d}-{m + 1:02d}"


def months_between(start: str, end: str) -> int:
    (y1, m1), (y2, m2) = map(int, start.split("-")), map(int, end.split("-"))
    return (y2 - y1) * 12 + m2 - m1


def cycle_start(period: str, months: int) -> str:
    """The standard period a month falls in: quarters Jan/Apr/Jul/Oct, half-years Jan/Jul, years Jan."""
    y, m = map(int, period.split("-"))
    return f"{y:04d}-{(m - 1) // months * months + 1:02d}"


def cycle_bill(amount: int, months: int, first: str, until: str | None = None) -> tuple[int, int]:
    """(months, amount) of a fee bill starting in `first`: to the end of its standard period (or `until`,
    the plan's last month, if sooner), priced for the months it covers (600,000đ a quarter from November:
    2 months, 400,000đ), rounded up to 1,000đ."""
    end = add_months(cycle_start(first, months), months)
    if until is not None and add_months(until, 1) < end:
        end = add_months(until, 1)
    k = months_between(first, end)
    return k, amount if k == months else int(math.ceil(amount * k / months / 1000) * 1000)


def period_name(period: str, months: int) -> str:
    """A billing period by its usual name: "11/2026", "2026Q4", "2026H2", "2026"."""
    y, m = period[:4], int(period[5:])
    if months == 3:
        return f"{y}Q{(m - 1) // 3 + 1}"
    if months == 6:
        return f"{y}H{(m - 1) // 6 + 1}"
    if months == 12:
        return y
    return f"{m:02d}/{y}"


def period_end(period: str, months: int) -> str:
    """The last month of the standard period `period` falls in (2026-11, quarterly: 2026-12)."""
    return add_months(cycle_start(period, months), months - 1)


def on_plan(x, period: str) -> bool:
    """Does a plan membership apply in `period` (inside its start/until months)?"""
    return (x.start is None or x.start <= period) and (x.until is None or period <= x.until)


def overlaps(a_start: str | None, a_until: str | None, b_start: str | None, b_until: str | None) -> bool:
    """Do two spans of months share a month? None: open on that side."""
    return (a_start or "0000-00") <= (b_until or "9999-99") and (b_start or "0000-00") <= (a_until or "9999-99")


def is_period(v) -> bool:
    """"2026-11"."""
    return isinstance(v, str) and re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", v) is not None


def covers(bill_period: str, months: int | None, period: str) -> bool:
    """Does a fee bill starting in bill_period for `months` months cover `period`?"""
    return 0 <= months_between(bill_period, period) < (months or 1)


def split_share(total: int, people: int) -> int:
    """Each person's share of a shared cost, rounded up to the next 1,000đ."""
    return int(math.ceil(total / people / 1000) * 1000)


def period_label(period: str, months: int | None = 1) -> str:
    """"11/2026", or "11/2026–01/2027" for a bill that covers several months."""
    year, month = period.split("-")
    if (months or 1) <= 1:
        return f"{month}/{year}"
    return f"{month}/{year}–{period_label(add_months(period, months - 1))}"


# ---- bot usernames ----------------------------------------------------------------------------

def bot_usernames(team_name: str) -> list[str]:
    """Usernames to try for a team's bot, best first: "FC 37 IT" -> fc_37_it_fund_bot, fc_37_it_tf_bot, ...
    Telegram's rules: 5-32 characters, a-z 0-9 _, starts with a letter, ends in "bot"."""
    text = unicodedata.normalize("NFKD", (team_name or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if c.isascii() and not unicodedata.combining(c)).lower()
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text)).strip("_")
    if not slug or not slug[0].isalpha():
        slug = ("team_" + slug).strip("_")
    slug = slug[:20].rstrip("_")
    return [f"{slug}_fund_bot", f"{slug}_tf_bot"] + [f"{slug}_fund{n}_bot" for n in range(2, 6)]


# ---- payment codes ----------------------------------------------------------------------------

CODE_PREFIX = "TF"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I, easy to read off a screen
# Lookahead so overlapping candidates are all tried, e.g. "GIFTF..." before the real code.
_CODE_RE = re.compile(rf"(?=({CODE_PREFIX}[{CODE_ALPHABET}]{{8}}))")


def new_payment_code() -> str:
    return CODE_PREFIX + "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def find_payment_codes(text: str) -> list[str]:
    """Every possible payment code in a bank transfer's content. Banks often add spaces or dashes."""
    normalized = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    return [m.group(1) for m in _CODE_RE.finditer(normalized)]


# Banks whose SePay link only reports transfers whose content starts with this keyword
# (SePay's VietinBank guide: "mọi giao dịch ... phải bắt đầu bằng từ khóa SEVQR").
SEPAY_KEYWORD = "SEVQR"
_KEYWORD_BANKS = {"vietinbank", "icb", "ctg"}


def transfer_content(bank_name: str | None, code: str) -> str:
    """What the payer types as the transfer content: the code, plus SePay's keyword where the bank needs it."""
    bank = re.sub(r"[^a-z]", "", (bank_name or "").lower())
    return f"{SEPAY_KEYWORD} {code}" if bank in _KEYWORD_BANKS else code


def new_public_token() -> str:
    """The secret in a team's pay page URL: unguessable, URL-safe, short enough to share."""
    return secrets.token_urlsafe(12)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---- schedule: announce, due, late -----------------------------------------------------------

MAX_DUE_DAYS = 60
MAX_LATE_FEE = 1_000_000
REMIND_EVERY_DAYS_AFTER_DUE = 3


DEFAULT_NOTIFY_DAY, DEFAULT_DUE_DAYS = 1, 5  # announced on the 1st, due 5 days later


def schedule(team: Team) -> tuple[int, int]:
    """(notify day of the month, days until due) of the default package."""
    if team.notify_day:
        return team.notify_day, team.due_days or 0
    return DEFAULT_NOTIFY_DAY, DEFAULT_DUE_DAYS


@dataclass(frozen=True)
class Terms:
    """A package's deadline and late fee: announced on notify_day, due due_days later, late_fee đ a day."""
    notify_day: int
    due_days: int
    late_fee: int
    late_since: date | None


def terms(team: Team, plan: FeePlan | None = None) -> Terms:
    """The default package's terms (the team's), or a plan's: its own where set, else the default's."""
    nd, dd = schedule(team)
    t = Terms(nd, dd, team.late_fee or 0, team.late_fee_since)
    if plan is None:
        return t
    if plan.notify_day:
        nd, dd = plan.notify_day, plan.due_days or 0
    lf, ls = (plan.late_fee, plan.late_fee_since) if plan.late_fee is not None else (t.late_fee, t.late_since)
    return Terms(nd, dd, lf or 0, ls)


def notify_date(tm: Terms, period: str) -> date:
    y, m = map(int, period.split("-"))
    return date(y, m, min(tm.notify_day, calendar.monthrange(y, m)[1]))


def due_date(tm: Terms, period: str) -> date:
    """The announcement day of the bill's first month plus the deadline (it may fall next month)."""
    return notify_date(tm, period) + timedelta(days=tm.due_days)


def late_days(due: date, today: date, since: date | None) -> int:
    """Whole days a bill is overdue, counted from the due date or from when the late fee was turned on."""
    start = max(due, since) if since else due
    return max(0, (today - start).days)


def should_remind(today: date, notify: date, due: date) -> bool:
    """3 days before the due date, on it, then every 3 days while someone still owes; never before the
    fee is announced."""
    if today < notify:
        return False
    diff = (today - due).days
    if diff in (-3, 0):
        return True
    return diff > 0 and diff % REMIND_EVERY_DAYS_AFTER_DUE == 0


# ---- backdating ------------------------------------------------------------------------------------

MAX_BACKDATE_DAYS = 3 * 366


# ---- validation -------------------------------------------------------------------------------

LANGS = ("en", "vi")
MEMBER_FIELD_LIMITS = {"name": 100, "nickname": 64, "shirt_number": 8, "phone": 32, "note": 255}
MEMBER_STATUSES = ("active", "paused", "left")


def valid_bank(bank_name: str, bank_account: str, bank_holder: str) -> bool:
    return (2 <= len(bank_name) <= 64 and re.fullmatch(r"[0-9A-Za-z]{4,32}", bank_account) is not None
            and 2 <= len(bank_holder) <= 128)


# ---- matches ----------------------------------------------------------------------------------

MATCH_FILL_DAYS = 56  # a repeating calendar fills matches 8 weeks ahead
MAX_MATCHES_AT_ONCE = 60


def fold(text: str | None) -> str:
    """For searching and comparing names: "FC Sóc Sơn" -> "fc soc son"."""
    text = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    """The nth weekday of a month (nth -1: the last one); None if the month has no such day."""
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, d).weekday() == weekday]
    if nth == -1:
        return date(year, month, days[-1])
    return date(year, month, days[nth - 1]) if 1 <= nth <= len(days) else None


def schedule_dates(s, start: date, end: date) -> list[date]:
    """The days a repeating calendar has matches, from start to end (both included)."""
    out: list[date] = []
    if s.every == "weekly" and s.weekday is not None:
        d = start + timedelta(days=(s.weekday - start.weekday()) % 7)
        while d <= end:
            out.append(d)
            d += timedelta(days=7)
    elif s.every == "biweekly" and s.anchor is not None:
        d = s.anchor
        if d < start:
            d += timedelta(days=14 * -(-(start - d).days // 14))
        while d <= end:
            out.append(d)
            d += timedelta(days=14)
    elif s.every == "monthly":
        y, m = start.year, start.month
        while date(y, m, 1) <= end:
            if s.mday:
                d = date(y, m, min(s.mday, calendar.monthrange(y, m)[1]))
            elif s.weekday is not None and s.nth:
                d = nth_weekday(y, m, s.weekday, s.nth)
            else:
                d = None
            if d and start <= d <= end:
                out.append(d)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out
