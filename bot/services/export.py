"""Excel (.xlsx) export: the same rows as the CSV export (bot/handlers/export.py),
as real dates and numbers with localized headers and category names."""
import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from bot.i18n import _
from bot.markers import display_name
from bot.utils.formatters import format_category

_HEADER_FILL = PatternFill(start_color="AEC6CF", end_color="AEC6CF", fill_type="solid")
_HEADER_FONT = Font(bold=True)
_DATE_FORMAT = "DD.MM.YYYY"
_MONEY_FORMAT = "#,##0.00"


def _add_sheet(wb: Workbook, title: str, headers: list[str], rows: list[list],
               date_cols: set[int], money_cols: set[int]) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    for row in rows:
        ws.append(row)
        for idx in date_cols:
            ws.cell(ws.max_row, idx + 1).number_format = _DATE_FORMAT
        for idx in money_cols:
            ws.cell(ws.max_row, idx + 1).number_format = _MONEY_FORMAT
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col_cells in ws.columns:
        longest = max((len(str(cell.value)) for cell in col_cells if cell.value is not None), default=8)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(longest, 10) + 2, 45)


def _receipt_category(r) -> str:
    key = r.category.value if r.category else (r.items[0].category.value if r.items else "other")
    return format_category(key)


def build_excel(receipts: list | None = None, item_rows: list | None = None) -> bytes:
    """Workbook with a "Transactions" sheet (receipts from
    crud.get_transactions_for_export) and/or an "Items" sheet (rows from
    crud.get_items_for_export); a sheet is left out when its data is None."""
    wb = Workbook()
    wb.remove(wb.active)

    if receipts is not None:
        _add_sheet(
            wb, _("Transactions"),
            [_("Date"), _("Store"), _("Total"), _("Currency"), _("Total PLN"), _("Category")],
            [[r.date, display_name(r.store) or "", float(r.total), r.currency or "PLN",
              float(r.total_pln), _receipt_category(r)] for r in receipts],
            date_cols={0}, money_cols={2, 4},
        )
    if item_rows is not None:
        _add_sheet(
            wb, _("Items"),
            [_("Date"), _("Store"), _("Item"), _("Qty"), _("Price"), _("Total"), _("Currency"),
             _("Total PLN"), _("Category")],
            [[row.date, display_name(row.store) or "", display_name(row.name), float(row.quantity),
              float(row.unit_price) if row.unit_price is not None else None, float(row.total_price),
              row.currency, round(float(row.total_pln), 2), format_category(row.category.value)]
             for row in item_rows],
            date_cols={0}, money_cols={4, 5, 7},
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
