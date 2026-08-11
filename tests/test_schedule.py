from datetime import date

import pytest

from ferrycast.config import ConfigError
from ferrycast.schedule import (
    day_tags,
    day_type,
    load_schedule,
    sailings_for_day,
    season,
)


def test_day_type_buckets():
    assert day_type(date(2026, 8, 12)) == "weekday"       # Wednesday
    assert day_type(date(2026, 8, 14)) == "friday"
    assert day_type(date(2026, 8, 15)) == "saturday"
    assert day_type(date(2026, 8, 16)) == "sunday_holiday"


def test_stat_holiday_is_treated_as_sunday_like():
    # Canada Day 2026 falls on a Wednesday but must bucket with Sundays.
    assert date(2026, 7, 1).weekday() == 2
    assert day_type(date(2026, 7, 1)) == "sunday_holiday"


def test_season_buckets():
    assert season(date(2026, 7, 15)) == "peak_summer"
    assert season(date(2026, 6, 25)) == "peak_summer"
    assert season(date(2026, 6, 24)) == "shoulder"
    assert season(date(2026, 5, 1)) == "shoulder"
    assert season(date(2026, 10, 15)) == "shoulder"
    assert season(date(2026, 1, 15)) == "winter"
    assert season(date(2026, 11, 15)) == "winter"


def test_day_tags_flag_long_weekends():
    tags = day_tags(date(2026, 8, 3))
    assert "long_weekend" in tags
    assert any(t.startswith("holiday:") for t in tags)


def test_sailings_for_day_covers_both_directions(config):
    blocks = load_schedule(config.schedule_path)
    destinations = {t.code: t.destination for t in config.route.terminals}
    sailings = sailings_for_day(
        blocks, date(2026, 8, 14), config.route.id, destinations, config.tz
    )
    assert len(sailings) == 6
    assert {s.origin for s in sailings} == {"SLT", "ERL"}
    assert [s.depart_hhmm for s in sailings if s.origin == "ERL"] == ["09:30", "13:30", "15:25"]
    assert all(s.day_type == "friday" for s in sailings)
    assert all(s.scheduled_departure.tzinfo is not None for s in sailings)


def test_sailings_can_be_filtered_by_origin(config):
    blocks = load_schedule(config.schedule_path)
    destinations = {t.code: t.destination for t in config.route.terminals}
    sailings = sailings_for_day(
        blocks, date(2026, 8, 14), config.route.id, destinations, config.tz, origin="SLT"
    )
    assert {s.origin for s in sailings} == {"SLT"}
    assert all(s.destination == "ERL" for s in sailings)


def test_block_outside_its_date_range_contributes_nothing(config, tmp_path):
    path = tmp_path / "sched.toml"
    path.write_text(
        """
[[block]]
terminal = "SLT"
effective_from = 2026-06-25
effective_to = 2026-09-08
days = ["sat"]
departures = ["22:15"]
"""
    )
    blocks = load_schedule(path)
    destinations = {"SLT": "ERL"}
    # Right season, wrong weekday.
    assert not sailings_for_day(
        blocks, date(2026, 7, 1), "route7", destinations, config.tz
    )
    # Right weekday, outside the date range.
    assert not sailings_for_day(
        blocks, date(2026, 3, 7), "route7", destinations, config.tz
    )
    assert sailings_for_day(blocks, date(2026, 7, 4), "route7", destinations, config.tz)


def test_missing_schedule_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="no schedule"):
        load_schedule(tmp_path / "absent.toml")


# ---- Where summer ends ------------------------------------------------------------------


def test_summer_ends_on_the_first_day_of_school():
    """BC schools open the Tuesday after Labour Day. 2026's first day is September 8."""
    from ferrycast.schedule import summer_ends

    assert summer_ends(2026) == date(2026, 9, 8)
    assert summer_ends(2026).strftime("%A") == "Tuesday"


def test_the_boundary_tracks_labour_day_rather_than_a_fixed_date():
    """A hardcoded 'September 8' is right for 2026 and wrong every year after."""
    from ferrycast.schedule import summer_ends

    assert summer_ends(2027) == date(2027, 9, 7)
    assert summer_ends(2028) == date(2028, 9, 5)


def test_labour_day_itself_is_still_peak_summer():
    """The last big travel day of the summer, and the busiest sailing of the weekend."""
    assert season(date(2026, 9, 7)) == "peak_summer"


def test_the_first_day_of_school_is_not_peak_summer():
    """The whole point: the traffic stops when school starts, not on an arbitrary date."""
    assert season(date(2026, 9, 8)) == "shoulder"


def test_a_september_sailing_is_not_compared_with_an_august_one(conn, config):
    """Before and after school starts are different worlds; the season split has to hold."""
    assert season(date(2026, 8, 20)) != season(date(2026, 9, 10))
