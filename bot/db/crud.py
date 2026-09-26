import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot import markers
from bot.db.models import Budget, Category, Item, Receipt, ReportSettings, User


def _get_since(period_key: str) -> date | None:
    days_map = {"day": 1, "week": 7, "month": 30, "year": 365}
    days = days_map.get(period_key)
    if days is None:
        return None
    return datetime.utcnow().date() - timedelta(days=days)


def _personal_pln_col():
    """Personal expense amount: split share when set, else the full total (Feature 3)."""
    return func.coalesce(Receipt.personal_total_pln, Receipt.total_pln)


def _item_pln_col():
    """Item price in PLN. Item prices are stored in the receipt's currency;
    convert with the receipt's own rate (total_pln / total) — the rate baked
    in when it was saved, so category sums agree with receipt totals."""
    return case(
        (Receipt.currency == "PLN", Item.total_price),
        else_=func.coalesce(Item.total_price * Receipt.total_pln / func.nullif(Receipt.total, 0), 0),
    )


def _not_income():
    """Everything except income: salary or refunds are not purchases, so they
    stay out of item / product statistics."""
    return func.coalesce(Receipt.tx_type, "purchase") != "income"


def _store_spending():
    """Receipts that are spending at a store: not income, not cash withdrawals."""
    return func.coalesce(Receipt.tx_type, "purchase").notin_(("income", "cash_withdrawal"))


def _is_product_line():
    """Items that are real receipt lines, for product stats and item export.

    Receipts from a photo, a PDF or /add have no source; whether a photo is
    stored does not matter (PDF receipts have none). Bank screenshots and
    statement imports carry a source: their one item is the whole
    transaction, not a product. Neither is income or a cash withdrawal, nor
    the placeholder item /add creates (a bot-generated marker name)."""
    return (
        Receipt.source.is_(None)
        & _store_spending()
        & Item.name.notin_(markers.GENERATED_NAMES)
    )


def _store_key():
    """Receipt.store with legacy Russian bot-generated names mapped to their
    language-neutral markers, so old and new rows group together."""
    return markers.canonical_sql(Receipt.store)


def _personal_item_filter():
    """Condition matching only items that count toward the user's personal totals.

    items.is_personal is NULL for anything not touched by /split — those
    count as fully personal (pre-split behaviour preserved).
    """
    return func.coalesce(Item.is_personal, True).is_(True)


async def create_receipt(
    session: AsyncSession,
    user_id: int,
    data: dict,
    photo_file_id: str | None = None,
    total_pln: float | None = None,
    source: str | None = None,
    tx_type: str | None = None,
    category: "Category | None" = None,
) -> Receipt:
    receipt = Receipt(
        user_id=user_id,
        store=data.get("store"),
        date=datetime.strptime(data["date"], "%Y-%m-%d").date() if data.get("date") else None,
        currency=data.get("currency", "PLN"),
        total=data.get("total", 0),
        total_pln=total_pln if total_pln is not None else data.get("total", 0),
        photo_file_id=photo_file_id,
        source=source,
        tx_type=tx_type,
        category=category,
    )
    session.add(receipt)
    await session.flush()

    for item_data in data.get("items", []):
        item = Item(
            receipt_id=receipt.id,
            name=item_data["name"],
            normalized_name=item_data.get("normalized_name") or item_data.get("name", "").lower().strip() or None,
            quantity=item_data.get("quantity", 1),
            unit_price=item_data.get("unit_price"),
            total_price=item_data.get("total_price", 0),
            category=Category(item_data.get("category", "other")),
            volume_ml=item_data.get("volume_ml"),
        )
        session.add(item)

    return receipt


