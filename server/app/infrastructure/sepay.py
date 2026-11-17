"""SePay adapters: the VietQR image URL (PaymentQr port) and the user API (SepayApi port).

The user API is reached with an API token the owner creates in my.sepay.vn (Cấu hình công ty → API Access).
Field names follow https://userapi.sepay.vn/v2 as it answers today (updates are PUT, not PATCH);
writes need an X-Idempotency-Key header."""
import json
import logging
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..application.ports import SepayBankAccount, SepayError, SepayWebhook, SepayWebhookSpec

log = logging.getLogger("sepay.api")


class SepayQr:
    def url(self, bank_name: str, bank_account: str, amount: int, content: str) -> str:
        return ("https://qr.sepay.vn/img?acc={acc}&bank={bank}&amount={amount}&des={des}&template=compact"
                .format(acc=quote(bank_account or ""), bank=quote(bank_name or ""), amount=amount, des=quote(content)))


class SepayUserApi:
    def __init__(self, base_url: str = "https://userapi.sepay.vn/v2", timeout: float = 15):
        self.base, self.timeout = base_url.rstrip("/"), timeout

    # ---- port ------------------------------------------------------------------------------------

    def bank_accounts(self, token: str) -> list[SepayBankAccount]:
        return [SepayBankAccount(xid=str(a["id"]), account_number=str(a.get("account_number") or ""),
                                 bank_short_name=a.get("bank_short_name") or "", bank_code=a.get("bank_code") or "",
                                 holder=a.get("account_holder_name") or "",
                                 active=str(a.get("active")) in ("1", "True", "true"))
                for a in self._pages(token, "/bank-accounts")]

    def webhooks(self, token: str) -> list[SepayWebhook]:
        return [SepayWebhook(id=str(w["id"]), url=w.get("webhook_url") or w.get("url") or "")
                for w in self._pages(token, "/webhooks")]

    def create_webhook(self, token: str, spec: SepayWebhookSpec) -> str:
        data = self._call(token, "POST", "/webhooks", self._body(spec))
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict) or not d.get("id"):
            raise SepayError("invalid", "no webhook id in SePay's answer")
        return str(d["id"])

    def update_webhook(self, token: str, webhook_id: str, spec: SepayWebhookSpec) -> None:
        self._call(token, "PUT", f"/webhooks/{quote(webhook_id)}", self._body(spec))

    def delete_webhook(self, token: str, webhook_id: str) -> None:
        self._call(token, "DELETE", f"/webhooks/{quote(webhook_id)}")

    # ---- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _body(spec: SepayWebhookSpec) -> dict:
        return {
            "name": spec.name,
            "url": spec.url,
            "bank_account_xid": spec.bank_account_xid,
            "event_type": "in",                       # incoming money only
            "auth_type": "apikey",                    # SePay sends "Authorization: Apikey <secret>"
            "secret": spec.api_key,
            "content_type": "json",
            "is_verify_payment": True,
            "skip_if_no_code": False,                 # our TF codes aren't SePay payment codes; we match them
            "only_va": False,
            "retry_conditions": {"non_2xx_status_code": 1},
            "active": True,
        }

    def _pages(self, token: str, path: str) -> list[dict]:
        items, page = [], 1
        while True:
            data = self._call(token, "GET", f"{path}?{urlencode({'page': page, 'limit': 100})}")
            items += data.get("data") or []
            p = (data.get("meta") or {}).get("pagination") or {}
            if not p.get("has_more") and p.get("current_page", 1) >= p.get("last_page", 1):
                return items
            page += 1

    def _call(self, token: str, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-Idempotency-Key"] = str(uuid.uuid4())
        req = Request(self.base + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            text = e.read().decode(errors="replace")[:500]
            log.warning("%s %s -> %s %s", method, path.split("?")[0], e.code, text)
            try:
                msg = json.loads(text).get("message") or text
            except ValueError:
                msg = text
            kind = {401: "unauthorized", 403: "unauthorized", 404: "not_found"}.get(e.code)
            kind = kind or ("invalid" if e.code in (400, 409, 422) else "unavailable")
            raise SepayError(kind, msg) from e
        except (URLError, TimeoutError, ValueError) as e:
            log.warning("%s %s failed: %s", method, path.split("?")[0], e)
            raise SepayError("unavailable", str(e)) from e
