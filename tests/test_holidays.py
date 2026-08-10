from datetime import date

from ferrycast.holidays import bc_holidays, easter_sunday, holiday_name, is_holiday, is_long_weekend


def test_easter_matches_known_dates():
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2024) == date(2024, 3, 31)


def test_good_friday_is_two_days_before_easter():
    assert holiday_name(date(2026, 4, 3)) == "Good Friday"


def test_floating_holidays_land_on_mondays():
    holidays = bc_holidays(2026)
    assert holidays[date(2026, 2, 16)] == "Family Day"        # 3rd Monday of February
    assert holidays[date(2026, 5, 18)] == "Victoria Day"      # Monday before May 25
    assert holidays[date(2026, 8, 3)] == "BC Day"             # 1st Monday of August
    assert holidays[date(2026, 9, 7)] == "Labour Day"         # 1st Monday of September
    assert holidays[date(2026, 10, 12)] == "Thanksgiving"     # 2nd Monday of October


def test_victoria_day_never_falls_on_may_25():
    for year in range(2024, 2035):
        victoria = next(d for d, name in bc_holidays(year).items() if name == "Victoria Day")
        assert victoria.weekday() == 0
        assert victoria < date(year, 5, 25)


def test_fixed_date_holidays():
    assert is_holiday(date(2026, 7, 1))    # Canada Day
    assert is_holiday(date(2026, 9, 30))   # Truth and Reconciliation
    assert not is_holiday(date(2026, 7, 2))


def test_long_weekend_detection():
    # BC Day 2026 is Monday 3 August, so the preceding weekend is long.
    assert is_long_weekend(date(2026, 8, 1))
    assert is_long_weekend(date(2026, 8, 2))
    assert is_long_weekend(date(2026, 8, 3))
    assert not is_long_weekend(date(2026, 8, 8))
