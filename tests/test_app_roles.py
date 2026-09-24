"""Process roles (APP_ROLE): the scheduler must run only in the bot role,
otherwise the api container would send every scheduled report a second time."""
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import bot.main as app_main


def test_scheduler_only_in_bot_and_all_roles():
    assert "scheduler" in app_main.role_components("bot")
    assert "scheduler" not in app_main.role_components("api")
    assert "polling" not in app_main.role_components("api")
    assert app_main.role_components("api") == {"api"}
    # migrations are a one-shot compose service in the split layout
    assert "migrations" not in app_main.role_components("bot")
    assert "migrations" not in app_main.role_components("api")


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        app_main.role_components("worker")


@pytest.fixture
def started(monkeypatch):
    """Patch every side effect of main() and record what was started."""
    calls: list[str] = []

    class _Scheduler:
        def start(self):
            calls.append("scheduler")

        def shutdown(self, wait=False):
            pass

    async def _noop(*a, **k):
        return None

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self):
            calls.append("api")

    class _Dispatcher:
        async def start_polling(self, bot):
            calls.append("polling")

    class _Bot:
        def __init__(self, *a, **k):
            self.session = type("S", (), {"close": _noop})()

    class _Redis:
        aclose = staticmethod(_noop)

    monkeypatch.setattr(app_main, "setup_scheduler", lambda bot, redis: _Scheduler())
    monkeypatch.setattr(app_main.uvicorn, "Server", _Server)
    monkeypatch.setattr(app_main, "build_dispatcher", lambda redis: _Dispatcher())
    monkeypatch.setattr(app_main, "Bot", _Bot)
    monkeypatch.setattr(app_main.aioredis, "from_url", lambda *a, **k: _Redis())
    monkeypatch.setattr(app_main, "_run_migrations", lambda: calls.append("migrations"))
    monkeypatch.setattr(app_main, "_heartbeat", lambda: _noop())
    monkeypatch.setattr(app_main, "engine", type("E", (), {"dispose": staticmethod(_noop)})())
    return calls


async def test_api_role_starts_no_scheduler(started):
    await app_main.main("api")
    assert started == ["api"]


async def test_bot_role_starts_scheduler_and_polling_but_no_api(started):
    await app_main.main("bot")
    assert sorted(started) == ["polling", "scheduler"]


async def test_role_read_from_environment(started, monkeypatch):
    monkeypatch.setenv("APP_ROLE", "api")
    await app_main.main()
    assert started == ["api"]


# ── /healthz ─────────────────────────────────────────────────────────────────

async def _get_healthz():
    from bot.api.app import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/healthz")


async def test_healthz_ok_without_auth(monkeypatch):
    import bot.api.app as api_app
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(api_app, "AsyncSessionLocal", sessionmaker(engine, class_=AsyncSession))
    resp = await _get_healthz()
    await engine.dispose()
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_healthz_reports_db_down(monkeypatch):
    import bot.api.app as api_app

    class _Broken:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(api_app, "AsyncSessionLocal", lambda: _Broken())
    resp = await _get_healthz()
    assert resp.status_code == 503
