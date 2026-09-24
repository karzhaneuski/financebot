"""Nothing secret may reach the logs (stdout in production)."""
import logging

import httpx
import pytest

import bot.main  # noqa: F401  -- applies the production logging setup
from bot.services import currency

SECRET = "sekret-exchange-key-123"


class _FakeRedis:
    async def get(self, key):
        return None

    async def setex(self, *a, **k):
        return True


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(currency.settings, "EXCHANGE_API_KEY", SECRET)

    def install(handler):
        real_client = httpx.AsyncClient

        def client(*a, **k):
            k["transport"] = httpx.MockTransport(handler)
            return real_client(*a, **k)
        monkeypatch.setattr(currency.httpx, "AsyncClient", client)
    return install


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(403, json={"result": "error"}),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom", request=request)),
])
async def test_fx_failure_never_logs_api_key(provider, handler, caplog):
    provider(handler)
    caplog.set_level(logging.DEBUG)
    rate = await currency.get_rate("EUR", "PLN", _FakeRedis())
    assert rate == 1.0
    assert caplog.records, "the failure should still be logged"
    assert SECRET not in caplog.text


async def test_fx_provider_exception_is_scrubbed(provider):
    provider(lambda request: httpx.Response(500))
    with pytest.raises(currency.FxProviderError) as exc:
        await currency._fetch_rate_from_provider("EUR")
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_httpx_request_urls_not_logged_at_info():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
