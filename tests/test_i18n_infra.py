"""Localization plumbing: users/language migration, legacy-name markers and
compiled catalogs."""
import datetime as dt
import io
import importlib.util
from pathlib import Path

import pytest
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bot import markers
from bot.db import crud
from bot.db.models import Base, Budget, Category, ReportSettings, User
from bot.i18n import LOCALES_DIR, SUPPORTED_LANGUAGES, i18n
from tests.conftest import make_receipt

ROOT = Path(__file__).resolve().parents[1]


# ── migration 011 ────────────────────────────────────────────────────────────

def _load_migration():
    path = ROOT / "alembic" / "versions" / "011_add_users_language.py"
    spec = importlib.util.spec_from_file_location("m011", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_migration_011_backfills_existing_users_as_russian():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    pre_tables = [t for t in Base.metadata.sorted_tables if t.name != "users"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=pre_tables))
        await conn.execute(text(
            "INSERT INTO receipts (user_id, currency, total, total_pln) VALUES (1, 'PLN', 10, 10), (1, 'PLN', 5, 5)"
        ))
        await conn.execute(text(
            "INSERT INTO budgets (user_id, category, limit_pln, month, last_notified_pct) "
            "VALUES (2, 'groceries', 100, '2026-06', 0)"
        ))
        await conn.execute(text("INSERT INTO report_settings (user_id, daily_enabled, weekly_enabled, "
                                "monthly_enabled) VALUES (3, 1, 1, 1)"))
        before = [
            (await conn.execute(text(f"SELECT * FROM {t} ORDER BY id"))).all()
            for t in ("receipts", "budgets", "report_settings")
        ]

        migration = _load_migration()

        def _upgrade(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                migration.upgrade()
        await conn.run_sync(_upgrade)

        users = (await conn.execute(text("SELECT user_id, language FROM users ORDER BY user_id"))).all()
        after = [
            (await conn.execute(text(f"SELECT * FROM {t} ORDER BY id"))).all()
            for t in ("receipts", "budgets", "report_settings")
        ]
    await engine.dispose()

    assert users == [(1, "ru"), (2, "ru"), (3, "ru")]
    assert after == before  # existing data untouched


# ── language resolution ──────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("pl", "pl"), ("en-US", "en"), ("ru", "ru"), ("de", "en"), (None, "en"), ("", "en"),
])
async def test_new_user_language_from_telegram(db_session, code, expected):
    assert await crud.get_or_create_user_language(db_session, 500, code) == expected
    assert await crud.get_user_language(db_session, 500) == expected


@pytest.mark.parametrize("seed", ["receipt", "budget", "report_settings"])
async def test_user_with_any_existing_data_gets_russian(db_session, seed):
    if seed == "receipt":
        await make_receipt(db_session, user_id=600)
    elif seed == "budget":
        db_session.add(Budget(user_id=600, category=Category.cafe, limit_pln=10, month="2026-06"))
    else:
        db_session.add(ReportSettings(user_id=600))
    await db_session.flush()
    assert await crud.get_or_create_user_language(db_session, 600, "pl") == "ru"


async def test_stored_language_wins_over_telegram(db_session):
    db_session.add(User(user_id=700, language="pl"))
    await db_session.flush()
    assert await crud.get_or_create_user_language(db_session, 700, "en") == "pl"


# ── markers ──────────────────────────────────────────────────────────────────

async def test_legacy_russian_names_group_with_new_markers(db_session):
    d = dt.date.today()
    await make_receipt(db_session, user_id=800, store="Академик", date=d, total=900, total_pln=900,
                       items=[{"name": "Академик", "total_price": 900, "category": "housing"}])
    await make_receipt(db_session, user_id=800, store=markers.AKADEMIK, date=d, total=900, total_pln=900,
                       items=[{"name": markers.AKADEMIK, "total_price": 900, "category": "housing"}])

    stores = await crud.get_spending_by_store(db_session, 800, 30)
    assert stores == [{"store": markers.AKADEMIK, "total_pln": 1800.0, "visits": 2}]
    items = await crud.get_items_grouped(db_session, 800, 30)
    assert [(i["name"], i["total_pln"]) for i in items] == [(markers.AKADEMIK, 1800.0)]

    for lang, label in (("ru", "Академик"), ("en", "Akademik")):
        with i18n.use_locale(lang):
            assert markers.display_name(stores[0]["store"]) == label
            assert markers.display_name("Академик") == label


def test_display_name_passes_real_names_through():
    assert markers.display_name("Kaufland") == "Kaufland"
    assert markers.display_name(None) is None


# ── catalogs ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_compiled_catalog_matches_po(lang):
    """The committed .mo must be compiled from the committed .po
    (run `pybabel compile -d bot/locales -D messages` after editing)."""
    base = LOCALES_DIR / lang / "LC_MESSAGES" / "messages"
    with open(f"{base}.po", "rb") as fh:
        catalog = read_po(fh)
    buf = io.BytesIO()
    write_mo(buf, catalog)
    assert buf.getvalue() == Path(f"{base}.mo").read_bytes()


def test_ru_catalog_is_complete():
    with open(LOCALES_DIR / "ru" / "LC_MESSAGES" / "messages.po", "rb") as fh:
        catalog = read_po(fh)
    untranslated = [m.id for m in catalog if m.id and (not m.string or "" in (m.string if isinstance(m.string, tuple) else (m.string,)))]
    assert untranslated == []
