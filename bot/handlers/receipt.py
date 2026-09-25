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
from bot.i18n import _, ngettext
from bot.keyboards.inline import (
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
from bot.services.llm import LLMUnavailableError
from bot.services.vision import parse_bank_transaction_screenshot, parse_receipt
from bot.utils.formatters import (
    currency_flag,
    format_category,
    format_date_str,
    format_items_list,
    format_receipt_amount,
)
from bot.keyboards.inline import picker_label
from bot.markers import display_name

logger = logging.getLogger(__name__)
router = Router()

# Shown when the vision provider is out of quota/balance or down — the user
# can still record the expense without image recognition. A function (not a
# module-level constant) so gettext picks the caller's current locale.
def vision_unavailable_text() -> str:
    return _(
        "⏳ Receipt recognition is temporarily unavailable. "
        "You can add the expense manually with /add "
        "or upload a bank statement (Erste PDF or Revolut CSV)."
    )

_ERSTE_KEY_TTL = 600  # 10 minutes
_REVOLUT_KEY_TTL = 600  # 10 minutes


_PDF_MAX_PAGES = 5


def _pdf_pages_to_jpeg(pdf_bytes: bytes, max_pages: int = _PDF_MAX_PAGES) -> list[bytes]:
    """Render the PDF's pages (up to max_pages) to JPEG bytes at 2× scale.

    All pages matter: on long e-receipts the "Suma PLN" total line can be on
    a later page than the items. Rendering only the first page made the model
    see no total at all.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count > max_pages:
        logger.warning("PDF has %d pages, only the first %d are recognized", doc.page_count, max_pages)
    return [
        doc[i].get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("jpeg")
        for i in range(min(doc.page_count, max_pages))
    ]


class ForeignMerchantStates(StatesGroup):
    waiting_merchant = State()


def _erste_redis_key(user_id: int) -> str:
    return f"erste_pending:{user_id}"


def _revolut_redis_key(user_id: int) -> str:
    return f"revolut_pending:{user_id}"


def _format_anomaly_alert(receipt, category_display: str, anomaly: dict) -> str:
    return _(
        "⚠️ *Unusual expense*\n"
        "{amount} PLN at {store} — that's {multiplier}× the average receipt in the "
        "“{category}” category (usually ~{average} PLN)"
    ).format(
        amount=f"{float(receipt.total_pln):.2f}",
        store=display_name(receipt.store) or "?",
        multiplier=f"{anomaly['multiplier']:.1f}",
        category=category_display,
        average=f"{anomaly['category_avg']:.0f}",
    )


@router.message(F.photo)
async def handle_receipt_photo(message: Message, bot: Bot, session: AsyncSession, redis: aioredis.Redis) -> None:
    status_msg = await message.answer(_("⏳ Processing the receipt..."))

    try:
        photo = message.photo[-1]
        buf = io.BytesIO()
        await bot.download(photo, destination=buf)
        image_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"Failed to download photo: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Couldn't download the photo. Please try again."))
        return

    # Try Erste Bank transaction screenshot detection first.
    try:
        bank_tx = await parse_bank_transaction_screenshot(image_bytes)
    except LLMUnavailableError as e:
        # The receipt parser uses the same provider — no point falling back.
        logger.warning(f"Vision provider unavailable: {e}")
        await status_msg.edit_text(vision_unavailable_text())
        return
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
        date_str = format_date_str(bank_tx["date"])
        await status_msg.edit_text(
            _("✅ Transaction saved!") + "\n"
            f"🏪 {bank_tx['merchant']} — {format_receipt_amount(receipt)}\n"
            f"📅 {date_str}\n"
            + _("🏷 Category: {category}").format(category=display_cat),
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
    except LLMUnavailableError as e:
        logger.warning(f"Vision provider unavailable: {e}")
        await status_msg.edit_text(vision_unavailable_text())
        return
    except ValueError as e:
        logger.warning(f"Receipt parse error: {e}")
        await status_msg.edit_text(
            _("❌ Couldn't read the receipt. Try taking a sharper photo "
              "or add the expense manually with /add")
        )
        return
    except Exception as e:
        logger.error(f"Vision service error: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Something went wrong while processing. Please try later."))
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

    store = data.get("store") or _("Unknown store")
    date_str = format_date_str(data.get("date"))
    items = data.get("items", [])

    lines = [
        _("✅ *Receipt saved!*") + "\n",
        _("🏪 Store: {store}").format(store=store),
        _("📅 Date: {date}").format(date=date_str),
        _("💰 Total: {amount}").format(amount=format_receipt_amount(receipt)),
    ]

    if items:
        lines.append("\n" + _("📦 Items ({count}):").format(count=len(items)))
        preview = items[:10]
        lines.append(format_items_list(preview, receipt.currency))
        if len(items) > 10:
            rest = len(items) - 10
            lines.append("  " + ngettext("... and {n} more item", "... and {n} more items", rest).format(n=rest))

    if data.get("total_mismatch"):
        lines.append("\n" + _("⚠️ The receipt total doesn't match the sum of the items."))

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

    status_msg = await message.answer(_("⏳ Downloading the file..."))

    try:
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        file_bytes = buf.getvalue()
    except Exception as e:
        logger.error(f"Failed to download document: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Couldn't download the file. Please try again."))
        return

    if is_csv:
        await _handle_revolut_csv(message, status_msg, file_bytes, redis)
        return

    pdf_bytes = file_bytes
    if not is_erste_bank_statement(pdf_bytes):
        await _handle_pdf_receipt(message, status_msg, pdf_bytes, session, redis, bot)
        return

    await status_msg.edit_text(_("⏳ Parsing the Erste Bank Polska statement..."))

    try:
        transactions = parse_erste_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"Erste PDF parse error: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Couldn't parse the statement. Try another file."))
        return

    if not transactions:
        await status_msg.edit_text(_("⚠️ No transactions found. The statement format may have changed."))
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
        _("🏦 *Erste Bank Polska — statement recognized*") + "\n",
        _("📊 Transactions found: *{count}*").format(count=len(transactions)),
        _("  — expenses: {count} pcs. totalling *{amount} PLN*").format(
            count=len(expenses), amount=f"{total_expenses:,.2f}".replace(",", " ")
        ),
        _("  — income: {count} pcs. totalling *{amount} PLN*").format(
            count=len(incomes), amount=f"{total_incomes:,.2f}".replace(",", " ")
        ),
    ]

    if expenses:
        lines.append("\n" + _("*Last 5 expenses:*"))
        for t in expenses[-5:]:
            lines.append(f"  {t['date']}  {t['category_display']}  -{abs(t['amount']):.2f} PLN")
            lines.append(f"  _{(display_name(t['description']) or '')[:60]}_")

    lines.append("\n" + _("Tap the button to save all transactions to the database:"))

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=erste_save_keyboard(),
    )


def _duplicates_header(count: int) -> str:
    return ngettext(
        "⚠️ Found {n} possible duplicate — this transaction was already added manually:",
        "⚠️ Found {n} possible duplicates — these transactions were already added manually:",
        count,
    ).format(n=count) + "\n"


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
            _("❌ Couldn't recognize the file. Supported: Erste Bank Polska statements (PDF) "
              "and Revolut consolidated statements (CSV).")
        )
        return

    await status_msg.edit_text(_("⏳ Parsing the Revolut statement..."))

    try:
        transactions = parse_revolut_csv(file_bytes)
    except Exception as e:
        logger.error(f"Revolut CSV parse error: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Couldn't parse the file. Try another export."))
        return

    if not transactions:
        await status_msg.edit_text(_("⚠️ No transactions found. The export format may have changed."))
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
        _("💳 *Revolut — consolidated statement recognized*") + "\n",
        _("📊 Purchases found: *{count}*").format(count=len(transactions)),
        _("💰 Total: *{amount} PLN*").format(amount=f"{total_pln:,.2f}".replace(",", " ")),
        "",
        _("*By currency:*") + " " + ", ".join(
            f"{currency_flag(c)} {c} ({n})" for c, n in sorted(by_currency.items())
        ),
    ]

    recent = transactions[-5:]
    if recent:
        lines.append("\n" + _("*Latest purchases:*"))
        for t in recent:
            lines.append(f"  {t['date']}  {t['category_display']}  -{t['amount']:.2f} {t['currency']}")
            lines.append(f"  _{(display_name(t['description']) or '')[:60]}_")

    lines.append("\n" + _("Tap the button to save all transactions to the database:"))

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
        await call.message.edit_text(_("❌ The data has expired. Upload the statement again."))
        return

    try:
        transactions = json.loads(raw)
    except Exception:
        await call.message.edit_text(_("❌ Couldn't read the data. Upload the statement again."))
        return

    await call.message.edit_text(_("⏳ Saving transactions..."))

    try:
        count, _pending, duplicates = await create_bank_transactions(
            session, call.from_user.id, transactions, source="revolut"
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to save Revolut transactions: {e}", exc_info=True)
        await call.message.edit_text(_("❌ Saving failed. Please try later."))
        return

    await redis.delete(_revolut_redis_key(call.from_user.id))

    total_pln = sum(t["total_pln"] for t in transactions)
    await call.message.edit_text(
        ngettext("✅ *Saved {n} transaction!*", "✅ *Saved {n} transactions!*", count).format(n=count) + "\n\n"
        + _("💸 Expenses: {amount} PLN").format(amount=f"{total_pln:,.2f}".replace(",", " ")) + "\n\n"
        + _("Use /stats to see your statistics."),
        parse_mode="Markdown",
    )

    if duplicates:
        lines = [_duplicates_header(len(duplicates))]
        for d in duplicates:
            date_str = format_date_str(d["date"]) if d.get("date") else "?"
            lines.append(f"  — {display_name(d['description'])} — {d['amount']:.2f} — {date_str}")
        lines.append("\n" + _("They were not added again."))
        await call.message.answer("\n".join(lines))

    await budget_service.check_and_notify_budgets(session, call.from_user.id, bot)


@router.callback_query(F.data == "revolut:cancel")
async def revolut_cancel_callback(call: CallbackQuery, redis: aioredis.Redis) -> None:
    await call.answer()
    await redis.delete(_revolut_redis_key(call.from_user.id))
    await call.message.edit_text(_("❌ Saving cancelled."))


async def _handle_pdf_receipt(
    message: Message,
    status_msg: Message,
    pdf_bytes: bytes,
    session: AsyncSession,
    redis: aioredis.Redis,
    bot: Bot,
) -> None:
    """Render the PDF's pages to images and run them through the receipt vision pipeline."""
    await status_msg.edit_text(_("⏳ Converting the PDF to an image..."))

    try:
        image_bytes = _pdf_pages_to_jpeg(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF render error: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Couldn't read the PDF. Try another file."))
        return

    await status_msg.edit_text(_("⏳ Processing the receipt..."))

    try:
        data = await parse_receipt(image_bytes)
    except LLMUnavailableError as e:
        logger.warning(f"Vision provider unavailable: {e}")
        await status_msg.edit_text(vision_unavailable_text())
        return
    except ValueError as e:
        logger.warning(f"Receipt parse error from PDF: {e}")
        await status_msg.edit_text(
            _("❌ Couldn't read the receipt in the PDF. Try sending a photo of the receipt "
              "or add the expense manually with /add")
        )
        return
    except Exception as e:
        logger.error(f"Vision service error for PDF receipt: {e}", exc_info=True)
        await status_msg.edit_text(_("❌ Something went wrong while processing. Please try later."))
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

    store = data.get("store") or _("Unknown store")
    date_str = format_date_str(data.get("date"))
    items = data.get("items", [])

    lines = [
        _("✅ *Receipt from PDF saved!*") + "\n",
        _("🏪 Store: {store}").format(store=store),
        _("📅 Date: {date}").format(date=date_str),
        _("💰 Total: {amount}").format(amount=format_receipt_amount(receipt)),
    ]

    if items:
        lines.append("\n" + _("📦 Items ({count}):").format(count=len(items)))
        preview = items[:10]
        lines.append(format_items_list(preview, receipt.currency))
        if len(items) > 10:
            rest = len(items) - 10
            lines.append("  " + ngettext("... and {n} more item", "... and {n} more items", rest).format(n=rest))

    if data.get("total_mismatch"):
        lines.append("\n" + _("⚠️ The receipt total doesn't match the sum of the items."))

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
        await call.message.edit_text(_("❌ The data has expired. Upload the statement again."))
        return

    try:
        transactions = json.loads(raw)
    except Exception:
        await call.message.edit_text(_("❌ Couldn't read the data. Upload the statement again."))
        return

    await call.message.edit_text(_("⏳ Saving transactions..."))

    try:
        count, pending_merchants, duplicates = await create_bank_transactions(
            session, call.from_user.id, transactions, source="erste"
        )
        await session.commit()
    except Exception as e:
        logger.error(f"Failed to save Erste transactions: {e}", exc_info=True)
        await call.message.edit_text(_("❌ Saving failed. Please try later."))
        return

    await redis.delete(_erste_redis_key(call.from_user.id))

    expenses = [t for t in transactions if t["type"] == "expense"]
    incomes = [t for t in transactions if t["type"] == "income"]
    total_expenses = sum(abs(t["amount"]) for t in expenses)
    total_incomes = sum(t["amount"] for t in incomes)

    await call.message.edit_text(
        ngettext("✅ *Saved {n} transaction!*", "✅ *Saved {n} transactions!*", count).format(n=count) + "\n\n"
        + _("💸 Expenses: {amount} PLN").format(amount=f"{total_expenses:,.2f}".replace(",", " ")) + "\n"
        + _("💰 Income: {amount} PLN").format(amount=f"{total_incomes:,.2f}".replace(",", " ")) + "\n\n"
        + _("Use /stats to see your statistics."),
        parse_mode="Markdown",
    )

    if duplicates:
        lines = [_duplicates_header(len(duplicates))]
        for d in duplicates:
            date_str = format_date_str(d["date"]) if d.get("date") else "?"
            lines.append(f"  — {display_name(d['description'])} — {d['amount']:.2f} PLN — {date_str}")
        lines.append("\n" + _("They were not added again."))
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
        _("💳 Card payment abroad: {amount} {currency} ({amount_pln} PLN)\n"
          "What should this payment be called? Enter the store/service name:").format(
            amount=orig_amount, currency=orig_currency, amount_pln=f"{amount_pln:.2f}"
        )
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
        await message.answer(_("Enter a non-empty name:"))
        return

    pm = pending_merchants[pm_index]
    await update_receipt_store(session, pm["receipt_id"], store_name)
    await session.commit()

    await message.answer(_("✅ Saved: *{name}*").format(name=store_name), parse_mode="Markdown")

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
    await call.message.edit_text(_("❌ Saving cancelled."))


@router.callback_query(F.data.startswith("recat:"))
async def recat_callback(call: CallbackQuery) -> None:
    receipt_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=recat_categories_keyboard(receipt_id))


@router.callback_query(F.data.startswith("recat_set:"))
async def recat_set_callback(call: CallbackQuery, session: AsyncSession) -> None:
    _prefix, receipt_id_str, category_str = call.data.split(":")
    receipt_id = int(receipt_id_str)
    await call.answer()

    try:
        category = Category(category_str)
    except ValueError:
        await call.answer(_("❌ Unknown category"), show_alert=True)
        return

    await update_receipt_category(session, receipt_id, category)

    receipt = await get_receipt_by_id(session, receipt_id)
    cat_label = picker_label(category_str)

    if receipt:
        date_str = format_date_str(receipt.date.isoformat() if receipt.date else None)
        text = (
            _("✅ Saved!") + "\n"
            f"🏪 {display_name(receipt.store) or '?'} — {format_receipt_amount(receipt)}\n"
            f"📅 {date_str}\n"
            + _("🏷 Category: {category}").format(category=cat_label) + " ✓"
        )
    else:
        text = _("✅ Category updated: {category}").format(category=cat_label)

    await call.message.edit_text(text, reply_markup=recat_keyboard(receipt_id))
