"""The SePay user API adapter: headers SePay's Cloudflare needs, and how HTTP errors are classified."""
import io
import json
from urllib.error import HTTPError

import pytest

from app.application.ports import SepayError
from app.infrastructure import sepay as mod


class _Res(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake(monkeypatch, status=200, body=None):
    seen = {}

    def urlopen(req, timeout):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        raw = json.dumps(body or {}).encode()
        if status >= 400:
            raise HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return _Res(raw)

    monkeypatch.setattr(mod, "urlopen", urlopen)
    return seen


def test_sends_a_real_user_agent(monkeypatch):
    seen = _fake(monkeypatch, body={"data": [], "meta": {"pagination": {"has_more": False}}})
    mod.SepayUserApi().bank_accounts("T" * 40)
    # Cloudflare in front of SePay blocks "Python-urllib/x.y" with error 1010
    assert seen["headers"]["user-agent"] == mod.USER_AGENT and "urllib" not in mod.USER_AGENT.lower()
    assert seen["headers"]["authorization"] == "Bearer " + "T" * 40


@pytest.mark.parametrize("status,body,kind", [
    (401, {"status": "error", "error_code": "unauthorized"}, "unauthorized"),
    (403, {"status": "error", "error_code": "forbidden"}, "unauthorized"),
    (403, {"title": "Error 1010: Access denied", "error_code": 1010}, "unavailable"),  # Cloudflare, not the token
    (404, {"status": "error", "error_code": "not_found"}, "not_found"),
    (422, {"status": "error", "error_code": "validation_error"}, "invalid"),
    (429, {"status": "error", "error_code": "rate_limited"}, "unavailable"),
])
def test_error_kinds(monkeypatch, status, body, kind):
    _fake(monkeypatch, status, body)
    with pytest.raises(SepayError) as e:
        mod.SepayUserApi().webhooks("T" * 40)
    assert e.value.kind == kind
