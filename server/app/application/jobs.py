"""Use cases run by the clock: open the month, remind, report, clean up."""
import logging
from datetime import timedelta

from ..domain.entities import JobRun
from ..domain.rules import add_months, due_date, is_last_day_of_month, notify_date, period_label, should_remind
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
        """Create this month's bills, keep late fees current, and announce the month once in each team's
        group on its announcement day."""
        period, today = self.d.clock.current_period(), self.d.clock.local_now().date()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    billing.ensure_bills(uow, self.d, team, period)
                    if (team.last_billed_period == period or not billing.has_fees(uow, team)
                            or today < notify_date(team, period)):
                        continue
                    ok = True
                    if team.group_chat_id:
                        ok = self.d.notifier.deliver({
                            "type": "bills_created", "team_id": team.id, "period_label": period_label(period),
                            "fee": team.fee, "fees": billing.fee_groups(uow, team),
                            "due": due_date(team, period).strftime("%d/%m"), "late_fee": team.late_fee or 0,
                            "group_chat_id": team.group_chat_id})
                    if ok:
                        team.last_billed_period = period
                        uow.commit()
                except Exception:
                    uow.rollback()
                    log.exception("open_month failed for team %s", team.id)

    def remind(self) -> None:
        now = self.d.clock.local_now()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    if not team.group_chat_id or not billing.has_fees(uow, team):
                        continue
                    # the month whose fee is open: this one once announced, else last month's
                    today, period = now.date(), self.d.clock.current_period()
                    if today < notify_date(team, period):
                        period = add_months(period, -1)
                    due = due_date(team, period)
                    key = f"remind:{team.id}:{now:%Y-%m-%d}"
                    if not should_remind(today, notify_date(team, period), due) or self._ran(uow, key):
                        continue
                    billing.ensure_bills(uow, self.d, team)
                    unpaid = [{"name": g["name"], "total": g["total"]} for g in billing.unpaid_by_member(uow, team)]
                    if not unpaid:
                        continue
                    if self.d.notifier.deliver({
                            "type": "reminder", "team_id": team.id,
                            "period_label": period_label(period), "due": due.strftime("%d/%m"),
                            "late_fee": team.late_fee or 0, "overdue": today > due, "unpaid": unpaid, "group_chat_id": team.group_chat_id}):
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
                    if not team.group_chat_id or self._ran(uow, key):
                        continue
                    status = billing.team_status(uow, self.d, team)
                    event = {"type": "month_report", "team_id": team.id, "period_label": period_label(period),
                             **billing.month_totals(uow, self.d, team, period),
                             "balance": billing.fund(uow, team)["balance"], "unpaid": status["unpaid"],
                             "paid_count": len(status["paid"]), "group_chat_id": team.group_chat_id}
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
