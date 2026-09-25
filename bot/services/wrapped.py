"""Year "Wrapped" summary (Feature 2) — one Spotify-Wrapped-style shareable image."""
import io
import logging
from calendar import monthrange
from datetime import date

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import crud
from bot.categories import category_label
from bot.i18n import _, ngettext
from bot.markers import display_name
from bot.services.charts import PALETTE
from bot.utils.formatters import format_day_month, month_name

logger = logging.getLogger(__name__)

_BG = "#F7F5F0"
_CARD = "#FFFFFF"
_TEXT = "#3B3A38"
_ACCENT = "#C7CEEA"


def _card(fig, x, y, w, h):
    fig.patches.append(FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.008,rounding_size=0.02",
        linewidth=0, facecolor=_CARD, transform=fig.transFigure, zorder=1,
    ))


def _label(ax, text, size=11, weight="normal", color=_TEXT, y=0.5):
    ax.text(0.05, y, text, transform=ax.transAxes, fontsize=size,
            fontweight=weight, color=color, va="top")


async def collect_wrapped_stats(session: AsyncSession, user_id: int, year: int | None = None) -> dict | None:
    """Gather all Wrapped stats for the given year (default: current).

    All numbers use a consistent expense-only filter: income and cash
    withdrawals are excluded everywhere so the headline total matches the
    breakdowns.
    """
    year = year or date.today().year
    start, end = date(year, 1, 1), date(year, 12, 31)

    receipts = await crud.get_receipts_by_date_range(session, user_id, start, end)
    purchases = [
        r for r in receipts
        if r.tx_type != "income" and r.tx_type != "cash_withdrawal"
    ]
    total = sum(r.personal_amount() for r in purchases)
    tx_count = len(purchases)
    if total == 0:
        return None

    by_cat_raw = await crud.get_spending_by_category_range(session, user_id, start, end)
    top_category = by_cat_raw[0] if by_cat_raw else None

    priciest = max(purchases, key=lambda r: r.personal_amount(), default=None)

    stores = await crud.get_spending_by_store_range(session, user_id, start, end)
    most_visited = max(stores, key=lambda s: s["visits"], default=None) if stores else None

    # Month with highest spending
    monthly = [0.0] * 12
    for r in purchases:
        if r.date and r.date.year == year:
            monthly[r.date.month - 1] += r.personal_amount()
    top_month_idx = monthly.index(max(monthly)) if max(monthly) > 0 else None

    products = await crud.get_products_stats(
        session, user_id, "all", date_from=start, date_to=end
    )
    products = [
        p for p in products
        if not p["normalized_name"].startswith("кауция")
    ][:10]

    return {
        "year": year,
        "total": total,
        "tx_count": tx_count,
        "top_category": top_category,
        "priciest": priciest,
        "most_visited": most_visited,
        "monthly": monthly,
        "top_month_idx": top_month_idx,
        "products": products[:3],
    }


