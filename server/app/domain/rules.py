"""Pure business rules. No I/O, no framework: easy to test and to read."""
import calendar
import hashlib
import math
import re
import secrets

from .entities import MANAGER, OWNER, Member, Team

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


def split_share(total: int, people: int) -> int:
    """Each person's share of a shared cost, rounded up to the next 1,000đ."""
    return int(math.ceil(total / people / 1000) * 1000)


def period_label(period: str) -> str:
    year, month = period.split("-")
    return f"{month}/{year}"


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


# ---- validation -------------------------------------------------------------------------------

LANGS = ("en", "vi")
MEMBER_FIELD_LIMITS = {"name": 100, "nickname": 64, "shirt_number": 8, "phone": 32, "note": 255}
MEMBER_STATUSES = ("active", "paused", "left")


def valid_bank(bank_name: str, bank_account: str, bank_holder: str) -> bool:
    return (2 <= len(bank_name) <= 64 and re.fullmatch(r"[0-9A-Za-z]{4,32}", bank_account) is not None
            and 2 <= len(bank_holder) <= 128)
