"""Shared fixtures: in-memory SQLite async DB with synthetic data.

Never touches a real database (local or remote).
"""
import os

# Tests never read the real .env: required settings get dummy values before
# bot.config is imported (env vars take precedence over .env).
for _key, _value in {
    "BOT_TOKEN": "123456:TEST",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "REDIS_URL": "redis://localhost:6379/0",
    "EXCHANGE_API_KEY": "test",
}.items():
    os.environ.setdefault(_key, _value)

import datetime
import enum

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# NOTE: we deliberately do NOT import bot.db.models here for the test schema?
# No — we DO want the real models so aggregates under test match production.
from bot.db.models import Base, Category, Item, Receipt


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch):
    """No test may reach a real LLM API: default to Gemini without a key
    (get_provider() raises LLMUnavailableError) and drop the cached provider
    around every test. Tests that exercise a provider configure it explicitly."""
    from bot.config import settings
    from bot.services import llm

    monkeypatch.setattr(settings, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    llm.reset_provider()
    yield
    llm.reset_provider()


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def make_receipt(session, **kwargs) -> tuple[Receipt, list[int]]:
    """Create a receipt (+items). Returns (receipt, item_ids) so callers never
    need to touch r.items (lazy-loading it under asyncio raises MissingGreenlet
    after flush; use crud.get_receipt_by_id for an eager reload instead)."""
    items = kwargs.pop("items", None)
    r = Receipt(
        user_id=kwargs.pop("user_id", 1),
        store=kwargs.pop("store", "Test Store"),
        date=kwargs.pop("date", datetime.date(2026, 6, 15)),
        currency=kwargs.pop("currency", "PLN"),
        total=kwargs.pop("total", 100.0),
        total_pln=kwargs.pop("total_pln", 100.0),
        personal_total_pln=kwargs.pop("personal_total_pln", None),
        photo_file_id=kwargs.pop("photo_file_id", "file-1"),
        tx_type=kwargs.pop("tx_type", "purchase"),
        category=kwargs.pop("category", None),
    )
    session.add(r)
    await session.flush()
    item_ids: list[int] = []
    if items:
        for it in items:
            obj = Item(
                receipt_id=r.id,
                name=it.get("name", "item"),
                quantity=it.get("quantity", 1),
                unit_price=it.get("total_price", 10.0),
                total_price=it.get("total_price", 10.0),
                category=Category(it.get("category", "other")),
            )
            session.add(obj)
            item_ids.append(obj)
        await session.flush()
        item_ids = [obj.id for obj in item_ids]
    return r, item_ids


_ = (BigInteger, Date, DateTime, Float, ForeignKey, Integer, Numeric, String,
     func, Mapped, mapped_column, enum)  # keep imports quiet if unused
