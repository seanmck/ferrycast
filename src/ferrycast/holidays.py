"""British Columbia statutory holidays.

The PRD maps BC stat holidays to "Sunday-like" for day-type bucketing. Computing them
locally avoids a dependency and a network call; the rules below are stable.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

BC_HOLIDAY_NAMES = (
    "New Year's Day",
    "Family Day",
    "Good Friday",
    "Victoria Day",
    "Canada Day",
    "BC Day",
    "Labour Day",
    "National Day for Truth and Reconciliation",
    "Thanksgiving",
    "Remembrance Day",
    "Christmas Day",
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) `weekday` of a month. Monday == 0."""
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_before(limit: date, weekday: int) -> date:
    """The latest `weekday` strictly before `limit`."""
    offset = (limit.weekday() - weekday) % 7
    return limit - timedelta(days=offset or 7)


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def bc_holidays(year: int) -> dict[date, str]:
    """Statutory holidays observed in British Columbia for a calendar year."""
    return {
        date(year, 1, 1): "New Year's Day",
        _nth_weekday(year, 2, 0, 3): "Family Day",
        easter_sunday(year) - timedelta(days=2): "Good Friday",
        _last_weekday_before(date(year, 5, 25), 0): "Victoria Day",
        date(year, 7, 1): "Canada Day",
        _nth_weekday(year, 8, 0, 1): "BC Day",
        _nth_weekday(year, 9, 0, 1): "Labour Day",
        date(year, 9, 30): "National Day for Truth and Reconciliation",
        _nth_weekday(year, 10, 0, 2): "Thanksgiving",
        date(year, 11, 11): "Remembrance Day",
        date(year, 12, 25): "Christmas Day",
    }


def holiday_name(day: date) -> str | None:
    return bc_holidays(day.year).get(day)


def is_holiday(day: date) -> bool:
    return day in bc_holidays(day.year)


def is_long_weekend(day: date) -> bool:
    """True if `day` falls in a Sat-Mon long weekend created by a stat holiday."""
    if is_holiday(day):
        return True
    weekday = day.weekday()
    if weekday == 5:  # Saturday — Monday holiday makes it a long weekend
        return is_holiday(day + timedelta(days=2))
    if weekday == 6:  # Sunday
        return is_holiday(day + timedelta(days=1)) or is_holiday(day - timedelta(days=2))
    if weekday == 4:  # Friday — a Good Friday sits on the Friday itself
        return is_holiday(day + timedelta(days=3))
    return False
