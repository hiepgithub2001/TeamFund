"""Use cases run by the clock: open the month, remind, report, clean up."""
import logging
from datetime import datetime, timedelta

from ..domain.entities import JobRun
from ..domain.rules import add_months, period_label, should_remind
from . import billing
from .ports import Deps

log = logging.getLogger("jobs")


class JobUseCases:
    def __init__(self, d: Deps):
        self.d = d

    def _ran(self, uow, key: str) -> bool:
        return uow.job_runs.exists(key)

    def _mark(self, uow, key: str) -> None:
        uow.job_runs.add(JobRun(key=key, created_at=self.d.clock.now()))
        uow.commit()

    def open_month(self) -> None:
        """Create this month's bills and keep late fees current. Each package's new bills are announced
        once in the team's group on that package's announcement day (packages sharing a day share a post)."""
        period, today = self.d.clock.current_period(), self.d.clock.local_now().date()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    billing.ensure_bills(uow, self.d, team, period)
                    to = uow.followers.active_ids(team.id)
                    if team.last_billed_period == period or not to:
                        continue  # announced the old way already this month, or nobody to tell
                    keys, due = [], []
                    for g in billing.opened_groups(uow, self.d, team, period):
                        key = f"announce:{team.id}:{g['plan_id'] or 0}:{g['months']}:{g['amount']}:{g['period']}"
                        if today >= g["notify"] and not self._ran(uow, key):
                            keys.append(key)
                            due.append({k: v for k, v in g.items() if k not in ("notify", "due_on", "period")})
                    if due and self.d.notifier.deliver({
                            "type": "bills_created", "team_id": team.id, "period_label": period_label(period),
                            "fee": team.fee, "fees": due, "to": to}):
                        for key in keys:
                            uow.job_runs.add(JobRun(key=key, created_at=self.d.clock.now()))
                        uow.commit()
                except Exception:
                    uow.rollback()
                    log.exception("open_month failed for team %s", team.id)

    def remind(self) -> None:
        """One reminder a day at most, when any package is 3 days from its due date, on it, or every 3 days
        after it (and announced already). It lists everyone who still owes."""
        now = self.d.clock.local_now()
        today, period = now.date(), self.d.clock.current_period()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    key = f"remind:{team.id}:{now:%Y-%m-%d}"
                    to = uow.followers.active_ids(team.id)
                    if not to or self._ran(uow, key):
                        continue
                    billing.ensure_bills(uow, self.d, team)
                    # who still owes a fee bill (with its late fee); a charge alone isn't the fee
                    owing = billing.unpaid_by_member(uow, team)
                    fee_keys = {(b["period"], b["months"]) for o in owing for b in o["bills"] if b["kind"] == "monthly"}
                    hit = None
                    for p in (period, add_months(period, -1)):
                        for g in billing.opened_groups(uow, self.d, team, p):
                            due = g["due_on"]
                            # only a package somebody still owes for: a paid-up quarter doesn't nag
                            if (g["period"], g["months"]) in fee_keys and should_remind(today, g["notify"], due):
                                hit = hit or (g, due)
                    if not hit:
                        continue
                    unpaid = []
                    for o in owing:
                        fee = [b for b in o["bills"] if b["kind"] == "monthly" or (b["kind"] == "late" and not b.get("of"))]
                        if any(b["kind"] == "monthly" for b in fee):
                            unpaid.append({"name": o["name"], "total": sum(b["amount"] for b in fee)})
                    if not unpaid:
                        continue
                    g, due = hit
                    if self.d.notifier.deliver({
                            "type": "reminder", "team_id": team.id,
                            "period_label": period_label(g["period"], g["months"]),
                            "due": due.strftime("%d/%m"), "late_fee": g["late_fee"], "overdue": today > due,
                            "unpaid": unpaid, "to": to}):
                        self._mark(uow, key)
                except Exception:
                    uow.rollback()
                    log.exception("remind failed for team %s", team.id)

    def remind_charges(self) -> None:
        """Charges with a deadline (/charge, splits): one message a day at most, when a charge is 3 days from
        its deadline, on it, or every 3 days after it, listing who still owes it (with its fine)."""
        now = self.d.clock.local_now()
        today = now.date()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    key = f"remind_charge:{team.id}:{now:%Y-%m-%d}"
                    to = uow.followers.active_ids(team.id)
                    if not to or self._ran(uow, key):
                        continue
                    fines = {lb.monthly_key: lb.amount for lb in uow.bills.late_of_team(team.id) if lb.status == "unpaid"}
                    groups: dict = {}
                    for b, m in uow.bills.unpaid_with_members(team.id):
                        if b.kind not in ("charge", "split") or not b.due_on or b.match_id or m.status != "active":
                            continue
                        made = self.d.clock.fmt(b.created_at)
                        made_on = datetime.strptime(made, "%d/%m/%Y %H:%M").date() if made else today
                        if not should_remind(today, made_on, b.due_on):
                            continue
                        g = groups.setdefault((b.title, b.kind, b.created_at, b.due_on), {
                            "title": b.title, "due": b.due_on.strftime("%d/%m"), "overdue": today > b.due_on,
                            "late_fee": team.charge_late_fee or 0, "unpaid": []})
                        g["unpaid"].append({"name": m.name, "total": b.amount + fines.get(f"late:{b.id}", 0)})
                    if not groups:
                        continue
                    charges = [g for k, g in sorted(groups.items(), key=lambda kv: (kv[0][3], kv[0][0]))]
                    for g in charges:
                        g["unpaid"].sort(key=lambda u: u["name"].casefold())
                    if self.d.notifier.deliver({"type": "charge_reminder", "team_id": team.id, "charges": charges,
                                                "to": to}):
                        self._mark(uow, key)
                except Exception:
                    uow.rollback()
                    log.exception("remind_charges failed for team %s", team.id)

    def late_fees(self) -> None:
        """Once a day, just after midnight: add the new day to every overdue fee bill's late fee."""
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    billing.update_late_fees(uow, self.d, team)
                except Exception:
                    uow.rollback()
                    log.exception("late_fees failed for team %s", team.id)

    def matches(self) -> None:
        """Every hour: fill the repeating calendars 8 weeks ahead; from 09:00 tell managers about a match in
        7 days with no opponent yet; from 18:00 remind followers of tomorrow's matches. Once a day each."""
        from .matches import MatchUseCases, fill
        mu, now = MatchUseCases(self.d), self.d.clock.local_now()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    if fill(uow, team, now.date(), self.d.clock.now()):
                        uow.commit()
                    due = mu.due_reminders(team, uow)
                    key = f"match_open:{team.id}:{now:%Y-%m-%d}"
                    if now.hour >= 9 and due["open"] and not self._ran(uow, key):
                        admins = [a.tg_id for a in uow.admins.of_team(team.id)]
                        if self.d.notifier.deliver({"type": "match_no_opponent", "team_id": team.id,
                                                    "matches": due["open"], "to": admins}):
                            self._mark(uow, key)
                    key = f"match_eve:{team.id}:{now:%Y-%m-%d}"
                    to = uow.followers.active_ids(team.id)
                    if now.hour >= 18 and due["tomorrow"] and to and not self._ran(uow, key):
                        if self.d.notifier.deliver({"type": "match_tomorrow", "team_id": team.id,
                                                    "matches": due["tomorrow"], "to": to}):
                            self._mark(uow, key)
                except Exception:
                    uow.rollback()
                    log.exception("matches failed for team %s", team.id)

    def clean_up(self) -> None:
        """Expire payment intents after 24 hours and manager requests after 7 days."""
        now = self.d.clock.now()
        with self.d.uow() as uow:
            uow.intents.expire_pending_before(now)
            uow.manager_requests.expire_created_before(now - timedelta(days=7))
            uow.commit()