async def get_receipts(session: AsyncSession, user_id: int, days: int) -> list[Receipt]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id, Receipt.date >= since)
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_items_grouped(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            markers.canonical_sql(Item.name).label("name"),
            func.sum(Item.quantity).label("total_quantity"),
            func.sum(_item_pln_col()).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= since, _not_income())
        .group_by(markers.canonical_sql(Item.name))
        .order_by(func.sum(_item_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"name": row.name, "total_quantity": float(row.total_quantity), "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_category(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            Item.category,
            func.sum(_item_pln_col()).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= since, _personal_item_filter())
        .group_by(Item.category)
        .order_by(func.sum(_item_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"category": row.category.value, "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_store(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            _store_key().label("store"),
            func.sum(_personal_pln_col()).label("total_pln"),
            func.count(Receipt.id).label("visits"),
        )
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= since,
            Receipt.store.isnot(None),
            _store_spending(),
        )
        .group_by(_store_key())
        .order_by(func.sum(_personal_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"store": row.store, "total_pln": float(row.total_pln), "visits": row.visits} for row in result]


async def get_cash_withdrawal_total(session: AsyncSession, user_id: int, days: int) -> float:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = select(func.sum(_personal_pln_col())).where(
        Receipt.user_id == user_id,
        Receipt.date >= since,
        Receipt.tx_type == "cash_withdrawal",
    )
    result = await session.execute(stmt)
    total = result.scalar()
    return float(total) if total is not None else 0.0


async def update_receipt_store(session: AsyncSession, receipt_id: int, store: str) -> None:
    stmt = select(Receipt).where(Receipt.id == receipt_id)
    result = await session.execute(stmt)
    receipt = result.scalar_one_or_none()
    if receipt:
        receipt.store = store[:255]


async def create_bank_transactions(
    session: AsyncSession,
    user_id: int,
    transactions: list[dict],
    source: str,
) -> tuple[int, list[dict], list[dict]]:
    """Bulk-save bank-statement transactions (Erste PDF, Revolut CSV, ...).

    Each tx dict: date (YYYY-MM-DD), amount (native currency, unsigned),
    currency (defaults to PLN — Erste statements are PLN-only), total_pln
    (defaults to amount — same reason), description, category, type
    (expense/income/cash_withdrawal), plus the Erste-specific
    foreign_card_no_merchant/orig_amount/orig_currency fields.

    Returns (count_saved, pending_merchant_list, duplicates) where:
    - pending_merchant_list: foreign-card transactions that still need a merchant name
    - duplicates: transactions skipped because a matching screenshot/manual entry exists
    """
    count = 0
    pending_merchants: list[dict] = []
    duplicates: list[dict] = []

    # Snapshot already-persisted receipts for the batch's date range ONCE,
    # before any inserts. Dup checks below only ever match against this
    # frozen snapshot — never re-queried mid-loop — so two genuinely
    # identical rows within the same import (e.g. two same-day metro
    # top-ups) both save. Re-querying the DB per row would pick up this
    # batch's own not-yet-committed inserts too (session.flush() makes them
    # visible to subsequent SELECTs in the same transaction) and silently
    # collapse legitimate repeats into one.
    batch_dates: set[date] = set()
    for tx in transactions:
        if tx.get("date"):
            try:
                batch_dates.add(datetime.strptime(tx["date"], "%Y-%m-%d").date())
            except ValueError:
                pass

    existing: list[Receipt] = []
    if batch_dates:
        existing_stmt = select(Receipt).where(
            Receipt.user_id == user_id,
            Receipt.date.in_(batch_dates),
        )
        existing = list((await session.execute(existing_stmt)).scalars().all())
    exact_keys = {(r.date, r.currency, float(r.total)) for r in existing}

    for tx in transactions:
        tx_type = tx.get("type", "expense")
        if tx_type == "income":
            continue
        # Erste-only: a "transfer to Revolut" line in the PLN account would
        # double-count once the same spend is also imported from a Revolut
        # statement, so skip it there. Doesn't apply to Revolut's own export.
        if source == "erste" and "revolut" in tx.get("description", "").lower():
            continue

        try:
            date_obj = datetime.strptime(tx["date"], "%Y-%m-%d").date() if tx.get("date") else None
        except ValueError:
            date_obj = None

        amount = abs(float(tx.get("amount", 0)))
        currency = tx.get("currency") or "PLN"
        total_pln = abs(float(tx.get("total_pln", amount)))
        description = tx.get("description", "")[:255]
        category_str = tx.get("category", "other")
        try:
            category = Category(category_str)
        except ValueError:
            category = Category.other

        if date_obj is not None and (date_obj, currency, amount) in exact_keys:
            continue

        # Soft-dup check: warn if a screenshot/manual receipt already covers this transaction.
        # Match on same date + PLN amount within 0.01 (currency-agnostic) + store
        # name shares the first significant word. Checked against the same frozen
        # `existing` snapshot as above, for the same reason.
        first_word = description.split()[0] if description else ""
        if date_obj is not None and len(first_word) > 2:
            soft_dup = next(
                (
                    r for r in existing
                    if r.date == date_obj
                    and r.store
                    and first_word.lower() in r.store.lower()
                    and abs(float(r.total_pln) - total_pln) < 0.01
                ),
                None,
            )
            if soft_dup:
                duplicates.append({
                    "description": description,
                    "amount": amount,
                    "date": tx.get("date"),
                })
                continue

        is_foreign_no_merchant = tx.get("foreign_card_no_merchant", False)
        store = None if is_foreign_no_merchant else (description or None)

        receipt = Receipt(
            user_id=user_id,
            store=store,
            date=date_obj,
            currency=currency,
            total=amount,
            total_pln=total_pln,
            source=source,
            tx_type=tx_type if tx_type == "cash_withdrawal" else "purchase",
        )
        session.add(receipt)
        await session.flush()

        item = Item(
            receipt_id=receipt.id,
            name=description or markers.FOREIGN_CARD_PAYMENT,
            quantity=1,
            # Item prices are in the receipt's currency, like every other source.
            unit_price=amount,
            total_price=amount,
            category=category,
        )
        session.add(item)
        count += 1

        if is_foreign_no_merchant:
            pending_merchants.append({
                "receipt_id": receipt.id,
                "orig_amount": tx.get("orig_amount"),
                "orig_currency": tx.get("orig_currency"),
                "amount_pln": total_pln,
            })

    return count, pending_merchants, duplicates


async def set_budget(session: AsyncSession, user_id: int, category: str, limit_pln: float, month: str) -> Budget:
    stmt = select(Budget).where(
        Budget.user_id == user_id,
        Budget.category == Category(category),
        Budget.month == month,
    )
    result = await session.execute(stmt)
    budget = result.scalar_one_or_none()

    if budget:
        budget.limit_pln = limit_pln
        budget.last_notified_pct = 0
    else:
        budget = Budget(
            user_id=user_id,
            category=Category(category),
            limit_pln=limit_pln,
            month=month,
            last_notified_pct=0,
        )
        session.add(budget)

    return budget


async def get_monthly_spending_by_category(session: AsyncSession, user_id: int, month: str) -> dict[str, float]:
    year, mon = int(month[:4]), int(month[5:])
    start = date(year, mon, 1)
    _, last_day = monthrange(year, mon)
    end = date(year, mon, last_day)

    stmt = (
        select(
            Item.category,
            func.sum(_item_pln_col()).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= start, Receipt.date <= end, _personal_item_filter())
        .group_by(Item.category)
    )
    result = await session.execute(stmt)
    return {row.category.value: float(row.total_pln) for row in result}


async def get_budgets(session: AsyncSession, user_id: int, month: str) -> list[Budget]:
    stmt = select(Budget).where(Budget.user_id == user_id, Budget.month == month)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def delete_budget(session: AsyncSession, user_id: int, category: str, month: str) -> bool:
    stmt = select(Budget).where(
        Budget.user_id == user_id,
        Budget.category == Category(category),
        Budget.month == month,
    )
    result = await session.execute(stmt)
    budget = result.scalar_one_or_none()
    if budget:
        await session.delete(budget)
        return True
    return False


async def get_daily_spending(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            Receipt.date.label("day"),
            func.sum(_personal_pln_col()).label("total"),
        )
        .where(Receipt.user_id == user_id, Receipt.date >= since, Receipt.date.isnot(None))
        .group_by(Receipt.date)
        .order_by(Receipt.date)
    )
    result = await session.execute(stmt)
    db_map: dict[date, float] = {row.day: float(row.total) for row in result}

    today = datetime.utcnow().date()
    out: list[dict[str, Any]] = []
    current = since
    while current <= today:
        out.append({"date": current.isoformat(), "total": db_map.get(current, 0.0)})
        current += timedelta(days=1)
    return out


