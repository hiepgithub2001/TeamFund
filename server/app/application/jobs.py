"""Use cases run by the clock: open the month, remind, report, clean up."""
import logging
from datetime import timedelta

from ..domain.entities import JobRun
from ..domain.rules import add_months, is_last_day_of_month, period_label, should_remind
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
                    for g in billing.opened_groups(uow, team, period):
                        key = f"announce:{team.id}:{g['plan_id'] or 0}:{g['months']}:{g['amount']}:{period}"
                        if today >= g["notify"] and not self._ran(uow, key):
                            keys.append(key)
                            due.append({k: v for k, v in g.items() if k not in ("notify", "due_on")})
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
                    hit = None
                    for p in (period, add_months(period, -1)):
                        for g in billing.opened_groups(uow, team, p):
                            due = g["due_on"]
                            if should_remind(today, g["notify"], due):
                                hit = hit or (p, g, due)
                    if not hit:
                        continue
                    unpaid = [{"name": g["name"], "total": g["total"]} for g in billing.unpaid_by_member(uow, team)]
                    if not unpaid:
                        continue
                    p, g, due = hit
                    if self.d.notifier.deliver({
                            "type": "reminder", "team_id": team.id, "period_label": period_label(p),
                            "due": due.strftime("%d/%m"), "late_fee": g["late_fee"], "overdue": today > due,
                            "unpaid": unpaid, "to": to}):
                        self._mark(uow, key)
                except Exception:
                    uow.rollback()
                    log.exception("remind failed for team %s", team.id)

    def month_report(self) -> None:
        now = self.d.clock.local_now()
        if not is_last_day_of_month(now.year, now.month, now.day):
            return
        period = self.d.clock.current_period()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    key = f"report:{team.id}:{period}"
                    to = uow.followers.active_ids(team.id)
                    if not to or self._ran(uow, key):
                        continue
                    status = billing.team_status(uow, self.d, team)
                    event = {"type": "month_report", "team_id": team.id, "period_label": period_label(period),
                             **billing.month_totals(uow, self.d, team, period),
                             "balance": billing.fund(uow, team)["balance"], "unpaid": status["unpaid"],
                             "paid_count": len(status["paid"]), "to": to}
                    if self.d.notifier.deliver(event):
                        self._mark(uow, key)
                except Exception:
                    uow.rollback()
                    log.exception("month_report failed for team %s", team.id)

    def clean_up(self) -> None:
        """Expire payment intents after 24 hours and manager requests after 7 days."""
        now = self.d.clock.now()
        with self.d.uow() as uow:
            uow.intents.expire_pending_before(now)
            uow.manager_requests.expire_created_before(now - timedelta(days=7))
            uow.commit()
