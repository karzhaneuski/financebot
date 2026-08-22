from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.forecast import format_forecast_line, get_month_forecast


@pytest.mark.asyncio
async def test_get_month_forecast_projects_run_rate():
    fake_today = datetime(2026, 8, 23)  # 23rd of a 31-day month -> 23 days elapsed
    mock_datetime = MagicMock(wraps=datetime)
    mock_datetime.utcnow.return_value = fake_today

    with patch("bot.services.forecast.datetime", mock_datetime), \
         patch("bot.services.forecast.crud.get_expenses_total_range", AsyncMock(return_value=230.0)):
        forecast = await get_month_forecast(AsyncMock(), user_id=1)

    assert forecast["spent_so_far"] == 230.0
    assert forecast["days_elapsed"] == 23
    assert forecast["days_in_month"] == 31
    assert forecast["projected_total"] == pytest.approx((230.0 / 23) * 31)


def test_format_forecast_line_skips_early_month():
    forecast = {"spent_so_far": 50.0, "days_elapsed": 2, "days_in_month": 30, "projected_total": 750.0}
    assert format_forecast_line(forecast) is None


def test_format_forecast_line_formats_with_thousand_separator():
    forecast = {"spent_so_far": 3250.0, "days_elapsed": 23, "days_in_month": 30, "projected_total": 4250.0}
    line = format_forecast_line(forecast)
    assert line == "📈 Прогноз на конец месяца: ~4 250 PLN (при текущем темпе трат)"


def test_format_forecast_line_rounds_projected_total():
    forecast = {"spent_so_far": 10.0, "days_elapsed": 3, "days_in_month": 30, "projected_total": 99.6}
    line = format_forecast_line(forecast)
    assert line == "📈 Прогноз на конец месяца: ~100 PLN (при текущем темпе трат)"
