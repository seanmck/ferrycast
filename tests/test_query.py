from datetime import date, timedelta

from ferrycast.holidays import is_long_weekend
from ferrycast.query import (
    arrival_curve,
    default_sailing_time,
    query_distribution,
    sailing_times,
    upcoming_sailings,
)
from ferrycast.schedule import day_type, season
from ferrycast.timeutil import combine_local, parse_hhmm

from .conftest import add_observation


def seed_record(conn, config, service_date: date, hhmm: str, outcome: str, origin="SLT", **kw):
    departure = combine_local(service_date, parse_hhmm(hhmm), config.tz)
    cur = conn.execute(
        """INSERT OR IGNORE INTO sailings
               (route, origin, destination, service_date, scheduled_departure,
                depart_hhmm, day_type, season)
           VALUES (?, ?, 'ERL', ?, ?, ?, ?, ?)""",
        (
            config.route.id,
            origin,
            service_date.isoformat(),
            departure.isoformat(),
            hhmm,
            day_type(service_date),
            season(service_date),
        ),
    )
    # Always look the id up. On INSERT OR IGNORE that ignores, `cur.lastrowid` still holds
    # the *previous* successful insert's rowid, so `cur.lastrowid or ...` silently attaches
    # the record to whichever sailing was inserted last — invisible until a test seeds more
    # than one sailing before recording against an earlier one.
    del cur
    sailing_id = conn.execute(
        "SELECT id FROM sailings WHERE origin = ? AND scheduled_departure = ?",
        (origin, departure.isoformat()),
    ).fetchone()["id"]
    conn.execute(
        """INSERT OR REPLACE INTO sailing_records
               (sailing_id, outcome, peak_queue, carryover, n_frames, confidence,
                queue_truncated, computed_at)
           VALUES (?, ?, ?, ?, 8, 0.9, 0, '2026-01-01T00:00:00Z')""",
        (sailing_id, outcome, kw.get("peak_queue", 50), kw.get("carryover")),
    )
    conn.commit()
    return sailing_id


# A Friday with no long weekend attached. 2026-07-31 looks like an ordinary Friday but sits
# against BC Day, so it is deliberately NOT comparable to plain Fridays — using it as a test
# target silently exercises the relaxation ladder instead of the exact match.
PLAIN_FRIDAY = date(2026, 8, 14)


def fridays(count: int, *, start: date = date(2026, 7, 3)):
    """Consecutive Fridays inside the peak-summer bucket."""
    return [start + timedelta(days=7 * i) for i in range(count)]


def test_distribution_counts_and_shares(conn, config):
    days = fridays(4)
    for day, outcome in zip(days, ["boarded", "waited_1", "waited_1", "waited_2plus"], strict=True):
        seed_record(conn, config, day, "12:30", outcome)

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.n == 4
    assert result.counts == {
        "boarded": 1,
        "filled": 0,
        "waited_1": 2,
        "waited_2plus": 1,
        "cancelled": 0,
    }
    assert result.shares["waited_1"] == 0.5
    assert result.match_level == "exact"
    assert result.relaxations == []
    assert result.sufficient is True


