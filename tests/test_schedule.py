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
