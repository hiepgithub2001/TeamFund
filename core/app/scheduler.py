"""Background jobs: monthly bills, group reminders, month-end report, cleanup.

Run with: python -m app.scheduler"""
import logging
from datetime import timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select, update

from .db import Base, SessionLocal, engine
from .models import JobLog, ManagerRequest, PaymentIntent, Team, utcnow
from .services import billing
from .services.events import emit
from .services.timeutil import (TZ, current_period, due_date_in_month, is_last_day_of_month, local_now,
                                period_label)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")

REMIND_EVERY_DAYS_AFTER_DUE = 3


def _teams(db):
    return db.scalars(select(Team).where(Team.active.is_(True))).all()


def _once(db, key: str) -> bool:
    return db.get(JobLog, key) is None


def _mark(db, key: str):
    db.add(JobLog(key=key))
    db.commit()


def job_billing():
    """Open the month: create bills and announce them once in the group."""
    period = current_period()
    with SessionLocal() as db:
        for team in _teams(db):
            try:
                billing.ensure_bills(db, team, period)
                if team.last_billed_period == period or team.fee <= 0:
                    continue
                ok = True
                if team.group_chat_id:
                    ok = emit({"type": "bills_created", "team_id": team.id, "period_label": period_label(period),
                               "fee": team.fee, "due_day": team.due_day, "group_chat_id": team.group_chat_id})
                if ok:
                    team.last_billed_period = period
                    db.commit()
            except Exception:
                db.rollback()
                log.exception("billing failed for team %s", team.id)


def should_remind(day: int, due: int) -> bool:
    if day in (due - 3, due):
        return True
    return day > due and (day - due) % REMIND_EVERY_DAYS_AFTER_DUE == 0


def job_reminders():
    now = local_now()
    with SessionLocal() as db:
        for team in _teams(db):
            try:
                if not team.group_chat_id or team.fee <= 0:
                    continue
                due = due_date_in_month(team.due_day, now)
                key = f"remind:{team.id}:{now:%Y-%m-%d}"
                if not should_remind(now.day, due) or not _once(db, key):
                    continue
                billing.ensure_bills(db, team)
                unpaid = [{"name": g["name"], "total": g["total"]} for g in billing.unpaid_by_member(db, team)]
                if not unpaid:
                    continue
                if emit({"type": "reminder", "team_id": team.id, "period_label": period_label(current_period()),
                         "due_day": due, "overdue": now.day > due, "unpaid": unpaid,
                         "group_chat_id": team.group_chat_id}):
                    _mark(db, key)
            except Exception:
                db.rollback()
                log.exception("reminder failed for team %s", team.id)


def job_month_report():
    now = local_now()
    if not is_last_day_of_month(now):
        return
    period = current_period()
    with SessionLocal() as db:
        for team in _teams(db):
            try:
                key = f"report:{team.id}:{period}"
                if not team.group_chat_id or not _once(db, key):
                    continue
                status = billing.team_status(db, team)
                totals = billing.month_totals(db, team, period)
                event = {"type": "month_report", "team_id": team.id, "period_label": period_label(period),
                         **totals, "balance": billing.fund(db, team)["balance"], "unpaid": status["unpaid"],
                         "paid_count": len(status["paid"]), "group_chat_id": team.group_chat_id}
                if emit(event):
                    _mark(db, key)
            except Exception:
                db.rollback()
                log.exception("report failed for team %s", team.id)


def job_cleanup():
    with SessionLocal() as db:
        now = utcnow()
        db.execute(update(PaymentIntent).where(PaymentIntent.status == "pending", PaymentIntent.expires_at < now)
                   .values(status="expired"))
        db.execute(update(ManagerRequest).where(ManagerRequest.status == "pending",
                                                ManagerRequest.created_at < now - timedelta(days=7))
                   .values(status="expired"))
        db.commit()


def main():
    Base.metadata.create_all(engine)
    sched = BlockingScheduler(timezone=TZ)
    sched.add_job(job_billing, "cron", minute=5, id="billing")
    sched.add_job(job_reminders, "cron", hour=9, minute=0, id="reminders")
    sched.add_job(job_month_report, "cron", hour=20, minute=0, id="report")
    sched.add_job(job_cleanup, "cron", minute=30, id="cleanup")
    log.info("scheduler started (%s)", TZ)
    job_billing()
    sched.start()


if __name__ == "__main__":
    main()
