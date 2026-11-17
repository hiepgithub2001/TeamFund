"""Notifier port, implemented with the Telegram bots.

Use cases run in worker threads; this hands their events to the event loop where the bots live."""
import asyncio
import logging

from . import notify

log = logging.getLogger("notifier")


class TelegramNotifier:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def publish(self, event: dict) -> None:
        """Send in the background; the caller doesn't wait."""
        if self.loop is None:
            log.warning("notifier not bound; dropped %s", event.get("type"))
            return
        asyncio.run_coroutine_threadsafe(notify.deliver(event), self.loop)

    def deliver(self, event: dict, timeout: float = 60) -> bool:
        """Send and wait. Must be called from a worker thread, never from the event loop itself."""
        if self.loop is None:
            return False
        try:
            return asyncio.run_coroutine_threadsafe(notify.deliver(event), self.loop).result(timeout)
        except Exception as e:
            log.warning("could not deliver %s: %s", event.get("type"), e)
            return False
