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



_TOKEN = {1: r"(?P<m>\d{1,2})[/-](?P<y>\d{4}|\d{2})",
          3: r"(?P<y>\d{4})\s*[-/ ]?\s*q(?P<n>[1-4])|q(?P<n2>[1-4])\s*[-/ ]?\s*(?P<y2>\d{4})",
          6: r"(?P<y>\d{4})\s*[-/ ]?\s*h(?P<n>[12])|h(?P<n2>[12])\s*[-/ ]?\s*(?P<y2>\d{4})",
          12: r"(?P<y>\d{4})"}
_SEP = r"(?:-|–|đến|den|to)"


def _bounds(m: re.Match, months: int) -> tuple[str, str] | None:
    """A period token -> (first month, last month)."""
    g = m.groupdict()
    y = g.get("y") or g.get("y2")
    y = int(y) + (2000 if len(y) == 2 else 0)
    if months == 1:
        first = int(g["m"])
        if not 1 <= first <= 12:
            return None
    elif months == 12:
        first = 1
    else:
        first = (int(g.get("n") or g.get("n2")) - 1) * months + 1
    if not 2000 <= y <= 2099:
        return None
    start = f"{y:04d}-{first:02d}"
    ey, em = divmod(y * 12 + first - 1 + months - 1, 12)
    return start, f"{ey:04d}-{em + 1:02d}"


def parse_periods(text: str, months: int, default_start: str) -> tuple[str, str | None] | None:
    """Whole periods a plan applies, in the plan's own cycle: "2026Q4 - 2027Q2", "2027Q1" (from, for good),
    "- 2027Q2" (from default_start), "2026H2", "2027", "11/2026 - 03/2027". None when it doesn't read."""
    text = (text or "").strip().lower()
    toks = list(re.finditer(_TOKEN[months], text))
    rest = re.sub(_TOKEN[months], "#", text)
    shape = re.fullmatch(rf"\s*(#)?\s*({_SEP})?\s*(#)?\s*", rest)
    if not toks or len(toks) > 2 or not shape:
        return None
    a, sep, b = shape.groups()
    if len(toks) == 2 and not (a and b):
        return None
    parts = [_bounds(t, months) for t in toks]
    if None in parts:
        return None
    if a and b:
        start, until = parts[0][0], parts[1][1]
    elif a:
        start, until = parts[0][0], None
    elif sep:
        start, until = default_start, parts[0][1]
    else:
        return None
    return (start, until) if until is None or until >= start else None
