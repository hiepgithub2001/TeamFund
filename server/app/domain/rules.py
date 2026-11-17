"""Pure business rules. No I/O, no framework: easy to test and to read."""
import calendar
import hashlib
import math
import re
import secrets
import unicodedata

from .entities import MANAGER, OWNER, FeePlan, Member, Team

# ---- roles ------------------------------------------------------------------------------------

def can_manage(role: str | None) -> bool:
    return role in (OWNER, MANAGER)


def is_owner(role: str | None) -> bool:
    return role == OWNER


# ---- fees and bills ---------------------------------------------------------------------------

MAX_FEE = 100_000_000
MAX_AMOUNT = 1_000_000_000


def effective_fee(team: Team, member: Member) -> int:
    return member.fee_override if member.fee_override is not None else (team.fee or 0)


CYCLES = (1, 3, 6, 12)  # months per fee bill: monthly, quarterly, half-yearly, yearly


def member_fee(team: Team, member: Member, plan: FeePlan | None) -> tuple[int, int]:
    """(amount, months) a member is billed: their plan, else their own monthly fee, else the team's."""
    if plan is not None:
        return plan.amount, plan.months
    return effective_fee(team, member), 1


def add_months(period: str, n: int) -> str:
    y, m = map(int, period.split("-"))
    y, m = divmod(y * 12 + m - 1 + n, 12)
    return f"{y:04d}-{m + 1:02d}"


def months_between(start: str, end: str) -> int:
    (y1, m1), (y2, m2) = map(int, start.split("-")), map(int, end.split("-"))
    return (y2 - y1) * 12 + m2 - m1


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


# ---- reminders --------------------------------------------------------------------------------

REMIND_EVERY_DAYS_AFTER_DUE = 3


def due_day_in_month(due_day: int, year: int, month: int) -> int:
    return min(due_day, calendar.monthrange(year, month)[1])


def is_last_day_of_month(year: int, month: int, day: int) -> bool:
    return day == calendar.monthrange(year, month)[1]


def should_remind(day: int, due: int) -> bool:
    """3 days before the due day, on it, then every 3 days while someone still owes."""
    if day in (due - 3, due):
        return True
    return day > due and (day - due) % REMIND_EVERY_DAYS_AFTER_DUE == 0


# ---- backdating ------------------------------------------------------------------------------------

MAX_BACKDATE_DAYS = 3 * 366


# ---- validation -------------------------------------------------------------------------------

LANGS = ("en", "vi")
MEMBER_FIELD_LIMITS = {"name": 100, "nickname": 64, "shirt_number": 8, "phone": 32, "note": 255}
MEMBER_STATUSES = ("active", "paused", "left")


def valid_bank(bank_name: str, bank_account: str, bank_holder: str) -> bool:
    return (2 <= len(bank_name) <= 64 and re.fullmatch(r"[0-9A-Za-z]{4,32}", bank_account) is not None
            and 2 <= len(bank_holder) <= 128)
