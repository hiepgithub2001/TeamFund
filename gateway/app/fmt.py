import html
import re


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def money(n, lang: str = "vi") -> str:
    s = f"{int(n or 0):,}"
    return (s.replace(",", ".") if lang == "vi" else s) + "đ"


def parse_amount(raw) -> int | None:
    """'200000', '200.000', '200,000đ', '200k', '1.5tr', '2m' -> integer VND, or None."""
    if not raw:
        return None
    s = re.sub(r"\s+", "", str(raw).strip().lower())
    s = re.sub(r"đ|vnd$", "", s)
    m = re.search(r"(k|m|tr|trieu)$", s)
    if m:
        mult = 1_000 if m.group(1) == "k" else 1_000_000
        try:
            n = float(s[: -len(m.group(1))].replace(",", "."))
        except ValueError:
            return None
        return int(n * mult + 0.5) if n > 0 else None
    s = re.sub(r"[.,]", "", s)
    if not re.fullmatch(r"[0-9]+", s):
        return None
    n = int(s)
    return n if n > 0 else None


def amount_and_title(text) -> tuple[int | None, str | None]:
    """'500000 Pitch rental' -> (500000, 'Pitch rental')."""
    m = re.match(r"^(\S+)\s+(.+)$", (text or "").strip(), re.S)
    if not m:
        return None, None
    return parse_amount(m.group(1)), m.group(2).strip()


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)
