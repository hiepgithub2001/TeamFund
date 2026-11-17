"""Use cases run by the clock: open the month, remind, report, clean up."""
import logging
from datetime import timedelta

from ..domain.entities import JobRun
from ..domain.rules import due_day_in_month, is_last_day_of_month, period_label, should_remind
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
        """Create this month's bills and announce them once in each team's group."""
        period = self.d.clock.current_period()
        with self.d.uow() as uow:
            for team in uow.teams.active():
                try:
                    billing.ensure_bills(uow, self.d, team, period)
                    if team.last_billed_period == period or team.fee <= 0:
                        continue
                    ok = True
                    if team.group_chat_id:
                        ok = self.d.notifier.deliver({
                            "type": "bills_created", "team_id": team.id, "period_label": period_label(period),
                            "fee": team.fee, "due_day": team.due_day, "group_chat_id": team.group_chat_id})
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
                    if not team.group_chat_id or team.fee <= 0:
                        continue
                    due = due_day_in_month(team.due_day, now.year, now.month)
                    key = f"remind:{team.id}:{now:%Y-%m-%d}"
                    if not should_remind(now.day, due) or self._ran(uow, key):
                        continue
                    billing.ensure_bills(uow, self.d, team)
                    unpaid = [{"name": g["name"], "total": g["total"]} for g in billing.unpaid_by_member(uow, team)]
                    if not unpaid:
                        continue
                    if self.d.notifier.deliver({
                            "type": "reminder", "team_id": team.id,
                            "period_label": period_label(self.d.clock.current_period()), "due_day": due,
                            "overdue": now.day > due, "unpaid": unpaid, "group_chat_id": team.group_chat_id}):
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