def test_samples_carry_the_underlying_dates(conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1", peak_queue=88, carryover=30)

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert len(result.samples) == 3
    assert {s.service_date for s in result.samples} == {d.isoformat() for d in fridays(3)}
    assert result.samples[0].peak_queue == 88
    assert result.samples[0].carryover == 30


def test_target_day_itself_is_excluded_from_its_own_history(conn, config):
    target = PLAIN_FRIDAY
    seed_record(conn, config, target, "12:30", "boarded")
    result = query_distribution(
        conn, config, origin="SLT", target_date=target, depart_hhmm="12:30"
    )
    assert result.n == 0


def test_only_matching_day_types_are_comparable(conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1")
    # Wednesdays in the same weeks — same time, different day type.
    for day in fridays(3):
        seed_record(conn, config, day - timedelta(days=2), "12:30", "boarded")

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )
    assert result.n == 3
    assert result.counts["waited_1"] == 3


def test_direction_matters(conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1", origin="SLT")
    result = query_distribution(
        conn, config, origin="ERL", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )
    assert result.n == 0


def test_search_widens_when_the_exact_bucket_is_thin(conn, config):
    # Only two peak-summer Fridays, below the min_sample of 3; winter Fridays fill in.
    for day in fridays(2):
        seed_record(conn, config, day, "12:30", "waited_1")
    for day in fridays(3, start=date(2026, 1, 2)):
        seed_record(conn, config, day, "12:30", "boarded")

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.n == 5
    assert result.match_level == "any_season"
    assert result.relaxations  # the UI must be able to say the bucket was widened


def test_widening_reports_every_step_it_took(conn, config):
    # Nothing at 12:30 at all; only a 13:30 Friday sailing exists.
    for day in fridays(3):
        seed_record(conn, config, day, "13:30", "boarded")

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert result.n == 3
    assert result.match_level == "wider_time"
    assert len(result.relaxations) == 2


def test_thin_result_is_marked_insufficient(conn, config):
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")
    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )
    assert result.n == 1
    assert result.sufficient is False


def test_unknown_outcomes_are_excluded_but_counted(conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "boarded")
    seed_record(conn, config, date(2026, 7, 24), "12:30", "unknown")

    result = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )
    assert result.n == 3
    assert result.unknown_excluded == 1


def test_holiday_is_compared_against_sundays(conn, config):
    # Canada Day 2026 is a Wednesday but buckets as sunday_holiday.
    for day in [date(2026, 6, 7), date(2026, 6, 14), date(2026, 6, 21)]:
        seed_record(conn, config, day, "12:30", "waited_2plus")

    result = query_distribution(
        conn, config, origin="SLT", target_date=date(2026, 7, 1), depart_hhmm="12:30"
    )
    assert result.day_type == "sunday_holiday"
    assert result.n == 3
    assert result.tags  # the holiday name is surfaced to the UI


def test_sailing_times_come_from_the_schedule(config):
    assert sailing_times(config, "SLT", date(2026, 8, 14)) == ["08:30", "12:30", "16:30"]
    assert sailing_times(config, "ERL", date(2026, 8, 14)) == ["09:30", "13:30", "15:25"]


def test_upcoming_sailings_start_from_the_reference_time(config):
    from datetime import datetime

    reference = datetime(2026, 8, 14, 12, 0, tzinfo=config.tz)
    upcoming = upcoming_sailings(config, at=reference, limit=3)
    assert [s.depart_hhmm for s in upcoming] == ["12:30", "13:30", "15:25"]


def test_upcoming_sailings_roll_into_the_next_day(config):
    from datetime import datetime

    reference = datetime(2026, 8, 14, 23, 0, tzinfo=config.tz)
    upcoming = upcoming_sailings(config, at=reference, limit=1)
    assert upcoming[0].service_date == date(2026, 8, 15)


def test_default_sailing_time_skips_departures_that_have_gone(config):
    from datetime import datetime

    times = sailing_times(config, "SLT", date(2026, 8, 14))
    assert times == ["08:30", "12:30", "16:30"]

    at = datetime(2026, 8, 14, 9, 0, tzinfo=config.tz)
    assert default_sailing_time(config, times, date(2026, 8, 14), at=at) == "12:30"


def test_default_sailing_time_takes_the_last_once_the_day_is_over(config):
    from datetime import datetime

    times = sailing_times(config, "SLT", date(2026, 8, 14))
    at = datetime(2026, 8, 14, 23, 0, tzinfo=config.tz)
    # Nothing is still to come, so the nearest sailing is the one that just went — not the
    # 08:30, which is the furthest point of the day from now.
    assert default_sailing_time(config, times, date(2026, 8, 14), at=at) == "16:30"


def test_default_sailing_time_starts_at_the_top_of_any_other_day(config):
    from datetime import datetime

    at = datetime(2026, 8, 14, 23, 0, tzinfo=config.tz)
    for day in (date(2026, 8, 15), date(2026, 8, 13)):
        times = sailing_times(config, "SLT", day)
        assert default_sailing_time(config, times, day, at=at) == "08:30"


def test_default_sailing_time_is_none_when_nothing_is_scheduled(config):
    assert default_sailing_time(config, [], date(2026, 8, 14)) is None


