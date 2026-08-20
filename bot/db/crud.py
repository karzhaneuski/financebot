from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import Budget, Category, Item, Receipt, ReportSettings


def _get_since(period_key: str) -> date | None:
    days_map = {"day": 1, "week": 7, "month": 30, "year": 365}
    days = days_map.get(period_key)
    if days is None:
        return None
    return datetime.utcnow().date() - timedelta(days=days)


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
            Item.name,
            func.sum(Item.quantity).label("total_quantity"),
            func.sum(Item.total_price).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= since)
        .group_by(Item.name)
        .order_by(func.sum(Item.total_price).desc())
    )
    result = await session.execute(stmt)
    return [{"name": row.name, "total_quantity": float(row.total_quantity), "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_category(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            Item.category,
            func.sum(Item.total_price).label("total_pln"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= since)
        .group_by(Item.category)
        .order_by(func.sum(Item.total_price).desc())
    )
    result = await session.execute(stmt)
    return [{"category": row.category.value, "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_store(session: AsyncSession, user_id: int, days: int) -> list[dict[str, Any]]:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = (
        select(
            Receipt.store,
            func.sum(Receipt.total_pln).label("total_pln"),
            func.count(Receipt.id).label("visits"),
        )
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= since,
            Receipt.store.isnot(None),
            (Receipt.tx_type != "cash_withdrawal") | Receipt.tx_type.is_(None),
        )
        .group_by(Receipt.store)
        .order_by(func.sum(Receipt.total_pln).desc())
    )
    result = await session.execute(stmt)
    return [{"store": row.store, "total_pln": float(row.total_pln), "visits": row.visits} for row in result]


async def get_cash_withdrawal_total(session: AsyncSession, user_id: int, days: int) -> float:
    since = datetime.utcnow().date() - timedelta(days=days)
    stmt = select(func.sum(Receipt.total_pln)).where(
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
            name=description or "Оплата картой за рубежом",
            quantity=1,
            unit_price=total_pln,
            total_price=total_pln,
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
        select(Item.category, func.sum(Item.total_price).label("total_pln"))
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= start, Receipt.date <= end)
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
            func.sum(Receipt.total_pln).label("total"),
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
    result = await session.execute(select(Receipt).where(Receipt.id == receipt_id))
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


async def get_products_stats(session: AsyncSession, user_id: int, period_key: str) -> list[dict[str, Any]]:
    since = _get_since(period_key)
    conditions = [Receipt.user_id == user_id, Receipt.photo_file_id.isnot(None)]
    if since:
        conditions.append(Receipt.date >= since)

    stmt = (
        select(
            _norm_col().label("norm_name"),
            func.sum(Item.total_price).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
            func.count(func.distinct(Receipt.store)).label("store_count"),
            func.sum(Item.quantity * Item.volume_ml).label("total_volume_ml"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .group_by(_norm_col())
        .order_by(func.sum(Item.total_price).desc())
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
    conditions = [Receipt.user_id == user_id, Receipt.photo_file_id.isnot(None), _norm_col() == normalized_name]
    if since:
        conditions.append(Receipt.date >= since)

    total_stmt = (
        select(
            func.sum(Item.total_price).label("total_spent"),
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
            func.sum(Item.total_price).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
            func.sum(Item.quantity * Item.volume_ml).label("total_volume_ml"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions, Receipt.store.isnot(None))
        .group_by(Receipt.store)
        .order_by(func.sum(Item.total_price).desc())
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
        select(Receipt.date, Receipt.store, Item.quantity, Item.total_price)
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .order_by(Receipt.date.desc())
        .limit(10)
    )
    history = [
        {"date": r.date, "store": r.store, "qty": float(r.quantity), "total": float(r.total_price)}
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
        .where(Receipt.user_id == user_id, Receipt.photo_file_id.isnot(None))
    )
    result = await session.execute(stmt)
    return [row[0] for row in result if row[0]]


async def get_store_stats_by_period(session: AsyncSession, user_id: int, period_key: str) -> list[dict[str, Any]]:
    since = _get_since(period_key)
    conditions = [
        Receipt.user_id == user_id,
        Receipt.store.isnot(None),
        (Receipt.tx_type != "cash_withdrawal") | Receipt.tx_type.is_(None),
    ]
    if since:
        conditions.append(Receipt.date >= since)

    stmt = (
        select(
            Receipt.store,
            func.sum(Receipt.total_pln).label("total_pln"),
            func.count(Receipt.id).label("visits"),
        )
        .where(*conditions)
        .group_by(Receipt.store)
        .order_by(func.sum(Receipt.total_pln).desc())
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
    conditions = [Receipt.user_id == user_id, Receipt.photo_file_id.isnot(None)]
    if date_from:
        conditions.append(Receipt.date >= date_from)
    if date_to:
        conditions.append(Receipt.date <= date_to)
    stmt = (
        select(Receipt.date, Receipt.store, Item.name, Item.normalized_name,
               Item.quantity, Item.unit_price, Item.total_price, Item.category)
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
        select(Item.category, func.sum(Item.total_price).label("total_pln"))
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(Receipt.user_id == user_id, Receipt.date >= date_from, Receipt.date <= date_to)
        .group_by(Item.category)
        .order_by(func.sum(Item.total_price).desc())
    )
    result = await session.execute(stmt)
    return [{"category": row.category.value, "total_pln": float(row.total_pln)} for row in result]


async def get_spending_by_store_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    stmt = (
        select(Receipt.store, func.sum(Receipt.total_pln).label("total_pln"), func.count(Receipt.id).label("visits"))
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            Receipt.store.isnot(None),
            (Receipt.tx_type != "cash_withdrawal") | Receipt.tx_type.is_(None),
        )
        .group_by(Receipt.store)
        .order_by(func.sum(Receipt.total_pln).desc())
    )
    result = await session.execute(stmt)
    return [{"store": row.store, "total_pln": float(row.total_pln), "visits": row.visits} for row in result]


async def get_total_spending_range(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> tuple[float, int]:
    stmt = select(func.sum(Receipt.total_pln), func.count(Receipt.id)).where(
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
            func.sum(Receipt.total).label("total_original"),
            func.sum(Receipt.total_pln).label("total_pln"),
            func.count(Receipt.id).label("count"),
        )
        .where(
            Receipt.user_id == user_id,
            Receipt.date >= date_from,
            Receipt.date <= date_to,
            (Receipt.tx_type != "income") | Receipt.tx_type.is_(None),
        )
        .group_by(Receipt.currency)
        .order_by(func.sum(Receipt.total_pln).desc())
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
    stmt = select(func.sum(Receipt.total_pln)).where(
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
    stmt = select(func.sum(Receipt.total_pln)).where(
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
        select(Receipt.date.label("day"), func.sum(Receipt.total_pln).label("total"))
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
        select(Receipt.date.label("day"), func.sum(Receipt.total_pln).label("total"))
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
    conditions = [Receipt.user_id == user_id, Receipt.store == store, Receipt.photo_file_id.isnot(None)]
    if since:
        conditions.append(Receipt.date >= since)

    stmt = (
        select(
            _norm_col().label("norm_name"),
            func.sum(Item.total_price).label("total_spent"),
            func.sum(Item.quantity).label("total_qty"),
        )
        .join(Receipt, Item.receipt_id == Receipt.id)
        .where(*conditions)
        .group_by(_norm_col())
        .order_by(func.sum(Item.total_price).desc())
        .limit(20)
    )
    result = await session.execute(stmt)
    return [
        {"normalized_name": r.norm_name, "total_spent": float(r.total_spent), "total_qty": float(r.total_qty)}
        for r in result
        if r.norm_name
    ]