def render_wrapped_image(stats: dict) -> bytes:
    """Render all Wrapped stats onto a single canvas (matches charts.py style)."""
    year = stats["year"]
    months_short = _short_month_labels()

    # Content occupies y >= 0.24 in figure fractions, so a shorter canvas
    # simply crops the dead band instead of squashing the layout.
    fig = plt.figure(figsize=(9, 10.6), facecolor=_BG)

    def ax_at(x, y, w, h):
        ax = fig.add_axes([x, y, w, h])
        ax.set_zorder(2)          # paint above the card patches
        ax.patch.set_alpha(0.0)   # don't cover them with an opaque bg
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        return ax

    # ── Header ────────────────────────────────────────────────────────────
    header = ax_at(0, 0.93, 1, 0.07)
    header.text(0.5, 0.55, _("YEAR IN REVIEW {year}").format(year=year), ha="center", fontsize=26,
                fontweight="bold", color=_TEXT, transform=header.transAxes)
    header.text(0.5, 0.08, _("FinanceBot · your personal year in numbers"), ha="center",
                fontsize=11, color="#8A8885", transform=header.transAxes)

    # ── Total card ────────────────────────────────────────────────────────
    card_total = ax_at(0, 0.80, 1, 0.12)
    _card(fig, 0.06, 0.815, 0.88, 0.095)
    total_str = f"{stats['total']:,.0f}".replace(",", " ")
    card_total.text(0.5, 0.72, f"{total_str} PLN", ha="center", fontsize=30,
                    fontweight="bold", color=_TEXT, transform=card_total.transAxes)
    tx_count = stats["tx_count"]
    tx_words = ngettext("{n} transaction", "{n} transactions", tx_count).format(n=tx_count)
    card_total.text(0.5, 0.22, _("spent this year · {transactions}").format(transactions=tx_words),
                    ha="center", fontsize=12, color="#8A8885", transform=card_total.transAxes)

    # ── Top category + priciest receipt ───────────────────────────────────
    row1 = ax_at(0, 0.665, 1, 0.12)
    _card(fig, 0.06, 0.68, 0.42, 0.095)
    _card(fig, 0.52, 0.68, 0.42, 0.095)
    cat = stats.get("top_category")
    if cat:
        pct = round(cat["total_pln"] / stats["total"] * 100)
        name = category_label(cat["category"])
        row1.text(0.27, 0.75, _("FAVOURITE CATEGORY"), ha="center", fontsize=9,
                  color="#8A8885", transform=row1.transAxes)
        cat_amt = f"{cat['total_pln']:,.0f}".replace(",", " ")
        row1.text(0.27, 0.48, f"{name} — {cat_amt} PLN",
                  ha="center", fontsize=13, fontweight="bold", color=_TEXT, transform=row1.transAxes)
        row1.text(0.27, 0.20, _("{pct} of all spending").format(pct=f"{pct}%"), ha="center", fontsize=10,
                  color="#8A8885", transform=row1.transAxes)
    else:
        row1.text(0.27, 0.5, _("No categories yet"), ha="center", fontsize=11,
                  color="#8A8885", transform=row1.transAxes)

    priciest = stats.get("priciest")
    if priciest is not None:
        d = priciest.date
        date_str = f"{d.day} {months_short[d.month - 1]}" if d else ""
        store = (display_name(priciest.store) or "?")[:16]
        row1.text(0.73, 0.75, _("MOST EXPENSIVE RECEIPT"), ha="center", fontsize=9,
                  color="#8A8885", transform=row1.transAxes)
        priciest_amt = f"{priciest.personal_amount():,.0f}".replace(",", " ")
        row1.text(0.73, 0.48, f"{priciest_amt} PLN",
                  ha="center", fontsize=13, fontweight="bold", color=_TEXT, transform=row1.transAxes)
        row1.text(0.73, 0.20, f"{store} · {date_str}", ha="center", fontsize=10,
                  color="#8A8885", transform=row1.transAxes)

    # ── Monthly bar chart ─────────────────────────────────────────────────
    row2 = ax_at(0, 0.50, 1, 0.15)
    _card(fig, 0.06, 0.515, 0.88, 0.13)
    row2.text(0.5, 0.92, _("Spending by month"), ha="center", fontsize=11,
              fontweight="bold", color=_TEXT, transform=row2.transAxes)
    bars_ax = fig.add_axes([0.12, 0.535, 0.76, 0.08])
    bars_ax.set_zorder(2)         # paint above the card patch
    bars_ax.patch.set_alpha(0.0)
    colors = ["#AEC6CF"] * 12
    top_m = stats.get("top_month_idx")
    if top_m is not None:
        colors[top_m] = "#F8B4B4"
    bars = bars_ax.bar(range(12), stats["monthly"], color=colors)
    bars_ax.set_xticks(range(12))
    bars_ax.set_xticklabels(months_short, fontsize=8, color="#8A8885")
    bars_ax.set_yticks([])
    for spine in bars_ax.spines.values():
        spine.set_visible(False)

    # ── Most visited store + top products ────────────────────────────────
    row3 = ax_at(0, 0.30, 1, 0.19)
    _card(fig, 0.06, 0.315, 0.88, 0.17)
    visited = stats.get("most_visited")
    if visited:
        visits = visited["visits"]
        visits_str = ngettext("{n} visit", "{n} visits", visits).format(n=visits)
        visited_amt = f"{visited['total_pln']:,.0f}".replace(",", " ")
        row3.text(0.5, 0.88, _("Most visited store: {store} — {visits}, {amount} PLN").format(
                      store=display_name(visited["store"])[:24], visits=visits_str, amount=visited_amt),
                  ha="center", fontsize=11, fontweight="bold", color=_TEXT, transform=row3.transAxes)
    else:
        row3.text(0.5, 0.88, _("No stores yet"), ha="center", fontsize=11,
                  color="#8A8885", transform=row3.transAxes)

    row3.text(0.5, 0.66, _("TOP 3 PRODUCTS OF THE YEAR"), ha="center", fontsize=9,
              color="#8A8885", transform=row3.transAxes)
    products = stats.get("products") or []
    y = 0.45
    for i, p in enumerate(products, 1):
        name = (p["normalized_name"] or "?").title()[:28]
        spent_str = f"{p['total_spent']:,.0f}".replace(",", " ")
        row3.text(0.18, y, f"{i}. {name}", fontsize=11, color=_TEXT, transform=row3.transAxes)
        row3.text(0.82, y, f"{spent_str} PLN", fontsize=11, color=_TEXT,
                  ha="right", transform=row3.transAxes)
        y -= 0.18
    if not products:
        row3.text(0.5, y, _("no product data"), ha="center", fontsize=10,
                  color="#8A8885", transform=row3.transAxes)

    # ── Footer ────────────────────────────────────────────────────────────
    footer = ax_at(0, 0.24, 1, 0.05)
    # ✨ (U+2728) has no glyph in DejaVu Sans — renders as a missing box on the
    # canvas. Use plain text; the caption (Telegram-rendered) keeps its emoji.
    footer.text(0.5, 0.4, _("{year} — thanks for tracking with FinanceBot").format(year=year),
                ha="center", fontsize=12, color="#8A8885", transform=footer.transAxes)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=_BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_wrapped_caption(stats: dict) -> str:
    """Short celebratory caption mirroring the image numbers."""
    year = stats["year"]
    total_str = f"{stats['total']:,.2f}".replace(",", " ")
    tx_count = stats["tx_count"]
    lines = [_("🎉 Your {year} in FinanceBot!").format(year=year),
             ngettext("💸 Spent: *{amount} PLN* in {n} transaction",
                      "💸 Spent: *{amount} PLN* in {n} transactions", tx_count).format(amount=total_str, n=tx_count)]

    cat = stats.get("top_category")
    if cat:
        pct = round(cat["total_pln"] / stats["total"] * 100)
        name = category_label(cat["category"])
        cat_amt = f"{cat['total_pln']:,.0f}".replace(",", " ")
        lines.append(_("🏆 Top category: {name} — {amount} PLN ({pct})").format(name=name, amount=cat_amt, pct=f"{pct}%"))

    priciest = stats.get("priciest")
    if priciest is not None:
        d = priciest.date
        date_part = f", {format_day_month(d)}" if d else ""
        priciest_amt = f"{priciest.personal_amount():,.2f}".replace(",", " ")
        lines.append(_("💎 Most expensive receipt: {amount} PLN — {store}").format(
            amount=priciest_amt, store=display_name(priciest.store) or "?") + date_part)

    visited = stats.get("most_visited")
    if visited:
        visits = visited["visits"]
        lines.append(ngettext("🏪 Your most visited store: {store} ({n} time)",
                              "🏪 Your most visited store: {store} ({n} times)", visits).format(
            store=display_name(visited["store"]), n=visits))

    top_m = stats.get("top_month_idx")
    if top_m is not None:
        m_amount = f"{stats['monthly'][top_m]:,.0f}".replace(",", " ")
        lines.append(_("📈 Most expensive month: {month} — {amount} PLN").format(
            month=month_name(top_m + 1), amount=m_amount))

    lines.append("\n" + _("Share the picture with your year stats 😉"))
    return "\n".join(lines)



def _short_month_labels() -> list[str]:
    """Short month names for the monthly bar chart axis."""
    return [_("Jan"), _("Feb"), _("Mar"), _("Apr"), _("May"), _("Jun"),
            _("Jul"), _("Aug"), _("Sep"), _("Oct"), _("Nov"), _("Dec")]
