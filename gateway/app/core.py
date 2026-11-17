"""Client for the core API."""
import httpx

from .config import cfg


class CoreError(Exception):
    def __init__(self, status: int, detail):
        if isinstance(detail, dict) and detail.get("code"):
            code = detail["code"]
        else:
            code = {404: "not_found", 422: "invalid"}.get(status, "generic")
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=f"{cfg.core_url}/api", timeout=30,
                                    headers={"X-Internal-Key": cfg.internal_key})
    return _client


async def core(path: str, method: str = "GET", body=None, actor: int | None = None, raw: bool = False):
    """Call the core API. `actor` is the Telegram user the action is done for."""
    headers = {"X-Actor-Id": str(actor)} if actor else {}
    r = await _http().request(method, path, json=body, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = None
        raise CoreError(r.status_code, detail)
    return r.content if raw else r.json()


async def close():
    if _client is not None:
        await _client.aclose()
