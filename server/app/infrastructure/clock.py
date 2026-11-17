"""Clock port: real time in the teams' time zone."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


class SystemClock:
    def __init__(self, tz_name: str = "Asia/Ho_Chi_Minh"):
        self.tz = ZoneInfo(tz_name)

    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def local_now(self) -> datetime:
        return datetime.now(self.tz)

    def current_period(self) -> str:
        return self.local_now().strftime("%Y-%m")

    def month_bounds(self, period: str) -> tuple[datetime, datetime]:
        year, month = (int(x) for x in period.split("-"))
        start = datetime(year, month, 1, tzinfo=self.tz)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=self.tz)
        utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
        return utc(start), utc(end)

    def at_local(self, day: date) -> datetime:
        """Noon of a local day, as naive UTC: the timestamp of something recorded for that day."""
        return datetime.combine(day, time(12), self.tz).astimezone(timezone.utc).replace(tzinfo=None)

    def fmt(self, dt: datetime | None) -> str | None:
        if dt is None:
            return None
        return dt.replace(tzinfo=timezone.utc).astimezone(self.tz).strftime("%d/%m/%Y %H:%M")
