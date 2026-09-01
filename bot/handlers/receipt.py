import io
import json
import logging

import fitz  # pymupdf
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from bot.db.crud import (
    create_bank_transactions,
    create_receipt,
    get_receipt_by_id,
    update_receipt_category,
    update_receipt_store,
)
from bot.db.models import Category
from bot.keyboards.inline import (
    CATEGORY_LABEL,
    erste_save_keyboard,
    recat_categories_keyboard,
    recat_keyboard,
    revolut_save_keyboard,
)
from bot.parsers.erste import categorize, is_erste_bank_statement, parse_erste_pdf
from bot.parsers.revolut import is_revolut_statement, parse_revolut_csv
from bot.services import budget as budget_service
from bot.services.anomaly import check_anomaly
from bot.services.currency import convert_to_pln
from bot.services.normalization import normalize_item_names
from bot.services.vision import parse_bank_transaction_screenshot, parse_receipt
from bot.utils.formatters import (
    currency_flag,
    format_category,
    format_date_ru,
    format_items_list,
    format_receipt_amount,
)

logger = logging.getLogger(__name__)
router = Router()

_ERSTE_KEY_TTL = 600  # 10 minutes
_REVOLUT_KEY_TTL = 600  # 10 minutes


def _pdf_first_page_to_jpeg(pdf_bytes: bytes) -> bytes:
    """Render the first page of a PDF to JPEG bytes at 2× scale."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    return pix.tobytes("jpeg")


class ForeignMerchantStates(StatesGroup):
    waiting_merchant = State()


def _erste_redis_key(user_id: int) -> str:
    return f"erste_pending:{user_id}"


def _revolut_redis_key(user_id: int) -> str:
    return f"revolut_pending:{user_id}"


def _format_anomaly_alert(receipt, category_display: str, anomaly: dict) -> str:
    return (
        "⚠️ *Необычная трата*\n"
        f"{float(receipt.total_pln):.2f} PLN в {receipt.store or '?'} — это в "
        f"{anomaly['multiplier']:.1f} раза больше среднего чека в категории «{category_display}» "
        f"(обычно ~{anomaly['category_avg']:.0f} PLN)"
    )


@router.message(F.photo)
async def handle_receipt_photo(message: Message, bot: Bot, session: AsyncSession, redis: aioredis.Redis) -> None:
    status_msg = await message.answer("⏳ Обрабатываю чек...")

    try:
        photo = message.photo[-1]
        buf = io.BytesIO()
        await bot.download(photo, destination=buf)
        image_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"Failed to download photo: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось загрузить фото. Попробуй ещё раз.")
        return

    # Try Erste Bank transaction screenshot detection first.
    try:
        bank_tx = await parse_bank_transaction_screenshot(image_bytes)
    except Exception as e:
        logger.warning(f"Bank screenshot detection failed, falling back to receipt parser: {e}")
        bank_tx = None

    if bank_tx is not None:
        db_cat, display_cat = categorize(bank_tx["merchant"])
        try:
            cat_enum = Category(db_cat)
        except ValueError:
            cat_enum = Category.other

        bank_currency = bank_tx["currency"]
        bank_amount = float(bank_tx["amount"])
        bank_total_pln = await convert_to_pln(bank_amount, bank_currency, redis)

        receipt = await create_receipt(
            session,
            user_id=message.from_user.id,
            data={
                "store": bank_tx["merchant"],
                "date": bank_tx["date"],
                "currency": bank_currency,
                "total": bank_amount,
                "items": [],
            },
            photo_file_id=photo.file_id,
            total_pln=bank_total_pln,
            source="screenshot",
            tx_type="purchase",
            category=cat_enum,
        )
        date_str = format_date_ru(bank_tx["date"])
        await status_msg.edit_text(
            f"✅ Транзакция сохранена!\n"
            f"🏪 {bank_tx['merchant']} — {format_receipt_amount(receipt)}\n"
            f"📅 {date_str}\n"
            f"🏷 Категория: {display_cat}",
            reply_markup=recat_keyboard(receipt.id),
        )

        anomaly = await check_anomaly(session, message.from_user.id, receipt)
        if anomaly:
            await message.answer(
                _format_anomaly_alert(receipt, format_category(db_cat), anomaly),
                parse_mode="Markdown",
            )
        return

    try:
        data = await parse_receipt(image_bytes)
    except ValueError as e:
        logger.warning(f"Receipt parse error: {e}")
        await status_msg.edit_text(
            "❌ Не удалось распознать чек. Попробуй сфотографировать чётче "
            "или добавь трату вручную командой /add"
        )
        return
    except Exception as e:
        logger.error(f"Vision service error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Произошла ошибка при обработке. Попробуй позже.")
        return

    currency = data.get("currency", "PLN")
    total = float(data.get("total", 0))

    total_pln = await convert_to_pln(total, currency, redis)

    if data.get("items"):
        await normalize_item_names(data["items"])

    receipt = await create_receipt(
        session,
        user_id=message.from_user.id,
        data=data,
        photo_file_id=photo.file_id,
        total_pln=total_pln,
    )

    store = data.get("store") or "Неизвестный магазин"
    date_str = format_date_ru(data.get("date"))
    items = data.get("items", [])

    lines = [
        "✅ *Чек сохранён!*\n",
        f"🏪 Магазин: {store}",
        f"📅 Дата: {date_str}",
        f"💰 Итого: {format_receipt_amount(receipt)}",
    ]

    if items:
        lines.append(f"\n📦 Товары ({len(items)}):")
        preview = items[:10]
        lines.append(format_items_list(preview, receipt.currency))
        if len(items) > 10:
            lines.append(f"  ... и ещё {len(items) - 10} позиций")

    if data.get("total_mismatch"):
        lines.append("\n⚠️ Итог чека не совпадает с суммой позиций.")

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=recat_keyboard(receipt.id),
    )

    anomaly = await check_anomaly(session, message.from_user.id, receipt)
    if anomaly and items:
        await message.answer(
            _format_anomaly_alert(receipt, format_category(items[0].get("category", "other")), anomaly),
            parse_mode="Markdown",
        )

    await budget_service.check_and_notify_budgets(session, message.from_user.id, bot)


@router.message(F.document)
async def handle_document(message: Message, bot: Bot, session: AsyncSession, redis: aioredis.Redis) -> None:
    doc = message.document
    mime = doc.mime_type or ""
    filename = (doc.file_name or "").lower()
    is_pdf = mime == "application/pdf" or filename.endswith(".pdf")
    is_csv = (
        mime in ("text/csv", "text/comma-separated-values", "application/vnd.ms-excel")
        or filename.endswith(".csv")
    )
    if not (is_pdf or is_csv):
        return

    status_msg = await message.answer("⏳ Загружаю файл...")

    try:
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        file_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"Failed to download document: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось загрузить файл. Попробуй ещё раз.")
        return

    if is_csv:
        await _handle_revolut_csv(message, status_msg, file_bytes, redis)
        return

    pdf_bytes = file_bytes
    if not is_erste_bank_statement(pdf_bytes):
        await _handle_pdf_receipt(message, status_msg, pdf_bytes, session, redis, bot)
        return

    await status_msg.edit_text("⏳ Парсю выписку Erste Bank Polska...")

    try:
        transactions = parse_erste_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"Erste PDF parse error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось разобрать выписку. Попробуй другой файл.")
        return

    if not transactions:
        await status_msg.edit_text("⚠️ Транзакции не найдены. Возможно, формат выписки изменился.")
        return

    expenses = [t for t in transactions if t["type"] == "expense"]
    incomes = [t for t in transactions if t["type"] == "income"]
    total_expenses = sum(abs(t["amount"]) for t in expenses)
    total_incomes = sum(t["amount"] for t in incomes)

    # Save parsed transactions to Redis for the confirm callback
    await redis.set(
        _erste_redis_key(message.from_user.id),
        json.dumps(transactions, ensure_ascii=False),
        ex=_ERSTE_KEY_TTL,
    )

    lines = [
        "🏦 *Erste Bank Polska — выписка распознана*\n",
        f"📊 Транзакций найдено: *{len(transactions)}*",
        f"  — расходы: {len(expenses)} шт. на *{total_expenses:,.2f} PLN*".replace(",", " "),
        f"  — доходы: {len(incomes)} шт. на *{total_incomes:,.2f} PLN*".replace(",", " "),
    ]

    if expenses:
        lines.append("\n*Последние 5 расходов:*")
        for t in expenses[-5:]:
            lines.append(f"  {t['date']}  {t['category_display']}  -{abs(t['amount']):.2f} PLN")
            lines.append(f"  _{t['description'][:60]}_")

    lines.append("\nНажми кнопку, чтобы сохранить все транзакции в базу данных:")

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=erste_save_keyboard(),
    )


async def _handle_revolut_csv(
    message: Message,
    status_msg: Message,
    file_bytes: bytes,
    redis: aioredis.Redis,
) -> None:
    """Parse a Revolut consolidated statement CSV and stage it for confirmation."""
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    if not is_revolut_statement(text):
        await status_msg.edit_text(
            "❌ Не удалось распознать файл. Поддерживаются выписки Erste Bank Polska (PDF) "
            "и сводные данные Revolut (CSV)."
        )
        return

    await status_msg.edit_text("⏳ Парсю выписку Revolut...")

    try:
        transactions = parse_revolut_csv(file_bytes)
    except Exception as e:
        logger.error(f"Revolut CSV parse error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось разобрать файл. Попробуй другой экспорт.")
        return

    if not transactions:
        await status_msg.edit_text("⚠️ Транзакции не найдены. Возможно, формат экспорта изменился.")
        return

    total_pln = sum(t["total_pln"] for t in transactions)
    by_currency: dict[str, int] = {}
    for t in transactions:
        by_currency[t["currency"]] = by_currency.get(t["currency"], 0) + 1

    await redis.set(
        _revolut_redis_key(message.from_user.id),
        json.dumps(transactions, ensure_ascii=False),
        ex=_REVOLUT_KEY_TTL,
    )

    lines = [
        "💳 *Revolut — сводная выписка распознана*\n",
        f"📊 Покупок найдено: *{len(transactions)}*",
        f"💰 Итого: *{total_pln:,.2f} PLN*".replace(",", " "),
        "",
        "*По валютам:* " + ", ".join(
            f"{currency_flag(c)} {c} ({n})" for c, n in sorted(by_currency.items())
        ),
    ]

    recent = transactions[-5:]
    if recent:
        lines.append("\n*Последние покупки:*")
        for t in recent:
            lines.append(f"  {t['date']}  {t['category_display']}  -{t['amount']:.2f} {t['currency']}")
            lines.append(f"  _{t['description'][:60]}_")

    lines.append("\nНажми кнопку, чтобы сохранить все транзакции в базу данных:")

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=revolut_save_keyboard(),
    )


@router.callback_query(F.data == "revolut:save")
async def revolut_save_callback(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: aioredis.Redis,
) -> None:
    await call.answer()

    raw = await redis.get(_revolut_redis_key(call.from_user.id))
    if not raw:
        await call.message.edit_text("❌ Данные истекли. Загрузи выписку ещё раз.")
        return

    try:
        transactions = json.loads(raw)
    except Exception:
        await call.message.edit_text("❌ Ошибка чтения данных. Загрузи выписку ещё раз.")
        return

    await call.message.edit_text("⏳ Сохраняю транзакции...")

    try:
        count, _pending, duplicates = await create_bank_transactions(
            session, call.from_user.id, transactions, source="revolut"
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to save Revolut transactions: {e}", exc_info=True)
        await call.message.edit_text("❌ Ошибка при сохранении. Попробуй позже.")
        return

    await redis.delete(_revolut_redis_key(call.from_user.id))

    total_pln = sum(t["total_pln"] for t in transactions)
    await call.message.edit_text(
        f"✅ *Сохранено {count} транзакций!*\n\n"
        f"💸 Расходы: {total_pln:,.2f} PLN\n\n".replace(",", " ") +
        "Используй /stats для просмотра статистики.",
        parse_mode="Markdown",
    )

    if duplicates:
        lines = [f"⚠️ Найдено {len(duplicates)} возможных дублей — эти транзакции уже были добавлены вручную:\n"]
        for d in duplicates:
            date_str = format_date_ru(d["date"]) if d.get("date") else "?"
            lines.append(f"  — {d['description']} — {d['amount']:.2f} — {date_str}")
        lines.append("\nОни не были добавлены повторно.")
        await call.message.answer("\n".join(lines))

    await budget_service.check_and_notify_budgets(session, call.from_user.id, bot)


@router.callback_query(F.data == "revolut:cancel")
async def revolut_cancel_callback(call: CallbackQuery, redis: aioredis.Redis) -> None:
    await call.answer()
    await redis.delete(_revolut_redis_key(call.from_user.id))
    await call.message.edit_text("❌ Сохранение отменено.")


async def _handle_pdf_receipt(
    message: Message,
    status_msg: Message,
    pdf_bytes: bytes,
    session: AsyncSession,
    redis: aioredis.Redis,
    bot: Bot,
) -> None:
    """Convert first PDF page to image and run through the receipt vision pipeline."""
    await status_msg.edit_text("⏳ Конвертирую PDF в изображение...")

    try:
        image_bytes = _pdf_first_page_to_jpeg(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF render error: {e}", exc_info=True)
        await status_msg.edit_text("❌ Не удалось прочитать PDF. Попробуй другой файл.")
        return

    await status_msg.edit_text("⏳ Обрабатываю чек...")

    try:
        data = await parse_receipt(image_bytes)
    except ValueError as e:
        logger.warning(f"Receipt parse error from PDF: {e}")
        await status_msg.edit_text(
            "❌ Не удалось распознать чек в PDF. Попробуй прислать фото чека "
            "или добавь трату вручную командой /add"
        )
        return
    except Exception as e:
        logger.error(f"Vision service error for PDF receipt: {e}", exc_info=True)
        await status_msg.edit_text("❌ Произошла ошибка при обработке. Попробуй позже.")
        return

    currency = data.get("currency", "PLN")
    total = float(data.get("total", 0))
    total_pln = await convert_to_pln(total, currency, redis)

    if data.get("items"):
        await normalize_item_names(data["items"])

    receipt = await create_receipt(
        session,
        user_id=message.from_user.id,
        data=data,
        photo_file_id=None,
        total_pln=total_pln,
    )

    store = data.get("store") or "Неизвестный магазин"
    date_str = format_date_ru(data.get("date"))
    items = data.get("items", [])

    lines = [
        "✅ *Чек из PDF сохранён!*\n",
        f"🏪 Магазин: {store}",
        f"📅 Дата: {date_str}",
        f"💰 Итого: {format_receipt_amount(receipt)}",
    ]

    if items:
        lines.append(f"\n📦 Товары ({len(items)}):")
        preview = items[:10]
        lines.append(format_items_list(preview, receipt.currency))
        if len(items) > 10:
            lines.append(f"  ... и ещё {len(items) - 10} позиций")

    if data.get("total_mismatch"):
        lines.append("\n⚠️ Итог чека не совпадает с суммой позиций.")

    await status_msg.edit_text("\n".join(lines), parse_mode="Markdown")

    await budget_service.check_and_notify_budgets(session, message.from_user.id, bot)


@router.callback_query(F.data == "erste:save")
async def erste_save_callback(
    call: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    redis: aioredis.Redis,
    state: FSMContext,
) -> None:
    await call.answer()

    raw = await redis.get(_erste_redis_key(call.from_user.id))
    if not raw:
        await call.message.edit_text("❌ Данные истекли. Загрузи выписку ещё раз.")
        return

    try:
        transactions = json.loads(raw)
    except Exception:
        await call.message.edit_text("❌ Ошибка чтения данных. Загрузи выписку ещё раз.")
        return

    await call.message.edit_text("⏳ Сохраняю транзакции...")

    try:
        count, pending_merchants, duplicates = await create_bank_transactions(
            session, call.from_user.id, transactions, source="erste"
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to save Erste transactions: {e}", exc_info=True)
        await call.message.edit_text("❌ Ошибка при сохранении. Попробуй позже.")
        return

    await redis.delete(_erste_redis_key(call.from_user.id))

    expenses = [t for t in transactions if t["type"] == "expense"]
    incomes = [t for t in transactions if t["type"] == "income"]
    total_expenses = sum(abs(t["amount"]) for t in expenses)
    total_incomes = sum(t["amount"] for t in incomes)

    await call.message.edit_text(
        f"✅ *Сохранено {count} транзакций!*\n\n"
        f"💸 Расходы: {total_expenses:,.2f} PLN\n".replace(",", " ") +
        f"💰 Доходы: {total_incomes:,.2f} PLN\n\n".replace(",", " ") +
        "Используй /stats для просмотра статистики.",
        parse_mode="Markdown",
    )

    if duplicates:
        lines = [f"⚠️ Найдено {len(duplicates)} возможных дублей — эти транзакции уже были добавлены вручную:\n"]
        for d in duplicates:
            date_str = format_date_ru(d["date"]) if d.get("date") else "?"
            lines.append(f"  — {d['description']} — {d['amount']:.2f} PLN — {date_str}")
        lines.append("\nОни не были добавлены повторно.")
        await call.message.answer("\n".join(lines))

    if pending_merchants:
        await state.set_state(ForeignMerchantStates.waiting_merchant)
        await state.update_data(pending_merchants=pending_merchants, pm_index=0)
        await _ask_foreign_merchant(call.message, pending_merchants[0])

    await budget_service.check_and_notify_budgets(session, call.from_user.id, bot)


async def _ask_foreign_merchant(message: Message, pm: dict) -> None:
    orig_amount = pm["orig_amount"]
    orig_currency = pm["orig_currency"]
    amount_pln = pm["amount_pln"]
    await message.answer(
        f"💳 Оплата картой за рубежом: {orig_amount} {orig_currency} ({amount_pln:.2f} PLN)\n"
        "Как назвать этот платёж? Введите название магазина/сервиса:"
    )


@router.message(ForeignMerchantStates.waiting_merchant, ~F.text.startswith("/"))
async def handle_foreign_merchant_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    pending_merchants: list[dict] = data["pending_merchants"]
    pm_index: int = data["pm_index"]

    store_name = message.text.strip()
    if not store_name:
        await message.answer("Введите непустое название:")
        return

    pm = pending_merchants[pm_index]
    await update_receipt_store(session, pm["receipt_id"], store_name)
    await session.commit()

    await message.answer(f"✅ Сохранено: *{store_name}*", parse_mode="Markdown")

    next_index = pm_index + 1
    if next_index < len(pending_merchants):
        await state.update_data(pm_index=next_index)
        await _ask_foreign_merchant(message, pending_merchants[next_index])
    else:
        await state.clear()


@router.callback_query(F.data == "erste:cancel")
async def erste_cancel_callback(call: CallbackQuery, redis: aioredis.Redis) -> None:
    await call.answer()
    await redis.delete(_erste_redis_key(call.from_user.id))
    await call.message.edit_text("❌ Сохранение отменено.")


@router.callback_query(F.data.startswith("recat:"))
async def recat_callback(call: CallbackQuery) -> None:
    receipt_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=recat_categories_keyboard(receipt_id))


@router.callback_query(F.data.startswith("recat_set:"))
async def recat_set_callback(call: CallbackQuery, session: AsyncSession) -> None:
    _, receipt_id_str, category_str = call.data.split(":")
    receipt_id = int(receipt_id_str)
    await call.answer()

    try:
        category = Category(category_str)
    except ValueError:
        await call.answer("❌ Неизвестная категория", show_alert=True)
        return

    await update_receipt_category(session, receipt_id, category)

    receipt = await get_receipt_by_id(session, receipt_id)
    cat_label = CATEGORY_LABEL.get(category_str, category_str)

    if receipt:
        date_str = format_date_ru(receipt.date.isoformat() if receipt.date else None)
        text = (
            f"✅ Сохранено!\n"
            f"🏪 {receipt.store or '?'} — {format_receipt_amount(receipt)}\n"
            f"📅 {date_str}\n"
            f"🏷 Категория: {cat_label} ✓"
        )
    else:
        text = f"✅ Категория обновлена: {cat_label}"

    await call.message.edit_text(text, reply_markup=recat_keyboard(receipt_id))
