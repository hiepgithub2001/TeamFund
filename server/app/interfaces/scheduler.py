"""Scheduler controller: runs the clock-driven use cases at fixed times, inside the server process."""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("scheduler")


def start(jobs, timezone: str) -> AsyncIOScheduler:
    """`jobs` is the JobUseCases; each job runs in a worker thread."""
    def threaded(fn):
        async def run():
            await asyncio.to_thread(fn)
        return run

    sched = AsyncIOScheduler(timezone=timezone)
    sched.add_job(threaded(jobs.open_month), "cron", minute=5, id="open_month")
    sched.add_job(threaded(jobs.remind), "cron", hour=9, minute=0, id="remind")
    sched.add_job(threaded(jobs.clean_up), "cron", minute=30, id="clean_up")
    sched.add_job(threaded(jobs.late_fees), "cron", hour=0, minute=5, id="late_fees")  # once a day
    sched.add_job(threaded(jobs.open_month), id="open_month_at_start")  # once, right away
    sched.add_job(threaded(jobs.late_fees), id="late_fees_at_start")  # a restart never skips a day
    sched.start()
    log.info("scheduler started (%s)", timezone)
    return sched
