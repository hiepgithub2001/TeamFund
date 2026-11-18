"""Use cases: the match calendar. Matches one by one or pasted many at a time, repeating calendars that fill
matches ahead, cancelling with a reason. Money only when a manager asks: after a match, what the team paid
the opponent (an expense) or the opponent paid the team (income), dated on the match day."""
from datetime import date, timedelta

from ..domain.entities import Expense, Match, MatchSchedule, Payment, Team
from ..domain.errors import DomainError, invalid
from ..domain.rules import MATCH_FILL_DAYS, MAX_MATCHES_AT_ONCE, fold, schedule_dates
from . import billing
from .access import get_team, manager_team
from .people import audit, display_name
from .ports import Deps, UnitOfWork

TEXT_LIMITS = {"place": 100, "opponent": 100, "note": 255, "reason": 255}
EVERY = ("weekly", "biweekly", "monthly")
MAX_SCHEDULES = 5


def _time(v) -> str | None:
    """"19:00" or None; anything else is refused."""
    if v is None:
        return None
    if not (isinstance(v, str) and len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit()
            and int(v[:2]) < 24 and int(v[3:]) < 60):
        raise invalid()
    return v


def _text(field: str, v) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise invalid()
    v = " ".join(v.split())
    if len(v) > TEXT_LIMITS[field]:
        raise invalid()
    return v or None


def _day(v) -> date:
    if not isinstance(v, date):
        raise invalid()
    return v


def match_dict(m: Match, today: date | None = None) -> dict:
    return {"id": m.id, "day": m.day.isoformat(), "weekday": m.day.weekday(), "at": m.at, "place": m.place,
            "opponent": m.opponent, "note": m.note, "status": m.status, "reason": m.reason,
            "schedule_id": m.schedule_id, "past": bool(today and m.day < today),
            "settleable": bool(today and m.day <= today), "paid": m.paid, "received": m.received}


def schedule_dict(s: MatchSchedule) -> dict:
    return {"id": s.id, "every": s.every, "weekday": s.weekday, "nth": s.nth, "mday": s.mday,
            "anchor": s.anchor.isoformat() if s.anchor else None, "at": s.at, "place": s.place}


def fill(uow: UnitOfWork, team: Team, today: date, now) -> int:
    """Add the matches each active calendar has in the next 8 weeks and doesn't have yet. A date whose match
    was deleted or cancelled stays as it is, and a day that already has a match gets no second one.
    Idempotent; the caller commits."""
    added = 0
    end = today + timedelta(days=MATCH_FILL_DAYS)
    busy = {m.day for m in uow.matches.between(team.id, today, end)}
    for s in uow.schedules.of_team(team.id):
        have = {m.day for m in uow.matches.of_schedule(s.id)} | busy
        for d in schedule_dates(s, today, end):
            if d not in have:
                busy.add(d)
                uow.matches.add(Match(team_id=team.id, day=d, at=s.at, place=s.place, schedule_id=s.id,
                                      created_at=now))
                added += 1
    return added



def upcoming(uow: UnitOfWork, team: Team, today: date, limit: int) -> list[Match]:
    """The next `limit` matches still on, with the cancelled ones between them (they don't count)."""
    out, on = [], 0
    for m in uow.matches.from_day(team.id, today, limit * 4 + 20):
        if on >= limit:
            break
        out.append(m)
        on += m.status == "scheduled"
    return out


class MatchUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _today(self) -> date:
        return self.d.clock.local_now().date()

    def _team(self, uow, team_id: int) -> Team:
        return get_team(uow, team_id)

    def _match(self, uow, team: Team, match_id) -> Match:
        m = uow.matches.get(match_id) if isinstance(match_id, int) else None
        if not m or m.team_id != team.id or m.status == "deleted":
            raise DomainError(404, "not_found")
        return m

    def _fill(self, uow, team: Team) -> None:
        if fill(uow, team, self._today(), self.d.clock.now()):
            uow.commit()

    # ---- reading (anyone who opened the bot) ----

    def show(self, team_id: int, actor: int | None, limit: int = 5) -> dict:
        """The next `limit` matches still on (cancelled ones shown between them), and the calendars."""
        limit = max(1, min(int(limit), 200))
        with self.d.uow() as uow:
            team = self._team(uow, team_id)
            self._fill(uow, team)
            today = self._today()
            items = upcoming(uow, team, today, limit)
            more = len(upcoming(uow, team, today, limit + 1)) > len(items)
            return {"matches": [match_dict(m, today) for m in items], "more": more, "limit": limit,
                    "schedules": [schedule_dict(s) for s in uow.schedules.of_team(team.id)],
                    "can_edit": bool(actor and uow.admins.role_of(team.id, actor))}

    def past(self, team_id: int, actor: int | None, limit: int = 10) -> list[dict]:
        with self.d.uow() as uow:
            team = self._team(uow, team_id)
            today = self._today()
            return [match_dict(m, today) for m in uow.matches.before(team.id, today, max(1, min(int(limit), 100)))]

    def get(self, team_id: int, actor: int, match_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            out = match_dict(m, self._today())
            key = fold(m.opponent)
            c = next((c for c in uow.contacts.of_team(team.id) if key and fold(c.name) == key), None)
            out["contact_id"] = c.id if c else None
            out["contact_phone"] = c.phone if c else None
            return out

    def suggest_days(self, team_id: int, actor: int, n: int = 4) -> list[str]:
        """Days to offer when adding a match: the calendars' next free days, else the next weekend days."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            today = self._today()
            taken = {m.day for m in uow.matches.between(team.id, today, today + timedelta(days=120))}
            days = sorted({d for s in uow.schedules.of_team(team.id)
                           for d in schedule_dates(s, today, today + timedelta(days=120)) if d not in taken})
            if not days:
                days = [today + timedelta(days=i) for i in range(60) if (today + timedelta(days=i)).weekday() >= 5]
            return [d.isoformat() for d in days[:n]]

    def defaults(self, team_id: int, actor: int) -> dict:
        """The usual time and pitch: the first calendar's."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            s = next(iter(uow.schedules.of_team(team.id)), None)
            return {"at": s.at, "place": s.place} if s else {"at": None, "place": None}

    # ---- changing (managers) ----

    def add(self, team_id: int, actor: int, items: list) -> list[dict]:
        """Add matches: each {day, at?, place?, opponent?, note?}."""
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_MATCHES_AT_ONCE:
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            made = []
            for it in items:
                if not isinstance(it, dict):
                    raise invalid()
                m = Match(team_id=team.id, day=_day(it.get("day")), at=_time(it.get("at")),
                          place=_text("place", it.get("place")), opponent=_text("opponent", it.get("opponent")),
                          note=_text("note", it.get("note")), created_at=self.d.clock.now())
                uow.matches.add(m)
                made.append(m)
            uow.flush()
            first = made[0]
            audit(uow, self.d.clock, team.id, actor, "match.add",
                  target=(f"{first.day:%d/%m} {first.opponent or ''}".strip() if len(made) == 1 else f"{len(made)}"),
                  after={"count": len(made)})
            uow.commit()
            return [match_dict(m, self._today()) for m in made]

    def update(self, team_id: int, actor: int, match_id: int, changes: dict) -> dict:
        """Change some of day, at, place, opponent, note (None clears all but the day)."""
        if not isinstance(changes, dict) or not changes or not set(changes) <= {"day", "at", "place", "opponent", "note"}:
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            clean = {k: (_day(v) if k == "day" else _time(v) if k == "at" else _text(k, v)) for k, v in changes.items()}
            before = {k: getattr(m, k) for k in clean}
            for k, v in clean.items():
                setattr(m, k, v)
            audit(uow, self.d.clock, team.id, actor, "match.update", target=f"{m.day:%d/%m} {m.opponent or ''}".strip(),
                  before={k: (v.isoformat() if isinstance(v, date) else v) for k, v in before.items()},
                  after={k: (v.isoformat() if isinstance(v, date) else v) for k, v in clean.items()})
            uow.commit()
            return match_dict(m, self._today())

    def delete(self, team_id: int, actor: int, match_id: int) -> dict:
        """Gone from the calendar (a calendar won't bring the date back)."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            m.status = "deleted"
            audit(uow, self.d.clock, team.id, actor, "match.delete", target=f"{m.day:%d/%m} {m.opponent or ''}".strip())
            uow.commit()
            return match_dict(m, self._today())

    def cancel(self, team_id: int, actor: int, match_id: int, reason: str | None = None) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            if m.status == "cancelled":
                raise DomainError(409, "already_resolved")
            m.status, m.reason = "cancelled", _text("reason", reason)
            audit(uow, self.d.clock, team.id, actor, "match.cancel", target=f"{m.day:%d/%m} {m.opponent or ''}".strip(),
                  after={"reason": m.reason})
            uow.commit()
            return match_dict(m, self._today())

    def restore(self, team_id: int, actor: int, match_id: int) -> dict:
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            if m.status != "cancelled":
                raise DomainError(409, "already_resolved")
            m.status, m.reason = "scheduled", None
            audit(uow, self.d.clock, team.id, actor, "match.restore", target=f"{m.day:%d/%m} {m.opponent or ''}".strip())
            uow.commit()
            return match_dict(m, self._today())

    def cancel_on(self, team_id: int, actor: int, day: date, reason: str | None = None) -> dict:
        """Cancel the one match still on that day (for `/matches cancel 30/11 rain`)."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            ids = [m.id for m in uow.matches.between(team.id, _day(day), day, statuses=("scheduled",))]
        if len(ids) != 1:
            raise DomainError(404 if not ids else 409, "match_not_found" if not ids else "match_ambiguous")
        return self.cancel(team_id, actor, ids[0], reason)

    def add_schedule(self, team_id: int, actor: int, every: str, weekday: int | None = None, nth: int | None = None,
                     mday: int | None = None, anchor: date | None = None, at: str | None = None,
                     place: str | None = None) -> dict:
        """A repeating calendar; its matches for the next 8 weeks appear at once."""
        ok = every in EVERY and (
            (every == "weekly" and weekday in range(7)) or
            (every == "biweekly" and isinstance(anchor, date)) or
            (every == "monthly" and ((isinstance(mday, int) and 1 <= mday <= 31) or
                                     (weekday in range(7) and nth in (1, 2, 3, 4, -1)))))
        if not ok:
            raise invalid()
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            if len(uow.schedules.of_team(team.id)) >= MAX_SCHEDULES:
                raise DomainError(400, "too_many_schedules")
            s = MatchSchedule(team_id=team.id, every=every, weekday=weekday if every != "biweekly" else anchor.weekday(),
                              nth=nth if every == "monthly" and not mday else None,
                              mday=mday if every == "monthly" else None,
                              anchor=anchor if every == "biweekly" else None, at=_time(at),
                              place=_text("place", place), created_at=self.d.clock.now())
            uow.schedules.add(s)
            uow.flush()
            fill(uow, team, self._today(), self.d.clock.now())
            audit(uow, self.d.clock, team.id, actor, "schedule.add", target=every, after=schedule_dict(s))
            uow.commit()
            return schedule_dict(s)

    def stop_schedule(self, team_id: int, actor: int, schedule_id: int) -> dict:
        """No more dates from it. Its future dates without an opponent or note go; the others stay."""
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            s = uow.schedules.get(schedule_id) if isinstance(schedule_id, int) else None
            if not s or s.team_id != team.id or not s.active:
                raise DomainError(404, "not_found")
            s.active = False
            today, dropped = self._today(), 0
            for m in uow.matches.of_schedule(s.id):
                if m.day >= today and m.status == "scheduled" and not m.opponent and not m.note:
                    uow.matches.remove(m)
                    dropped += 1
            audit(uow, self.d.clock, team.id, actor, "schedule.stop", target=s.every, after={"dropped": dropped})
            uow.commit()
            return {**schedule_dict(s), "dropped": dropped}

    def record_money(self, team_id: int, actor: int, match_id: int, kind: str, amount: int) -> dict:
        """After a match: "paid" (the team paid the opponent: an expense) or "received" (the opponent paid the
        team: income), counted in the fund on the match day and added up on the match. Undo takes both back."""
        from .money import MoneyUseCases
        if kind not in ("paid", "received"):
            raise invalid()
        mu = MoneyUseCases(self.d)
        with self.d.uow() as uow:
            team = manager_team(uow, team_id, actor)
            m = self._match(uow, team, match_id)
            if m.day > self._today():
                raise DomainError(400, "match_not_played")
            who = m.opponent or m.day.strftime("%d/%m")
            title = mu._check(amount, f"Trận vs {who}" if kind == "paid" else f"Đối trả: {who}", 200)
            at = mu._when(m.day)
            if kind == "paid":
                uow.expenses.add(Expense(team_id=team.id, amount=amount, title=title, created_by=actor, created_at=at))
                m.paid = (m.paid or 0) + amount
            else:
                uow.payments.add(Payment(team_id=team.id, source="other", amount=amount, status="matched",
                                         recorded_by=actor, note=title, created_at=at))
                m.received = (m.received or 0) + amount
            audit(uow, self.d.clock, team.id, actor, "match.money", target=title, after={"kind": kind, "amount": amount})
            uow.commit()
            return {"match": match_dict(m, self._today()), "kind": kind, "amount": amount, "title": title,
                    "by": display_name(uow, actor), "at": self.d.clock.fmt(at), "backdated": m.day != self._today(),
                    "balance": billing.fund(uow, team)["balance"]}

    # ---- the clock ----

    def due_reminders(self, team: Team, uow: UnitOfWork) -> dict:
        """For the job: tomorrow's matches (followers) and matches in 7 days without an opponent (managers)."""
        today = self._today()
        tomorrow = uow.matches.between(team.id, today + timedelta(days=1), today + timedelta(days=1), ("scheduled",))
        week = [m for m in uow.matches.between(team.id, today + timedelta(days=7), today + timedelta(days=7),
                                               ("scheduled",)) if not m.opponent]
        return {"tomorrow": [match_dict(m, today) for m in tomorrow], "open": [match_dict(m, today) for m in week]}
