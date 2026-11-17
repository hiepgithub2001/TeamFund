"""Gateway web server: Telegram webhooks for every bot, and events from the core.

Run with: python -m app.main"""
import asyncio
import hmac
import logging

from aiogram.types import Update
from aiohttp import web

from . import core as core_client
from . import registry
from .commands import set_master_commands
from .config import cfg, make_bot
from .core import core
from .events import handle_event
from .master import master_dp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("gateway")

state: dict = {"master": None, "master_poller": None}
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def _feed(dp, bot, data: dict) -> None:
    try:
        await dp.feed_update(bot, Update.model_validate(data, context={"bot": bot}))
    except Exception:
        log.exception("update failed")


async def health(_: web.Request):
    return web.json_response({"ok": True})


async def tg_master(request: web.Request):
    bot = state["master"]
    if bot is None:
        return web.Response(status=503)
    if not _same(request.headers.get(SECRET_HEADER, ""), cfg.master_secret):
        return web.Response(status=401)
    await _feed(master_dp, bot, await request.json())
    return web.json_response({"ok": True})


async def tg_team(request: web.Request):
    try:
        entry = registry.by_bot.get(int(request.match_info["bot_id"]))
    except ValueError:
        entry = None
    if entry is None:
        return web.Response(status=404)
    if not _same(request.headers.get(SECRET_HEADER, ""), entry.secret):
        return web.Response(status=401)
    await _feed(registry.team_dp, entry.bot, await request.json())
    return web.json_response({"ok": True})


async def internal_events(request: web.Request):
    if not _same(request.headers.get("X-Internal-Key", ""), cfg.internal_key):
        return web.Response(status=401)
    try:
        await handle_event(await request.json())
        return web.json_response({"ok": True})
    except Exception as e:
        log.error("event failed: %s", e)
        return web.json_response({"ok": False}, status=503)


async def wait_for_core() -> list[dict]:
    for i in range(10**9):
        try:
            return await core("/internal/bots")
        except Exception as e:
            if i % 10 == 0:
                log.info("waiting for core… (%s)", e)
            await asyncio.sleep(2)


async def boot() -> None:
    if cfg.mode == "webhook" and not cfg.public_url.startswith("https://"):
        raise RuntimeError("PUBLIC_URL must be an https:// URL in webhook mode (or set TELEGRAM_MODE=polling)")
    for team in await wait_for_core():
        try:
            await registry.register_team(team)
        except Exception as e:
            log.error("could not start bot for team %s (@%s): %s", team["team_id"], team["bot_username"], e)
    if not cfg.master_token:
        log.warning("MASTER_BOT_TOKEN is not set; the master bot is off")
        return
    bot = make_bot(cfg.master_token)
    me = await bot.get_me()
    state["master_poller"] = await registry.connect(bot, "/tg/master", cfg.master_secret, master_dp, me.username)
    try:
        await set_master_commands(bot, cfg.admin_ids)
    except Exception as e:
        log.warning("master commands: %s", e)
    state["master"] = bot
    log.info("master bot ready: @%s (%s)", me.username, cfg.mode)


async def on_startup(app: web.Application):
    async def run():
        try:
            await boot()
        except Exception:
            log.exception("boot failed")
            raise SystemExit(1)
    app["boot"] = asyncio.create_task(run())


async def on_cleanup(app: web.Application):
    if state["master_poller"]:
        state["master_poller"].cancel()
    if state["master"]:
        await state["master"].session.close()
    await registry.close_all()
    await core_client.close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/tg/master", tg_master)
    app.router.add_post("/tg/{bot_id}", tg_team)
    app.router.add_post("/internal/events", internal_events)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=cfg.port, print=None)
