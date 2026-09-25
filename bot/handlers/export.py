import csv
import io
import logging
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.i18n import _, ngettext
from bot.keyboards.inline import export_done_keyboard, export_period_keyboard, export_type_keyboard
from bot.markers import display_name

logger = logging.getLogger(__name__)
router = Router()


class ExportStates(StatesGroup):
    waiting_dates = State()


def _period_dates(period: str) -> tuple[date | None, date | None]:
    today = date.today()
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "month":
        return today.replace(day=1), today
    if period == "year":
        return today.replace(month=1, day=1), today
    return None, None  # "all"


def _filename(export_type: str, date_from: date | None, date_to: date | None) -> str:
    type_slug = {"transactions": "transactions", "items": "items", "all": "all"}[export_type]
    if date_from and date_to and date_from == date_to:
        period_slug = date_from.strftime("%d.%m.%Y")
    elif date_from and date_to:
        period_slug = f"{date_from.strftime('%d.%m')}-{date_to.strftime('%d.%m.%Y')}"
    elif date_from or date_to:
        d = (date_from or date_to)
        period_slug = d.strftime("%Y-%m")
    else:
        period_slug = "all"
    return f"financebot_{type_slug}_{period_slug}.csv"


def _build_transactions_csv(receipts: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "store", "amount_pln", "currency", "category", "type", "source"])
    for r in receipts:
        category = r.category.value if r.category else (
            r.items[0].category.value if r.items else "other"
        )
        writer.writerow([
            r.date.isoformat() if r.date else "",
            display_name(r.store) or "",
            float(r.total_pln),
            r.currency or "PLN",
            category,
            r.tx_type or "purchase",
            r.source or "receipt",
        ])
    return buf.getvalue()


def _build_items_csv(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "store", "product", "normalized_name", "quantity", "unit_price", "total_price", "category"])
    for row in rows:
        writer.writerow([
            row.date.isoformat() if row.date else "",
            display_name(row.store) or "",
            display_name(row.name),
            row.normalized_name or display_name(row.name).lower().strip(),
            float(row.quantity),
            float(row.unit_price) if row.unit_price is not None else "",
            float(row.total_price),
            row.category.value,
        ])
    return buf.getvalue()


def _build_all_csv(receipts: list, item_rows: list) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["type", "date", "store", "description", "amount_pln", "category", "source"])
    for r in receipts:
        category = r.category.value if r.category else (
            r.items[0].category.value if r.items else "other"
        )
        writer.writerow([
            "transaction",
            r.date.isoformat() if r.date else "",
            display_name(r.store) or "",
            "",
            float(r.total_pln),
            category,
            r.source or "receipt",
        ])
    for row in item_rows:
        writer.writerow([
            "item",
            row.date.isoformat() if row.date else "",
            display_name(row.store) or "",
            row.name,
            float(row.total_price),
            row.category.value,
            "receipt",
        ])
    return buf.getvalue()


async def _send_csv(
    target: Message | CallbackQuery,
    session: AsyncSession,
    export_type: str,
    date_from: date | None,
    date_to: date | None,
) -> None:
    msg = target if isinstance(target, Message) else target.message
    user_id = target.from_user.id

    status = await msg.answer(_("⏳ Building the CSV..."))
    try:
        if export_type == "transactions":
            receipts = await crud.get_transactions_for_export(session, user_id, date_from, date_to)
            csv_text = _build_transactions_csv(receipts)
            count = len(receipts)
        elif export_type == "items":
            rows = await crud.get_items_for_export(session, user_id, date_from, date_to)
            csv_text = _build_items_csv(rows)
            count = len(rows)
        else:  # all
            receipts = await crud.get_transactions_for_export(session, user_id, date_from, date_to)
            item_rows = await crud.get_items_for_export(session, user_id, date_from, date_to)
            csv_text = _build_all_csv(receipts, item_rows)
            count = len(receipts) + len(item_rows)
    except Exception as e:
        logger.error(f"CSV export failed: {e}", exc_info=True)
        await status.edit_text(_("❌ Couldn't create the file. Please try again."))
        return

    await status.delete()

    if count == 0:
        await msg.answer(
            _("📭 No data for the selected period."),
            reply_markup=export_done_keyboard(),
        )
        return

    filename = _filename(export_type, date_from, date_to)
    await msg.answer_document(
        BufferedInputFile(csv_text.encode("utf-8-sig"), filename=filename),
        caption=ngettext("✅ Export ready — {n} row", "✅ Export ready — {n} rows", count).format(n=count),
        reply_markup=export_done_keyboard(),
    )


@router.message(Command("export"))
async def cmd_export(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        _("📤 *Data export*\n\nWhat do you want to export?"),
        parse_mode="Markdown",
        reply_markup=export_type_keyboard(),
    )


@router.callback_query(F.data.startswith("export_type:"))
async def cb_export_type(callback: CallbackQuery, state: FSMContext) -> None:
    export_type = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        _("📅 Choose a period:"),
        reply_markup=export_period_keyboard(export_type),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("export_period:"))
async def cb_export_period(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _prefix, export_type, period = callback.data.split(":", 2)

    if period == "custom":
        await state.set_state(ExportStates.waiting_dates)
        await state.update_data(export_type=export_type)
        await callback.message.edit_text(
            _("📅 Enter the period as *DD.MM.YYYY-DD.MM.YYYY*\n"
              "For example: `01.05.2026-31.05.2026`"),
            parse_mode="Markdown",
        )
        await callback.answer()
        return

    await callback.message.delete()
    date_from, date_to = _period_dates(period)
    await _send_csv(callback, session, export_type, date_from, date_to)
    await callback.answer()


@router.message(ExportStates.waiting_dates)
async def handle_custom_dates(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    export_type = data.get("export_type", "transactions")
    await state.clear()

    raw = (message.text or "").strip()
    try:
        left, right = raw.split("-", 1)
        date_from = date(int(left[6:10]), int(left[3:5]), int(left[0:2]))
        date_to = date(int(right[6:10]), int(right[3:5]), int(right[0:2]))
        if date_from > date_to:
            raise ValueError("start > end")
    except Exception:
        await message.answer(
            _("❌ Invalid format. Enter the period as *DD.MM.YYYY-DD.MM.YYYY*\n"
              "For example: `01.05.2026-31.05.2026`\n\n"
              "Or /cancel to cancel."),
            parse_mode="Markdown",
        )
        await state.set_state(ExportStates.waiting_dates)
        await state.update_data(export_type=export_type)
        return

    await _send_csv(message, session, export_type, date_from, date_to)


@router.callback_query(F.data.startswith("export_done:"))
async def cb_export_done(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    if action == "again":
        await callback.message.edit_text(
            _("📤 *Data export*\n\nWhat do you want to export?"),
            parse_mode="Markdown",
            reply_markup=export_type_keyboard(),
        )
    else:
        await callback.message.edit_text(
            _("🏠 Main menu. Send a receipt photo or use /stats, /budget, /export."),
        )
    await callback.answer()
