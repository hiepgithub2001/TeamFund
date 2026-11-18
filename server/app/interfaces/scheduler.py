"""Scheduler controller: runs the clock-driven use cases at fixed times, inside the server process."""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("scheduler")


def start(jobs, timezone: str, daily_check=None) -> AsyncIOScheduler:
    """`jobs` is the JobUseCases; each job runs in a worker thread. `daily_check`: an async function run at 08:00
    (the system admins' health check, which talks to Telegram itself)."""
    def threaded(fn):
        async def run():
            await asyncio.to_thread(fn)
        return run

    sched = AsyncIOScheduler(timezone=timezone)
    sched.add_job(threaded(jobs.open_month), "cron", minute=5, id="open_month")
    sched.add_job(threaded(jobs.remind), "cron", hour=9, minute=0, id="remind")
    sched.add_job(threaded(jobs.clean_up), "cron", minute=30, id="clean_up")
    sched.add_job(threaded(jobs.late_fees), "cron", hour=0, minute=5, id="late_fees")  # once a day
    sched.add_job(threaded(jobs.matches), "cron", minute=10, id="matches")
    if daily_check:
        sched.add_job(daily_check, "cron", hour=8, minute=0, id="ops_check")
    sched.add_job(threaded(jobs.open_month), id="open_month_at_start")  # once, right away
    sched.add_job(threaded(jobs.matches), id="matches_at_start")
    sched.add_job(threaded(jobs.late_fees), id="late_fees_at_start")  # a restart never skips a day
    sched.start()
    log.info("scheduler started (%s)", timezone)
    return sched
