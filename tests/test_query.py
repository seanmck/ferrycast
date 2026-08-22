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
    # The axes are left NULL unless a test asks for them, which is also the shape of every
    # record written before they existed — so the fallback that reads them back out of the
    # outcome word is exercised by default rather than only where it is named.
    conn.execute(
        """INSERT OR REPLACE INTO sailing_records
               (sailing_id, outcome, filled, left_behind, left_full, method,
                peak_queue, carryover, n_frames, confidence,
                queue_truncated, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 8, 0.9, 0, '2026-01-01T00:00:00Z')""",
        (
            sailing_id,
            outcome,
            kw.get("filled"),
            kw.get("left_behind"),
            kw.get("left_full"),
            kw.get("method"),
            kw.get("peak_queue", 50),
            kw.get("carryover"),
        ),
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


# ---- Two axes, one pool -----------------------------------------------------------------
#
# The board's account of the deck used to be tallied separately, over its own set of
# sailings with its own sufficiency rule, so the page carried two denominators for one
# question. These pin the merged pool: the sailing axis sums to n, and the person axis sums
# to the filled slice it decomposes.


def test_the_sailing_axis_sums_to_the_sample(conn, config):
    days = fridays(4)
    seed_record(conn, config, days[0], "12:30", "boarded", filled=0, left_behind=0)
    seed_record(conn, config, days[1], "12:30", "filled", filled=1)
    seed_record(conn, config, days[2], "12:30", "filled", filled=1, left_behind=0)
    seed_record(conn, config, days[3], "12:30", "cancelled")

    d = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert d.n == 4
    assert d.had_space + d.filled + d.cancelled == d.n
    assert (d.had_space, d.filled, d.cancelled) == (1, 2, 1)
    assert d.filled_share == 0.5


def test_the_person_axis_sums_to_the_filled_slice(conn, config):
    days = fridays(4)
    seed_record(conn, config, days[0], "12:30", "waited_1", filled=1, left_behind=1)
    seed_record(conn, config, days[1], "12:30", "filled", filled=1, left_behind=0)
    # Ran out of room, and nobody has said whether that cost anyone their crossing.
    seed_record(conn, config, days[2], "12:30", "filled", filled=1)
    seed_record(conn, config, days[3], "12:30", "boarded", filled=0, left_behind=0)

    d = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert d.filled == 3
    assert d.left_behind + d.took_everyone + d.unresolved == d.filled
    assert (d.left_behind, d.took_everyone, d.unresolved) == (1, 1, 1)


def test_a_board_only_sailing_with_space_near_departure_counts_as_had_space(conn, config):
    """The whole point of pooling. The board is the fast-accruing source, so its readings
    widen this denominator instead of living in a card beside it — under the same
    sufficiency rule as everything else, which is what the separate tally never used."""
    from ferrycast.aggregate import aggregate_day

    from .test_deckspace_history import add_deck_space

    for day in fridays(4):
        # Scraped through to ten minutes before departure and never short of room.
        add_deck_space(conn, config, "SLT", day, "12:30", [(90, 60), (40, 35), (10, 20)])
        aggregate_day(conn, config, day)

    d = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert d.n == 4
    assert d.had_space == 4
    assert d.filled_share == 0.0
    assert d.board_reported == 4


def test_a_feed_that_stopped_early_asserts_nothing(conn, config):
    """The looser rule the capacity tally used: any reading at all counted as "the board
    spoke", so a series that went quiet an hour out was published as a comfortable crossing.
    One rule now, and it is the strict one."""
    from ferrycast.aggregate import aggregate_day

    from .test_deckspace_history import add_deck_space

    for day in fridays(4):
        add_deck_space(conn, config, "SLT", day, "12:30", [(180, 60), (120, 45)])
        aggregate_day(conn, config, day)

    d = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert d.n == 0


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


def test_bands_and_transitions_reach_the_query_layer(conn, config):
    """The archive stores fullness; the page has to be able to show it.

    Guards the seam between aggregation and display: a band that lands in `sailing_records`
    but never reaches a `ComparableSailing` is stored history nobody can read.
    """
    from ferrycast.aggregate import aggregate_day

    from .test_aggregate import _band_frames

    for day in fridays(4):
        departure = combine_local(day, parse_hhmm("12:30"), config.tz)
        _band_frames(conn, config, departure, [(-45, "light"), (-15, "heavy"), (20, "empty")])
        aggregate_day(conn, config, day)

    dist = query_distribution(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    sample = dist.samples[0]
    assert sample.outcome == "boarded"
    assert sample.peak_fullness == "heavy"
    # No count is claimed, because v2 does not ask for one.
    assert sample.peak_queue is None
    # Transitions are surfaced as local clock times, which is how a traveller reads them.
    assert sample.queue_started_local == "11:45"
    assert sample.cleared_local == "12:50"


def test_the_arrival_curve_is_drawn_from_lanes_in_use(conn, config):
    """The one feature built to answer "when do I need to arrive" returned nothing on this
    route: its frame source wanted vehicle counts, which the extractors stopped producing on
    purpose, and its fallback wanted a deck-space percentage this route has never published
    once. Lanes are ordinal, comparable across days, and free."""
    from ferrycast.aggregate import aggregate_day

    from .test_aggregate import _geom_frames

    for day in fridays(4):
        departure = combine_local(day, parse_hhmm("12:30"), config.tz)
        _geom_frames(
            conn, config, departure,
            [(-60, 1, "light"), (-30, 5, "moderate"), (-15, 9, "heavy"), (20, 0, "empty")],
        )
        aggregate_day(conn, config, day)

    curve = arrival_curve(
        conn, config, origin="SLT", target_date=PLAIN_FRIDAY, depart_hhmm="12:30"
    )

    assert curve["source"] == "lanes"
    assert curve["unit"] == "lanes in use"
    by_minute = {p["minutes_before"]: p["median"] for p in curve["points"]}
    # The build is the point: a reader should see how late the compound was still open.
    assert by_minute[60] < by_minute[30] < by_minute[15]
    assert all(p["n"] == 4 for p in curve["points"])


# ---- Where the previous sailing left ----------------------------------------------------
#
# The lookback window is two hours; Route 7's headways run from 110 minutes to nearly three.
# So the left-hand end of the arrival curve often sits on the sailing *before* the one being
# asked about. The queue falls there because that ferry loaded and left, not because this
# one's line thinned, and a chart that does not say so reads as though arriving earlier
# finds a shorter wait.


def _config_with_schedule(tmp_path, schedule: str):
    from ferrycast.config import load_config

    from .conftest import CONFIG_TEMPLATE

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(CONFIG_TEMPLATE)
    (config_dir / "schedule.toml").write_text(schedule)
    return load_config(config_dir / "ferrycast.toml")


def _seeded_curve(conn, config, hhmm: str, origin: str = "SLT", days=None):
    for day in days or fridays(3):
        seed_record(conn, config, day, hhmm, "boarded", origin=origin)
    return arrival_curve(
        conn, config, origin=origin, target_date=PLAIN_FRIDAY, depart_hhmm=hhmm
    )


def test_the_curve_says_where_the_previous_sailing_left(conn, config):
    # ERL runs 13:30 then 15:25 in the fixture: 115 minutes back, inside the two-hour window.
    curve = _seeded_curve(conn, config, "15:25", origin="ERL")
    assert curve["previous_departure_minutes"] == 115


def test_a_predecessor_just_outside_the_window_is_still_named(tmp_path):
    """The window is two hours and most of this route's headways are longer, so the strictest
    reading — mark it only if it falls inside the chart — stays silent on exactly the charts
    that fall hardest on the left. A ferry that left 20 minutes before the window opened was
    still unloading the compound inside it."""
    from ferrycast.db import init_db

    config = _config_with_schedule(
        tmp_path,
        """
[[block]]
terminal = "SLT"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["10:20", "12:30", "16:30"]

[[block]]
terminal = "ERL"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["09:30", "13:30", "15:25"]
""",
    )
    conn = init_db(config.db_path)
    try:
        curve = _seeded_curve(conn, config, "12:30")
        assert curve["previous_departure_minutes"] == 130
    finally:
        conn.close()


def test_a_predecessor_that_finished_draining_first_is_not_marked(conn, config):
    """SLT's fixture headway is four hours — past the window and past the 45 minutes the
    aggregator allows a departure to go on mattering. Whatever that chart does on the left is
    the queue building, and hanging a departure on it would explain the wrong thing."""
    curve = _seeded_curve(conn, config, "12:30")
    assert curve["previous_departure_minutes"] is None


def test_the_first_sailing_of_the_day_has_nothing_before_it(conn, config):
    curve = _seeded_curve(conn, config, "08:30")
    assert curve["previous_departure_minutes"] is None


def test_a_timetable_change_mid_history_leaves_the_mark_off(tmp_path):
    """The pool spans dates, and a schedule that changed across them puts the previous
    departure in two places at once. One rule through a smeared event claims a precision the
    pool does not have, so nothing is drawn and the caption stays quiet about it."""
    from ferrycast.db import init_db

    # 15:25 keeps its time throughout; what moves is the sailing in front of it — 115 minutes
    # ahead on the older Fridays, 85 on the newer ones.
    config = _config_with_schedule(
        tmp_path,
        """
[[block]]
terminal = "SLT"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["08:30", "12:30", "16:30"]

[[block]]
terminal = "ERL"
effective_from = 2020-01-01
effective_to = 2026-07-15
days = "all"
departures = ["09:30", "13:30", "15:25"]

[[block]]
terminal = "ERL"
effective_from = 2026-07-16
effective_to = 2030-12-31
days = "all"
departures = ["09:30", "14:00", "15:25"]
""",
    )
    conn = init_db(config.db_path)
    try:
        curve = _seeded_curve(conn, config, "15:25", origin="ERL", days=fridays(4))
        assert curve["sailings"] == 4
        assert curve["previous_departure_minutes"] is None
    finally:
        conn.close()
