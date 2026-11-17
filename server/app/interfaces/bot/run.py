"""Bridge from async controllers to the synchronous use cases."""
import asyncio


async def call(fn, *args, **kwargs):
    """Run a use case in a worker thread so the event loop keeps serving other updates."""
    return await asyncio.to_thread(fn, *args, **kwargs)