async def delete_user_data(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Delete all receipts, items (via cascade), and budgets for user_id.

    Returns a dict with counts of deleted rows per table.
    """
    receipt_ids_stmt = select(Receipt.id).where(Receipt.user_id == user_id)
    receipt_ids = list((await session.execute(receipt_ids_stmt)).scalars())

    items_deleted = 0
    if receipt_ids:
        res = await session.execute(delete(Item).where(Item.receipt_id.in_(receipt_ids)))
        items_deleted = res.rowcount

    res = await session.execute(delete(Receipt).where(Receipt.user_id == user_id))
    receipts_deleted = res.rowcount

    res = await session.execute(delete(Budget).where(Budget.user_id == user_id))
    budgets_deleted = res.rowcount

    return {
        "receipts": receipts_deleted,
        "items": items_deleted,
        "budgets": budgets_deleted,
    }


async def get_receipt_by_id(session: AsyncSession, receipt_id: int) -> Receipt | None:
    stmt = (
        select(Receipt)
        .where(Receipt.id == receipt_id)
        .options(selectinload(Receipt.items))
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def update_receipt_category(session: AsyncSession, receipt_id: int, category: Category) -> None:
    result = await session.execute(select(Receipt).where(Receipt.id == receipt_id))
    receipt = result.scalar_one_or_none()
    if receipt:
        receipt.category = category
    items_result = await session.execute(select(Item).where(Item.receipt_id == receipt_id))
    for item in items_result.scalars().all():
        item.category = category


def _norm_col():
    """Canonical expression: normalized_name if set, else LOWER(name)."""
    return func.coalesce(Item.normalized_name, func.lower(Item.name))


async def get_products_stats(
    session: AsyncSession,
    user_id: int,
    period_key: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    since = _get_since(period_key)
    conditions = [Receipt.user_id == user_id, _is_product_line()]
    if since:
        conditions.append(Receipt.date >= since)
    # Explicit range (used by /wrapped for year scoping) overrides period_key.
    if date_from:
        conditions.append(Receipt.date >= date_from)
    if date_to:
        conditions.append(Receipt.date <= date_to)

    stmt = (
        select(
            _norm_col().label("norm_name"),
            func.sum(_item_pln_col()).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
            func.count(func.distinct(Receipt.store)).label("store_count"),
            func.sum(Item.quantity * Item.volume_ml).label("total_volume_ml"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .group_by(_norm_col())
        .order_by(func.sum(_item_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "normalized_name": row.norm_name,
            "total_spent": float(row.total_spent),
            "total_qty": float(row.total_qty),
            "store_count": row.store_count,
            "total_volume_ml": int(row.total_volume_ml) if row.total_volume_ml is not None else None,
        }
        for row in result
        if row.norm_name
    ]


async def get_product_detail(session: AsyncSession, user_id: int, normalized_name: str, period_key: str) -> dict[str, Any]:
    since = _get_since(period_key)
    conditions = [Receipt.user_id == user_id, _is_product_line(), _norm_col() == normalized_name]
    if since:
        conditions.append(Receipt.date >= since)

    total_stmt = (
        select(
            func.sum(_item_pln_col()).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
            func.sum(Item.quantity * Item.volume_ml).label("total_volume_ml"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
    )
    total_row = (await session.execute(total_stmt)).one_or_none()

    store_stmt = (
        select(
            Receipt.store,
            func.sum(_item_pln_col()).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
            func.sum(Item.quantity * Item.volume_ml).label("total_volume_ml"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions, Receipt.store.isnot(None))
        .group_by(Receipt.store)
        .order_by(func.sum(_item_pln_col()).desc())
    )
    by_store = [
        {
            "store": r.store,
            "total_spent": float(r.total_spent),
            "total_qty": float(r.total_qty),
            "total_volume_ml": int(r.total_volume_ml) if r.total_volume_ml is not None else None,
        }
        for r in await session.execute(store_stmt)
    ]

    history_stmt = (
        select(Receipt.date, Receipt.store, Item.quantity, _item_pln_col().label("total_pln"))
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .order_by(Receipt.date.desc())
        .limit(10)
    )
    history = [
        {"date": r.date, "store": r.store, "qty": float(r.quantity), "total": float(r.total_pln)}
        for r in await session.execute(history_stmt)
    ]

    return {
        "total_spent": float(total_row.total_spent) if total_row and total_row.total_spent is not None else 0.0,
        "total_qty": float(total_row.total_qty) if total_row and total_row.total_qty is not None else 0.0,
        "total_volume_ml": int(total_row.total_volume_ml) if total_row and total_row.total_volume_ml is not None else None,
        "by_store": by_store,
        "history": history,
    }


async def get_all_normalized_names(session: AsyncSession, user_id: int) -> list[str]:
    stmt = (
        select(func.distinct(_norm_col()))
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, _is_product_line())
    )
    result = await session.execute(stmt)
    return [row[0] for row in result if row[0]]


async def get_store_stats_by_period(session: AsyncSession, user_id: int, period_key: str) -> list[dict[str, Any]]:
    since = _get_since(period_key)
    conditions = [
        Receipt.user_id == user_id,
        Receipt.store.isnot(None),
        _store_spending(),
    ]
    if since:
        conditions.append(Receipt.date >= since)

    stmt = (
        select(
            _store_key().label("store"),
            func.sum(_personal_pln_col()).label("total_pln"),
            func.count(Receipt.id).label("visits"),
        )
        .where(*conditions)
        .group_by(_store_key())
        .order_by(func.sum(_personal_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"store": r.store, "total_pln": float(r.total_pln), "visits": r.visits} for r in result]


async def get_transactions_for_export(
    session: AsyncSession,
    user_id: int,
    date_from: date | None,
    date_to: date | None,
) -> list[Receipt]:
    conditions = [Receipt.user_id == user_id]
    if date_from:
        conditions.append(Receipt.date >= date_from)
    if date_to:
        conditions.append(Receipt.date <= date_to)
    stmt = (
        select(Receipt)
        .where(*conditions)
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_items_for_export(
    session: AsyncSession,
    user_id: int,
    date_from: date | None,
    date_to: date | None,
) -> list[tuple]:
    conditions = [Receipt.user_id == user_id, _is_product_line()]
    if date_from:
        conditions.append(Receipt.date >= date_from)
    if date_to:
        conditions.append(Receipt.date <= date_to)
    stmt = (
        select(Receipt.date, Receipt.store, Item.name, Item.normalized_name,
               Item.quantity, Item.unit_price, Item.total_price, Receipt.currency,
               _item_pln_col().label("total_pln"), Item.category)
        .join(Item, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .order_by(Receipt.date.desc(), Item.id)
    )
    result = await session.execute(stmt)
    return list(result.all())


async def get_receipts_by_date_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[Receipt]:
    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id, Receipt.date >= date_from, Receipt.date <= date_to)
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date.desc(), Receipt.id.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_spending_by_category_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Item.category,
            func.sum(_item_pln_col()).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= date_from, Receipt.date <= date_to, _personal_item_filter())
        .group_by(Item.category)
        .order_by(func.sum(_item_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"category": row.category.value, "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_store_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    stmt = (
        select(_store_key().label("store"), func.sum(_personal_pln_col()).label("total_pln"), func.count(Receipt.id).label("visits"))
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            Receipt.store.isnot(None),
            _store_spending(),
        )
        .group_by(_store_key())
        .order_by(func.sum(_personal_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [{"store": row.store, "total_pln": float(row.total_pln), "visits": row.visits} for row in result]


async def get_total_spending_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> tuple[float, int]:
    stmt = select(func.sum(_personal_pln_col()), func.count(Receipt.id)).where(
        Receipt.user_id == user_id, Receipt.date >= date_from, Receipt.date <= date_to
    )
    row = (await session.execute(stmt)).one()
    total = float(row[0]) if row[0] is not None else 0.0
    count = int(row[1]) if row[1] is not None else 0
    return total, count


async def get_spending_by_currency(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Receipt.currency,
            # Native-currency total scaled by the same personal share as the
            # PLN column, so a split EUR receipt shows matching halves
            # (e.g. "150 EUR ≈ 125 PLN") instead of inconsistent split states.
            func.sum(
                Receipt.total * func.coalesce(
                    _personal_pln_col() / func.nullif(Receipt.total_pln, 0), 1.0
                )
            ).label("total_original"),
            func.sum(_personal_pln_col()).label("total_pln"),
            func.count(Receipt.id).label("count"),
        )
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            (Receipt.tx_type != "income") | Receipt.tx_type.is_(None),
        )
        .group_by(Receipt.currency)
        .order_by(func.sum(_personal_pln_col()).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "currency": row.currency,
            "total_original": float(row.total_original),
            "total_pln": float(row.total_pln),
            "count": row.count,
        }
        for row in result
    ]


async def get_report_settings(session: AsyncSession, user_id: int) -> ReportSettings:
    stmt = select(ReportSettings).where(ReportSettings.user_id == user_id)
    result = await session.execute(stmt)
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = ReportSettings(user_id=user_id)
        session.add(settings)
        await session.flush()
    return settings


async def set_report_setting(session: AsyncSession, user_id: int, field: str, value: bool) -> ReportSettings:
    settings = await get_report_settings(session, user_id)
    setattr(settings, field, value)
    return settings


async def get_all_users_with_reports(session: AsyncSession, report_type: str) -> list[int]:
    field_map = {
        "daily": ReportSettings.daily_enabled,
        "weekly": ReportSettings.weekly_enabled,
        "monthly": ReportSettings.monthly_enabled,
    }
    field = field_map.get(report_type)
    if field is None:
        return []
    stmt = (
        select(func.distinct(Receipt.user_id))
        .outerjoin(ReportSettings, ReportSettings.user_id == Receipt.user_id)
        .where((ReportSettings.id.is_(None)) | (field == True))
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_income_by_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> float:
    stmt = select(func.sum(_personal_pln_col())).where(
        Receipt.user_id == user_id,
        Receipt.date >= date_from,
        Receipt.date <= date_to,
        Receipt.tx_type == "income",
    )
    result = await session.execute(stmt)
    total = result.scalar()
    return float(total) if total is not None else 0.0


async def get_expenses_total_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> float:
    stmt = select(func.sum(_personal_pln_col())).where(
        Receipt.user_id == user_id,
        Receipt.date >= date_from,
        Receipt.date <= date_to,
        (Receipt.tx_type != "income") | Receipt.tx_type.is_(None),
    )
    result = await session.execute(stmt)
    total = result.scalar()
    return float(total) if total is not None else 0.0


async def get_daily_spending_and_income(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    spent_stmt = (
        select(Receipt.date.label("day"), func.sum(_personal_pln_col()).label("total"))
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            Receipt.date.isnot(None),
            (Receipt.tx_type != "income") | Receipt.tx_type.is_(None),
        )
        .group_by(Receipt.date)
    )
    spent_map: dict[date, float] = {
        row.day: float(row.total) for row in await session.execute(spent_stmt)
    }

    income_stmt = (
        select(Receipt.date.label("day"), func.sum(_personal_pln_col()).label("total"))
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            Receipt.date.isnot(None),
            Receipt.tx_type == "income",
        )
        .group_by(Receipt.date)
    )
    income_map: dict[date, float] = {
        row.day: float(row.total) for row in await session.execute(income_stmt)
    }

    out: list[dict[str, Any]] = []
    current = date_from
    while current <= date_to:
        out.append({"date": current.isoformat(), "spent": spent_map.get(current, 0.0), "income": income_map.get(current, 0.0)})
        current += timedelta(days=1)
    return out


async def get_recent_receipts(session: AsyncSession, user_id: int, limit: int) -> list[Receipt]:
    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id)
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date.desc(), Receipt.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_store_products(session: AsyncSession, user_id: int, store: str, period_key: str) -> list[dict[str, Any]]:
    since = _get_since(period_key)
    conditions = [Receipt.user_id == user_id, _store_key() == store, _is_product_line()]
    if since:
        conditions.append(Receipt.date >= since)

    stmt = (
        select(
            _norm_col().label("norm_name"),
            func.sum(_item_pln_col()).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .group_by(_norm_col())
        .order_by(func.sum(_item_pln_col()).desc())
        .limit(20)
    )
    result = await session.execute(stmt)
    return [
        {"normalized_name": r.norm_name, "total_spent": float(r.total_spent), "total_qty": float(r.total_qty)}
        for r in result
        if r.norm_name
    ]


def _normalize_merchant(store: str) -> str:
    return " ".join(store.split()).upper()


# Trailing payment-reference code or generic suffix word, e.g. "SPOTIFY P422DD162C"
# -> "SPOTIFY", "CLAUDE SUBSCRIPTION" -> "CLAUDE". Requires the input to already be
# uppercased (see _normalize_merchant) since it only matches uppercase/digit runs.
_REFERENCE_SUFFIX_RE = re.compile(r"\s?[A-Z0-9]{8,12}$")


def _strip_reference_suffix(store: str) -> str:
    stripped = _REFERENCE_SUFFIX_RE.sub("", store).strip()
    return stripped if len(stripped) > 2 else store


def _merchant_group_key(store: str) -> str:
    return _strip_reference_suffix(_normalize_merchant(store))


# Categories that recur monthly for reasons unrelated to subscriptions (rent,
# groceries, commute passes, ...) — pattern-based auto-detection must not fire
# for these, only for "other"/uncategorized items or ones already tagged
# category="subscriptions".
_PATTERN_ELIGIBLE_CATEGORIES = {None, Category.other, Category.subscriptions}


# Pattern-based detection needs at least this many chained, tolerance-matching
# occurrences — 2 lets coincidental same-amount one-offs (e.g. two unrelated
# bus-ticket top-ups) through; 3+ is much stronger evidence of a real recurrence.
_PATTERN_MIN_OCCURRENCES = 3


async def get_subscriptions(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    """Merge category="subscriptions" receipts with auto-detected recurring payments.

    A receipt is category-matched if Receipt.category is "subscriptions" or (when
    that's unset, e.g. bulk bank imports) its first item is. It's pattern-matched
    if it's part of a chain of _PATTERN_MIN_OCCURRENCES+ receipts sharing a merchant
    (normalized, with trailing reference codes/suffixes stripped), each consecutive
    pair within 5%-tolerance amount and 25-35 days apart, and its category isn't one
    that recurs monthly for non-subscription reasons (housing, groceries, transport,
    ...). Receipts satisfying both count once, as "both".

    Display fields (amount/currency/last_charge) always reflect the merchant
    group's chronologically latest receipt, since prices can change over time.
    A subscription that hasn't recurred in over 2x its average pattern interval
    is treated as stale: if it's pattern-only (no category tag backing it), it's
    dropped from the results entirely (likely cancelled); if it's category-tagged
    ("category" or "both"), it's kept but next_expected reverts to unknown rather
    than projecting a date into a series that's actually stopped.
    """
    stmt = (
        select(Receipt)
        .where(Receipt.user_id == user_id, Receipt.store.isnot(None), Receipt.date.isnot(None))
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date)
    )
    receipts = list((await session.execute(stmt)).scalars().all())
    if not receipts:
        return []

    def _primary_category(r: Receipt) -> Category | None:
        if r.category is not None:
            return r.category
        return r.items[0].category if r.items else None

    def _is_subscription_category(r: Receipt) -> bool:
        return _primary_category(r) == Category.subscriptions

    def _pattern_eligible(r: Receipt) -> bool:
        return _primary_category(r) in _PATTERN_ELIGIBLE_CATEGORIES

    groups: dict[str, list[Receipt]] = {}
    for r in receipts:
        groups.setdefault(_merchant_group_key(markers.canonical(r.store)), []).append(r)

    today = datetime.utcnow().date()
    results: list[dict[str, Any]] = []
    for group in groups.values():
        group.sort(key=lambda r: r.date)
        category_matches = [r for r in group if _is_subscription_category(r)]

        pattern_matches: dict[int, Receipt] = {}
        pattern_gaps: list[int] = []
        for prev, curr in zip(group, group[1:]):
            if not (_pattern_eligible(prev) and _pattern_eligible(curr)):
                continue
            gap_days = (curr.date - prev.date).days
            if not (25 <= gap_days <= 35):
                continue
            avg_amount = (float(prev.total_pln) + float(curr.total_pln)) / 2
            if avg_amount <= 0:
                continue
            if abs(float(curr.total_pln) - float(prev.total_pln)) <= 0.05 * avg_amount:
                pattern_matches[prev.id] = prev
                pattern_matches[curr.id] = curr
                pattern_gaps.append(gap_days)

        is_category = bool(category_matches)
        is_pattern = len(pattern_matches) >= _PATTERN_MIN_OCCURRENCES

        if not is_category and not is_pattern:
            continue

        detection = "both" if is_category and is_pattern else ("pattern" if is_pattern else "category")
        latest = group[-1]  # chronologically most recent receipt for this merchant

        next_expected = None
        if is_pattern:
            interval = round(sum(pattern_gaps) / len(pattern_gaps))
            days_since_last = (today - latest.date).days
            is_stale = days_since_last > 2 * interval
            if is_stale and not is_category:
                continue  # pattern-only and hasn't recurred in a long time -> likely cancelled
            if not is_stale:
                next_expected = latest.date + timedelta(days=interval)
                while next_expected < today:
                    next_expected += timedelta(days=interval)
            # else: category-tagged but stale -> keep the entry, but next_expected
            # reverts to unknown rather than projecting into a series that stopped.

        results.append({
            "store": min((markers.canonical(r.store) for r in group), key=len),
            "amount": float(latest.total),
            "currency": latest.currency,
            "last_charge": latest.date,
            "next_expected": next_expected,
            "monthly_total_pln": float(latest.total_pln),
            "detection": detection,
        })

    results.sort(key=lambda x: x["monthly_total_pln"], reverse=True)
    return results


async def get_category_average(
    session: AsyncSession,
    user_id: int,
    category: Category,
    days: int,
    exclude_receipt_id: int | None = None,
) -> tuple[float, int]:
    """Average and count of total_pln for past receipts whose primary category matches.

    Primary category = Receipt.category if set, else its first item's category.
    Looking back `days` naturally covers all-time history when the account is
    younger than that window.
    """
    since = datetime.utcnow().date() - timedelta(days=days)
    conditions = [Receipt.user_id == user_id, Receipt.date.isnot(None), Receipt.date >= since]
    if exclude_receipt_id is not None:
        conditions.append(Receipt.id != exclude_receipt_id)

    stmt = select(Receipt).where(*conditions).options(selectinload(Receipt.items))
    receipts = list((await session.execute(stmt)).scalars().all())

    def _matches(r: Receipt) -> bool:
        if r.category is not None:
            return r.category == category
        return bool(r.items) and r.items[0].category == category

    matched = [r for r in receipts if _matches(r)]
    if not matched:
        return 0.0, 0

    total = sum(float(r.total_pln) for r in matched)
    return total / len(matched), len(matched)


async def search_transactions(
    session: AsyncSession,
    user_id: int,
    merchant: str | None = None,
    category: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    include_cash: bool = False,
) -> list[Receipt]:
    """Natural-language search results (Feature 1).

    Fuzzy (case-insensitive substring) merchant match, optional category /
    date-range / PLN-amount filters. Cash withdrawals are excluded unless
    the query explicitly asked for cash.
    """
    conditions = [Receipt.user_id == user_id]
    if merchant:
        conditions.append(Receipt.store.ilike(f"%{merchant}%"))
    if category:
        try:
            cat = Category(category)
        except ValueError:
            cat = None
        if cat is not None:
            # OCR receipts carry category only on their items, so also match
            # via an EXISTS over items.
            conditions.append(
                or_(
                    Receipt.category == cat,
                    select(Item.id)
                    .where(Item.receipt_id == Receipt.id, Item.category == cat)
                    .exists(),
                )
            )
    if date_from:
        conditions.append(Receipt.date >= date_from)
    if date_to:
        conditions.append(Receipt.date <= date_to)
    if amount_min is not None:
        conditions.append(_personal_pln_col() >= amount_min)
    if amount_max is not None:
        conditions.append(_personal_pln_col() <= amount_max)
    if not include_cash:
        conditions.append(
            (Receipt.tx_type != "cash_withdrawal") | Receipt.tx_type.is_(None)
        )

    stmt = (
        select(Receipt)
        .where(*conditions)
        .order_by(Receipt.date.desc().nullslast(), Receipt.id.desc())
        # Hard cap: results are paginated client-side; item details for the
        # current page are fetched separately by the handler.
        .limit(200)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def set_item_personal_flags(
    session: AsyncSession, receipt_id: int, mine_item_ids: list[int]
) -> Receipt | None:
    """Persist /split results (Feature 3).

    Sets items.is_personal for every item of the receipt (True = mine,
    False = not mine; NULL stays only for receipts never split), then
    recomputes and stores Receipt.personal_total_pln from those flags using
    the rate already baked into the receipt — no fresh FX fetch.
    """
    receipt = await get_receipt_by_id(session, receipt_id)
    if receipt is None:
        return None

    mine = set(mine_item_ids)
    my_native = 0.0
    for it in receipt.items:
        it.is_personal = it.id in mine
        if it.id in mine:
            my_native += float(it.total_price)

    native_total = float(receipt.total)
    total_pln = float(receipt.total_pln)
    if native_total > 0:
        share = max(0.0, min(1.0, my_native / native_total))
    else:
        share = 1.0
    receipt.personal_total_pln = round(share * total_pln, 2)
    return receipt


async def get_recent_receipts_with_items(
    session: AsyncSession, user_id: int, limit: int = 10
) -> list[Receipt]:
    """Last N receipts with items loaded, most recent first — for /split."""
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)
    stmt = (
        select(Receipt)
        .where(
            Receipt.user_id == user_id,
            (Receipt.tx_type != "income") | Receipt.tx_type.is_(None),
        )
        .options(selectinload(Receipt.items))
        .order_by(Receipt.date.desc().nullslast(), Receipt.id.desc())
        .limit(limit * 2)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    # Prefer receipts from the last 7 days; fall back to plain most recent.
    recent = [r for r in rows if r.date and r.date >= week_ago]
    return (recent or rows)[:limit]


# ── users & language ─────────────────────────────────────────────────────────

async def has_legacy_data(session: AsyncSession, user_id: int) -> bool:
    """True if the user has any row predating the users table (receipts,
    budgets or report settings)."""
    stmt = select(
        exists().where(Receipt.user_id == user_id)
        | exists().where(Budget.user_id == user_id)
        | exists().where(ReportSettings.user_id == user_id)
    )
    return bool((await session.execute(stmt)).scalar())


async def get_user_language(session: AsyncSession, user_id: int) -> str | None:
    user = await session.get(User, user_id)
    return user.language if user else None


async def get_or_create_user_language(session: AsyncSession, user_id: int, language_code: str | None) -> str:
    """Stored language of the user; on first contact create the row.

    Anyone who already has data is a pre-localization user and gets Russian,
    everyone else gets their Telegram language (English if unsupported).
    """
    from bot.i18n import LEGACY_LANGUAGE, detect_language

    user = await session.get(User, user_id)
    if user is not None:
        return user.language

    language = LEGACY_LANGUAGE if await has_legacy_data(session, user_id) else detect_language(language_code)
    try:
        async with session.begin_nested():
            session.add(User(user_id=user_id, language=language))
    except IntegrityError:
        # Created concurrently by another update — use what won the race.
        user = await session.get(User, user_id, populate_existing=True)
        return user.language if user else language
    return language


async def get_languages_for_users(session: AsyncSession, user_ids: list[int]) -> dict[int, str]:
    """{user_id: language} for users with a stored language."""
    if not user_ids:
        return {}
    stmt = select(User.user_id, User.language).where(User.user_id.in_(user_ids))
    return {row.user_id: row.language for row in await session.execute(stmt)}


async def set_user_language(session: AsyncSession, user_id: int, language: str) -> None:
    user = await session.get(User, user_id)
    if user is None:
        session.add(User(user_id=user_id, language=language))
    else:
        user.language = language
    await session.flush()


async def resolve_user_language(session: AsyncSession, user_id: int) -> str:
    """Language for messages sent outside an update (scheduler, alerts):
    the stored one, else Russian for users with data, else the fallback."""
    from bot.i18n import FALLBACK_LANGUAGE, LEGACY_LANGUAGE

    language = await get_user_language(session, user_id)
    if language:
        return language
    return LEGACY_LANGUAGE if await has_legacy_data(session, user_id) else FALLBACK_LANGUAGE
