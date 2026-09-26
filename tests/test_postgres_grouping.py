"""PostgreSQL rejects `SELECT <expr> ... GROUP BY <expr>` unless both are
textually identical. markers.canonical_sql() is used in both places, so it
must not render bind parameters (SQLite accepts them, so the rest of the
suite can't catch this)."""
import re

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from bot import markers
from bot.db.models import Item, Receipt


def _pg_sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.asyncpg.dialect(), compile_kwargs={"render_postcompile": True}))


def test_canonical_sql_renders_identically_in_select_and_group_by():
    key = markers.canonical_sql(Receipt.store)
    sql = _pg_sql(select(key.label("store"), func.count()).group_by(key))
    select_expr = re.search(r"SELECT (CASE .*? END) AS store", sql, re.S).group(1)
    group_expr = re.search(r"GROUP BY (CASE .*? END)", sql, re.S).group(1)
    assert select_expr == group_expr
    assert "$" not in select_expr and "%(" not in select_expr
    assert "'Снятие наличных'" in select_expr and f"'{markers.CASH_WITHDRAWAL}'" in select_expr


def test_item_name_grouping_is_postgres_safe():
    key = markers.canonical_sql(Item.name)
    sql = _pg_sql(select(key.label("name")).group_by(key))
    select_expr = re.search(r"SELECT (CASE .*? END)", sql, re.S).group(1)
    assert select_expr == re.search(r"GROUP BY (CASE .*? END)", sql, re.S).group(1)
    assert "$" not in select_expr and "%(" not in select_expr