def test_arrival_curve_buckets_by_minutes_before_departure(conn, config):
    for day in fridays(3):
        seed_record(conn, config, day, "12:30", "waited_1")
        departure = combine_local(day, parse_hhmm("12:30"), config.tz)
        for minutes, count in [(90, 10), (60, 25), (30, 60), (15, 85)]:
            add_observation(
                conn, config, "SLT", departure - timedelta(minutes=minutes), count
            )

    curve = arrival_curve(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert curve["sailings"] == 3
    buckets = {p["minutes_before"]: p for p in curve["points"]}
    assert buckets[90]["median"] == 10
    assert buckets[15]["median"] == 85
    assert buckets[15]["n"] == 3
    # Points run from furthest-out down to departure, which is how the chart reads.
    assert [p["minutes_before"] for p in curve["points"]] == sorted(
        buckets, reverse=True
    )


# ---- Likeness of days: a long weekend is not an ordinary one ----------------------------
#
# A stat Monday already buckets as sunday_holiday, so it never pooled with ordinary Mondays.
# The days *around* it did: the Friday before BC Day looked like any other Friday to the
# schedule, and pooled with them. It is one of the busiest Fridays of the year.

LONG_WEEKEND_FRIDAY = date(2026, 7, 31)   # BC Day falls on the following Monday


def test_a_long_weekend_friday_does_not_match_ordinary_fridays(conn, config):
    for day in fridays(6):
        seed_record(conn, config, day, "12:30", "boarded")

    d = query_distribution(
        conn, config, origin="SLT", target_date=LONG_WEEKEND_FRIDAY, depart_hhmm="12:30"
    )
    assert d.match_level != "exact"
    assert any("long weekend" in r for r in d.relaxations), d.relaxations


def test_long_weekend_fridays_match_each_other(conn, config):
    """Victoria Day, Labour Day and Thanksgiving Fridays. NB 2026-06-26 is *not* one —
    Canada Day 2026 falls on a Wednesday — which is exactly the sort of thing worth
    computing rather than eyeballing."""
    for day in (date(2026, 5, 15), date(2026, 9, 4), date(2026, 10, 9)):
        assert is_long_weekend(day), day
        seed_record(conn, config, day, "12:30", "filled")

    d = query_distribution(
        conn, config, origin="SLT", target_date=LONG_WEEKEND_FRIDAY, depart_hhmm="12:30"
    )
    assert d.n >= 1
    assert all("long weekend" not in r for r in d.relaxations), d.relaxations


def test_an_ordinary_friday_is_not_polluted_by_long_weekend_ones(conn, config):
    """The failure this prevents: a busy long-weekend Friday inflating every other Friday."""
    for day in fridays(5):
        seed_record(conn, config, day, "12:30", "boarded")
    seed_record(conn, config, LONG_WEEKEND_FRIDAY, "12:30", "filled")

    d = query_distribution(conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30")
    assert d.match_level == "exact"
    assert d.counts["filled"] == 0, "the long-weekend Friday should not be in this sample"


# ---- Recency cap ------------------------------------------------------------------------


def test_only_the_most_recent_sailings_count(conn, config):
    """Without a cap the distribution accumulates forever, and a sailing from two timetables
    ago weighs as much as last week's — ageing quietly while still looking well-evidenced."""
    plain = [d for d in fridays(20) if not is_long_weekend(d)]
    old, recent = plain[:-3], plain[-3:]
    for day in old:
        seed_record(conn, config, day, "12:30", "filled")
    for day in recent:
        seed_record(conn, config, day, "12:30", "boarded")

    target = recent[-1] + timedelta(days=7)
    assert not is_long_weekend(target)
    d = query_distribution(conn, config, origin="SLT", target_date=target, depart_hhmm="12:30")

    assert d.n == config.query.max_samples
    assert len(d.samples) == config.query.max_samples
    # All three recent 'boarded' sailings are in; the older 'filled' ones are crowded out.
    assert d.counts["boarded"] == 3


def test_the_cap_applies_to_the_counts_not_just_the_listed_dates(conn, config):
    """A distribution that says "5 sailings" must be computed from those five and no others."""
    for day in fridays(20):
        seed_record(conn, config, day, "12:30", "boarded")
    d = query_distribution(conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30")
    assert d.n == len(d.samples) == config.query.max_samples
