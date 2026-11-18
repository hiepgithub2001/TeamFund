"""The team pay page (PWA) for people without Telegram: /p/<token>/ and its JSON API.

Everything lives under the team's secret path, so the service worker's scope is that one team and Caddy only
needs to pass /p/* through. The page can read who owes and create a QR code; managing stays in Telegram."""
import io
import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from ...domain.errors import DomainError
from ..bot.run import call
from ..deps import S

router = APIRouter()
log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent.parent / "web"
ASSETS = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml",
          "icon-192.png": "image/png", "icon-512.png": "image/png"}

# The token in the URL is the only key to the page: never send it to other sites (the QR image is loaded from
# qr.sepay.vn), never let search engines index it, and only run our own code.
SECURITY = {
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' https://qr.sepay.vn data:; script-src 'self'; "
                                "style-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
}
NO_STORE = {"Cache-Control": "no-store"}


class RateLimit:
    """At most `limit` hits per key in `window` seconds (one process, so memory is enough)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window, self.hits = limit, window, defaultdict(deque)

    def allow(self, key: str) -> bool:
        now, q = time.monotonic(), self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10_000:  # forget idle clients
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                del self.hits[k]
        return True


pay_limit = RateLimit(10, 60)      # QR codes per client per minute
read_limit = RateLimit(120, 60)    # page loads and status polls per client per minute


def client_ip(request: Request) -> str:
    # Caddy is the only way in and sets X-Forwarded-For; its first entry is the visitor
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _json(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={**SECURITY, **NO_STORE})


def _error(e: DomainError) -> JSONResponse:
    return _json({"error": e.code, **e.extra}, e.status)


def _too_many() -> JSONResponse:
    return _json({"error": "rate_limited"}, 429)


# ---- page and assets ------------------------------------------------------------------------------

@router.get("/p/{token}")
def page_redirect(token: str):
    return RedirectResponse(f"/p/{token}/", status_code=308, headers=SECURITY)


@router.get("/p/{token}/")
def page(token: str):
    # The same shell for every team; app.js reads the token from the URL and asks the API.
    return FileResponse(STATIC / "index.html", media_type="text/html",
                        headers={**SECURITY, "Cache-Control": "no-cache"})


@router.get("/p/{token}/sw.js")
def service_worker(token: str):
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={**SECURITY, "Cache-Control": "no-cache", "Service-Worker-Allowed": f"/p/{token}/"})


@router.get("/p/{token}/manifest.webmanifest")
async def manifest(token: str, request: Request):
    if not read_limit.allow(client_ip(request)):
        return _too_many()
    try:
        info = await call(S.web.page, token)
        photo, size = await bot_photo(await call(S.web.team_id, token))
    except DomainError as e:
        return _error(e)
    own = [{"src": "bot.jpg", "sizes": f"{size}x{size}", "type": "image/jpeg"},
           {"src": "bot.jpg", "sizes": f"{size}x{size}", "type": "image/jpeg", "purpose": "maskable"}] if photo else []
    body = {"name": f"{info['team']} · Team Fund", "short_name": info["team"][:12], "lang": info["lang"],
            "start_url": "./", "scope": "./", "id": "./", "display": "standalone",
            "background_color": "#f6f7f4", "theme_color": "#1f7a4d",
            "icons": own + [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                      {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/manifest+json",
                    headers={**SECURITY, "Cache-Control": "no-cache"})


# The team bot's Telegram profile photo, as the page's icon. Asked from Telegram at most every few hours.
PHOTO_TTL = 6 * 3600
_photos: dict[int, tuple[float, bytes | None, int]] = {}  # team id -> (fetched at, jpeg or None, size px)


async def bot_photo(team_id: int) -> tuple[bytes | None, int]:
    hit = _photos.get(team_id)
    if hit and time.monotonic() - hit[0] < PHOTO_TTL:
        return hit[1], hit[2]
    from ..bot import registry  # the running bots; imported here so the web app loads without them
    data, size = None, 0
    entry = registry.by_team.get(team_id)
    if entry is not None:
        try:
            photos = await entry.bot.get_user_profile_photos(entry.bot.id, limit=1)
            if photos.photos:
                best = max(photos.photos[0], key=lambda p: p.width)
                buf = io.BytesIO()
                await entry.bot.download(best.file_id, destination=buf)
                data, size = buf.getvalue(), best.width
        except Exception as e:  # no photo is fine: the page keeps its own icon
            log.warning("bot photo for team %s: %s", team_id, e)
            if hit:
                return hit[1], hit[2]
    _photos[team_id] = (time.monotonic(), data, size)
    return data, size


@router.get("/p/{token}/bot.jpg")
async def bot_icon(token: str, request: Request):
    """The team bot's photo; the default icon when it has none."""
    if not read_limit.allow(client_ip(request)):
        return _too_many()
    try:
        data, _ = await bot_photo(await call(S.web.team_id, token))
    except DomainError as e:
        return _error(e)
    if data is None:
        return FileResponse(STATIC / "icon-512.png", media_type="image/png",
                            headers={**SECURITY, "Cache-Control": "public, max-age=3600"})
    return Response(data, media_type="image/jpeg", headers={**SECURITY, "Cache-Control": "public, max-age=3600"})


@router.get("/p/{token}/{asset}")
def asset(token: str, asset: str):
    if asset not in ASSETS:
        return Response(status_code=404, headers=SECURITY)
    return FileResponse(STATIC / asset, media_type=ASSETS[asset],
                        headers={**SECURITY, "Cache-Control": "public, max-age=300"})


# ---- API ------------------------------------------------------------------------------------------

@router.get("/p/{token}/api/state")
async def state(token: str, request: Request):
    if not read_limit.allow(client_ip(request)):
        return _too_many()
    try:
        return _json(await call(S.web.page, token))
    except DomainError as e:
        return _error(e)


@router.get("/p/{token}/api/month/{period}")
async def month(token: str, period: str, request: Request):
    if not read_limit.allow(client_ip(request)):
        return _too_many()
    try:
        return _json(await call(S.web.month, token, period))
    except DomainError as e:
        return _error(e)


@router.post("/p/{token}/api/pay")
async def pay(token: str, request: Request, body: dict = Body(...)):
    if not pay_limit.allow(client_ip(request)):
        return _too_many()
    try:
        return _json(await call(S.web.create_intent, token, body.get("member_ids")))
    except DomainError as e:
        return _error(e)


@router.post("/p/{token}/api/donate")
async def donate(token: str, request: Request, body: dict = Body(...)):
    if not pay_limit.allow(client_ip(request)):
        return _too_many()
    try:
        return _json(await call(S.web.donate, token, body.get("amount"), body.get("name")))
    except DomainError as e:
        return _error(e)


@router.get("/p/{token}/api/pay/{code}")
async def pay_status(token: str, code: str, request: Request):
    if not read_limit.allow(client_ip(request)):
        return _too_many()
    try:
        return _json(await call(S.web.intent_status, token, code))
    except DomainError as e:
        return _error(e)
