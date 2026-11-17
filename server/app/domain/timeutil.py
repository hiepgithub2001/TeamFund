import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..config import settings

TZ = ZoneInfo(settings.timezone)


def local_now() -> datetime:
    return datetime.now(TZ)


def current_period() -> str:
    return local_now().strftime("%Y-%m")


def period_label(period: str) -> str:
    year, month = period.split("-")
    return f"{month}/{year}"


def to_local_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def month_bounds_utc(period: str) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of a local calendar month."""
    year, month = (int(x) for x in period.split("-"))
    start = datetime(year, month, 1, tzinfo=TZ)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=TZ)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(end)


def due_date_in_month(due_day: int, now: datetime) -> int:
    return min(due_day, calendar.monthrange(now.year, now.month)[1])


def is_last_day_of_month(now: datetime) -> bool:
    return now.day == calendar.monthrange(now.year, now.month)[1]
