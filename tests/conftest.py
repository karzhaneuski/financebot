"""Shared fixtures: in-memory SQLite async DB with synthetic data.

Never touches a real database (local or remote).
"""
import datetime
import enum

import pytest_asyncio
from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

# NOTE: we deliberately do NOT import bot.db.models here for the test schema?
# No — we DO want the real models so aggregates under test match production.
from bot.db.models import Base, Category, Item, Receipt


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
