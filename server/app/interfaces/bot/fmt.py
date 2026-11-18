import html
import re
from datetime import date


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(n, lang: str = "vi") -> str:
    s = f"{int(n or 0):,}"
    return (s.replace(",", ".") if lang == "vi" else s) + "đ"


_UNITS = {"k": 1_000, "nghin": 1_000, "nghìn": 1_000, "ngan": 1_000, "ngàn": 1_000,
          "m": 1_000_000, "tr": 1_000_000, "trieu": 1_000_000, "triệu": 1_000_000}
_UNIT_RE = "|".join(sorted(map(re.escape, _UNITS), key=len, reverse=True))


def parse_amount(raw) -> int | None:
    """Integer VND, or None. Accepts (see /money): 450000, 450.000, 450,000đ, 450k, 450 nghìn,
    1tr, 1m, 1M, 1 triệu, 1.5tr, 1,5tr, 1.5M, 1tr5 (= 1.5tr), 1tr250 (= 1.25tr)."""
    if not raw:
        return None
    s = re.sub(r"\s+", "", str(raw).strip().lower())
    s = re.sub(r"(đồng|dong|vnđ|vnd|đ)$", "", s)
    m = re.fullmatch(rf"(\d+(?:[.,]\d+)?)({_UNIT_RE})", s)
    if m:
        n = float(m.group(1).replace(",", "."))
        return int(n * _UNITS[m.group(2)] + 0.5) or None
    m = re.fullmatch(rf"(\d+)({_UNIT_RE})(\d{{1,3}})", s)  # 1tr5, 2tr250, 1m2: the digits after are a fraction
    if m and _UNITS[m.group(2)] == 1_000_000:
        return int(float(f"{m.group(1)}.{m.group(3)}") * 1_000_000 + 0.5) or None
    s = re.sub(r"[.,]", "", s)
    if not re.fullmatch(r"[0-9]+", s):
        return None
    return int(s) or None


def amount_and_title(text) -> tuple[int | None, str | None]:
    """'500000 Pitch rental' -> (500000, 'Pitch rental')."""
    m = re.match(r"^(\S+)\s+(.+)$", (text or "").strip(), re.S)
    if not m:
        return None, None
    return parse_amount(m.group(1)), m.group(2).strip()


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# 15/10/2026, 15-10-2026, 15/10/26, 15-10-26, or 15/10, 15-10 (this year); one separator throughout
_DATE_RE = re.compile(r"^(.*\S)\s+(\d{1,2})([/-])(\d{1,2})(?:\3(\d{4}|\d{2}))?$", re.S)
# 10/2026, 10-2026: the 1st of that month (a 4-digit year, so it can't be confused with a day and month)
_MONTH_RE = re.compile(r"^(.*\S)\s+(\d{1,2})[/-](\d{4})$", re.S)


def split_date(text: str, today: date) -> tuple[str, date | None, bool]:
    """'Pitch 15/10' -> ('Pitch', date(this year, 10, 15), True). See /money for the forms.
    The last value is False when the text ends in something date-like that isn't a real day (31/02)."""
    m = _DATE_RE.match((text or "").strip())
    if not m:
        mm = _MONTH_RE.match((text or "").strip())
        if not mm:
            return (text or "").strip(), None, True
        try:
            return mm.group(1).strip(), date(int(mm.group(3)), int(mm.group(2)), 1), True
        except ValueError:
            return mm.group(1).strip(), None, False
    title, d, mo, y = m.group(1).strip(), int(m.group(2)), int(m.group(4)), m.group(5)
    year = (2000 + int(y) if len(y) == 2 else int(y)) if y else today.year
    try:
        return title, date(year, mo, d), True
    except ValueError:
        return title, None, False


_PERIOD_RE = r"(\d{1,2})[/-](\d{2}|\d{4})"


def _period(mm: str, yy: str) -> str | None:
    m, y = int(mm), int(yy) + (2000 if len(yy) == 2 else 0)
    return f"{y:04d}-{m:02d}" if 1 <= m <= 12 and 2000 <= y <= 2099 else None


def parse_window(text: str, default_start: str) -> tuple[str, str | None] | None:
    """The months a plan applies: "11/2026" (from, for good), "11/2026 - 06/2027", "- 06/2027" (from the
    default start), "11/2026 -". Months as mm/yyyy or mm/yy. None when it doesn't read."""
    m = re.fullmatch(rf"\s*(?:{_PERIOD_RE})?\s*(?:(-|–|đến|den|to)\s*(?:{_PERIOD_RE})?)?\s*", (text or "").lower())
    if not m or not (m.group(1) or m.group(4)):
        return None
    start = _period(m.group(1), m.group(2)) if m.group(1) else default_start
    until = _period(m.group(4), m.group(5)) if m.group(4) else None
    if start is None or (m.group(4) and until is None) or (until is not None and until < start):
        return None
    return start, until
