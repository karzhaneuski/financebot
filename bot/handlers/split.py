import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.i18n import _
from bot.markers import display_name
from bot.services.wrapped import build_wrapped_caption, collect_wrapped_stats, render_wrapped_image

logger = logging.getLogger(__name__)
router = Router()

_SPLIT_KEY = "split:{uid}:{rid}"
_SPLIT_TTL = 1800  # generous window for working through a long receipt


@router.message(Command("wrapped"))
async def cmd_wrapped(message: Message, session: AsyncSession) -> None:
    stats = await collect_wrapped_stats(session, message.from_user.id)
    if stats is None:
        await message.answer(
            _("No spending this year yet 🤷 Add receipts — and get your summary at the end of the year!")
        )
        return

    try:
        image = render_wrapped_image(stats)
    except Exception:
        logger.exception("Failed to render wrapped image")
        await message.answer(_("❌ Couldn't build the picture. Please try later."))
        return

    await message.answer_photo(
        BufferedInputFile(image, filename=f"wrapped_{stats['year']}.png"),
        caption=build_wrapped_caption(stats),
        parse_mode="Markdown",
    )


# ── /split ────────────────────────────────────────────────────────────────────

def _receipt_label(r) -> str:
    date_str = r.date.strftime("%d.%m") if r.date else "?"
    store = (display_name(r.store) or "?")[:20]
    return f"{date_str} · {store} · {r.personal_amount():.2f} PLN"


async def _show_receipt_picker(message: Message, session: AsyncSession, user_id: int) -> None:
    receipts = await crud.get_recent_receipts_with_items(session, user_id, limit=10)
    if not receipts:
        await message.answer(_("No receipts yet — nothing to split 🙂"))
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for r in receipts:
        builder.button(text=_receipt_label(r), callback_data=f"splitpick:{r.id}")
    builder.adjust(1)
    await message.answer(_("Choose the receipt you want to split:"), reply_markup=builder.as_markup())


@router.message(Command("split"))
async def cmd_split(message: Message, session: AsyncSession, redis) -> None:
    await _show_receipt_picker(message, session, message.from_user.id)


@router.callback_query(F.data.startswith("splitpick:"))
async def split_pick_callback(call: CallbackQuery, session: AsyncSession, redis) -> None:
    receipt_id = int(call.data.split(":")[1])
    await call.answer()
    receipt = await crud.get_receipt_by_id(session, receipt_id)
    if receipt is None or receipt.user_id != call.from_user.id:
        await call.message.answer(_("⚠️ Receipt not found."))
        return
    if not receipt.items:
        await call.message.answer(_("This receipt has no items — it can't be split."))
        return
    if receipt.total_pln == 0:
        await call.message.answer(_("This receipt's total is zero — splitting makes no sense."))
        return

    # Preload toggle state from persisted is_personal flags so re-opening a
    # previously split receipt shows the saved selection instead of resetting
    # everything to "mine". NULL flag = not yet split = counts as "mine".
    mine = [it.id for it in receipt.items if it.is_personal is not False]
    await redis.set(
        _SPLIT_KEY.format(uid=call.from_user.id, rid=receipt_id),
        json.dumps({"mine": mine}),
        ex=_SPLIT_TTL,
    )
    await _render_split_state(call.message, session, redis, call.from_user.id, receipt_id)


def _personal_pln_preview(receipt, mine: set[int]) -> float:
    """PLN value of the currently-marked items using the receipt's own rate."""
    my_native = sum(float(it.total_price) for it in receipt.items if it.id in mine)
    native_total = float(receipt.total)
    total_pln = float(receipt.total_pln)
    if native_total > 0:
        return round(max(0.0, min(1.0, my_native / native_total)) * total_pln, 2)
    return total_pln


