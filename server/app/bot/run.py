"""Bridge from async bot handlers to the synchronous domain layer."""
import asyncio

from ..db import SessionLocal


async def call(fn, *args, **kwargs):
    """Run a domain function `fn(db, *args, **kwargs)` in a worker thread with its own session."""
    def work():
        with SessionLocal() as db:
            return fn(db, *args, **kwargs)
    return await asyncio.to_thread(work)