async def _render_split_state(target_message, session: AsyncSession, redis, user_id: int, receipt_id: int) -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    raw = await redis.get(_SPLIT_KEY.format(uid=user_id, rid=receipt_id))
    if not raw:
        await target_message.answer(_("⚠️ The session has expired. Run /split again."))
        return
    state = json.loads(raw)
    mine: set[int] = set(state["mine"])

    receipt = await crud.get_receipt_by_id(session, receipt_id)
    if receipt is None:
        await target_message.answer(_("⚠️ Receipt not found."))
        return

    builder = InlineKeyboardBuilder()
    for it in receipt.items:
        is_mine = it.id in mine
        label = ("✅" if is_mine else "❌") + f" {display_name(it.name)[:24]} · {float(it.total_price):.2f}"
        action = "un" if is_mine else "on"
        builder.button(text=label, callback_data=f"splittog:{receipt_id}:{it.id}:{action}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(
            text=_("✅ Done"),
            callback_data=f"splitdone:{receipt_id}",
        )
    )

    # Live preview of the personal PLN share (receipt's own baked-in rate),
    # not a raw native-currency sum mislabeled as PLN.
    personal_pln = _personal_pln_preview(receipt, mine)
    text = (
        _("Mark what was bought *for you* (receipt of {total} {currency} ≈ {total_pln} PLN):\n\n"
          "Yours right now: *{personal} PLN*").format(
            total=f"{float(receipt.total):.2f}", currency=receipt.currency,
            total_pln=f"{float(receipt.total_pln):.2f}", personal=f"{personal_pln:.2f}",
        )
    )
    try:
        await target_message.edit_text(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    except Exception:
        logger.exception("split render failed")
        await target_message.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("splittog:"))
async def split_toggle_callback(call: CallbackQuery, session: AsyncSession, redis) -> None:
    _prefix, rid_str, item_id_str, action = call.data.split(":")
    receipt_id, item_id = int(rid_str), int(item_id_str)
    await call.answer()

    raw = await redis.get(_SPLIT_KEY.format(uid=call.from_user.id, rid=receipt_id))
    if not raw:
        await call.message.answer(_("⚠️ The session has expired. Run /split again."))
        return
    state = json.loads(raw)
    mine: set[int] = set(state["mine"])
    if action == "on":
        mine.add(item_id)
    else:
        mine.discard(item_id)
    state["mine"] = sorted(mine)
    # Refresh TTL on every toggle so a slow user doesn't lose progress.
    await redis.set(
        _SPLIT_KEY.format(uid=call.from_user.id, rid=receipt_id),
        json.dumps(state),
        ex=_SPLIT_TTL,
    )
    await _render_split_state(call.message, session, redis, call.from_user.id, receipt_id)


@router.callback_query(F.data.startswith("splitdone:"))
async def split_done_callback(call: CallbackQuery, session: AsyncSession, redis) -> None:
    receipt_id = int(call.data.split(":")[1])
    await call.answer()

    raw = await redis.get(_SPLIT_KEY.format(uid=call.from_user.id, rid=receipt_id))
    if not raw:
        await call.message.answer(_("⚠️ The session has expired. Run /split again."))
        return
    mine: set[int] = set(json.loads(raw)["mine"])

    receipt = await crud.get_receipt_by_id(session, receipt_id)
    if receipt is None or receipt.user_id != call.from_user.id:
        await call.message.answer(_("⚠️ Receipt not found."))
        return

    old_personal = receipt.personal_amount()
    # set_item_personal_flags persists per-item flags AND recomputes
    # Receipt.personal_total_pln from them (receipt's own baked-in rate).
    await crud.set_item_personal_flags(session, receipt_id, sorted(mine))
    await session.commit()
    await redis.delete(_SPLIT_KEY.format(uid=call.from_user.id, rid=receipt_id))

    personal_pln = float(receipt.personal_total_pln or 0.0)

    await call.message.edit_text(
        _("✅ Updated: statistics will now count *{new} PLN* instead of {old} PLN").format(
            new=f"{personal_pln:.2f}", old=f"{old_personal:.2f}"
        ),
        parse_mode="Markdown",
    )
