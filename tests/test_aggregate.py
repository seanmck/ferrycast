"""R4 is the inferential heart of the project — these tests pin the overload rule down."""

from datetime import date, datetime, timedelta

from ferrycast.aggregate import aggregate_day, classify, classify_from_bands
from ferrycast.timeutil import combine_local, iso, now_utc, parse_hhmm, parse_iso

from .conftest import add_observation, build_sailing_frames


def _classify(**kwargs):
    base = dict(
        residual=0,
        queue_at_departure=40,
        departure_seen=True,
        capacity=125,
        residual_threshold=5,
    )
    return classify(**{**base, **kwargs})


def test_cleared_queue_means_everyone_boarded():
    outcome, overload, cancelled, carryover = _classify(residual=1)
    assert (outcome, overload, cancelled, carryover) == ("boarded", False, False, 0)


def test_residual_queue_after_departure_is_an_overload():
    outcome, overload, cancelled, carryover = _classify(residual=30)
    assert outcome == "waited_1"
    assert overload is True
    assert carryover == 30


def test_carryover_beyond_one_vessel_means_two_or_more_sailings():
    outcome, overload, _, carryover = _classify(residual=200)
    assert outcome == "waited_2plus"
    assert overload is True
    assert carryover == 200


def test_persistent_queue_with_no_vessel_is_a_cancellation():
    """Only where the camera actually shows the berth."""
    outcome, overload, cancelled, carryover = _classify(
        residual=60, departure_seen=False, berth_visible=True
    )
    assert outcome == "cancelled"
    assert cancelled is True
    assert overload is False
    assert carryover == 60


def test_a_camera_that_cannot_see_the_berth_never_reports_a_cancellation():
    """Earls Cove's camera points up the approach road and never sees a vessel, so "no
    ferry in any frame" is its normal state. Reading that as a cancellation marked every
    busy sailing at that terminal cancelled."""
    outcome, _, cancelled, _ = _classify(
        residual=60, departure_seen=False, berth_visible=False
    )
    assert outcome == "unknown"
    assert cancelled is False


def test_missing_frames_yield_unknown_rather_than_a_guess():
    assert _classify(residual=None)[0] == "unknown"
    assert _classify(queue_at_departure=None)[0] == "unknown"


def test_threshold_is_inclusive_boundary():
    assert _classify(residual=4)[0] == "boarded"
    assert _classify(residual=5)[0] == "waited_1"


def _departure(config, day: date, hhmm: str) -> datetime:
    return combine_local(day, parse_hhmm(hhmm), config.tz)


def test_end_to_end_overloaded_sailing(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(
        conn, config, departure, before=[20, 55, 90], after=[40, 45], dock_before=True
    )

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.* FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()
    assert row["outcome"] == "waited_1"
    assert row["overload"] == 1
    assert row["peak_queue"] == 90
    assert row["queue_at_departure"] == 90
    assert row["residual_queue"] == 40
    assert row["carryover"] == 40


def test_end_to_end_cleared_sailing(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(conn, config, departure, before=[10, 25, 30], after=[0, 2])

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome, r.peak_queue FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30'"""
    ).fetchone()
    assert row["outcome"] == "boarded"
    assert row["peak_queue"] == 30


def test_sailing_with_no_frames_is_unknown_not_empty(conn, config):
    day = date(2026, 8, 14)
    counts = aggregate_day(conn, config, day)
    # Every scheduled sailing still gets a row, marked unknown.
    assert counts["unknown"] == 6
    total = conn.execute("SELECT COUNT(*) FROM sailing_records").fetchone()[0]
    assert total == 6


def test_unusable_observations_are_excluded(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    # A dark frame claiming an empty compound must not be read as "the queue cleared".
    add_observation(conn, config, "SLT", departure - timedelta(minutes=15), 80)
    add_observation(
        conn, config, "SLT", departure + timedelta(minutes=15), 0, usable=False, confidence=0.05
    )

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["outcome"] == "unknown"


def test_truncated_queue_is_flagged(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    add_observation(
        conn, config, "SLT", departure - timedelta(minutes=15), 120, beyond_frame=True
    )
    add_observation(conn, config, "SLT", departure + timedelta(minutes=15), 60)

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.queue_truncated FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["queue_truncated"] == 1


def test_deck_space_alone_can_confirm_a_departure(conn, config):
    """Night sailings may have no usable camera view of the vessel; the feed still anchors it."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    # Queue persists, and no vessel is ever seen at the dock.
    build_sailing_frames(
        conn, config, departure, before=[50, 70], after=[40], dock_before=False
    )
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                percent_available, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30', 0, 'ok')""",
        (config.route.id, "2026-08-14T19:00:00Z", day.isoformat()),
    )
    conn.commit()

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.outcome, r.deck_space_min FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '12:30' AND s.origin = 'SLT'"""
    ).fetchone()
    # The sailing ran, so this is an overload rather than a cancellation.
    assert row["outcome"] == "waited_1"
    assert row["deck_space_min"] == 0


def test_aggregation_is_idempotent(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(conn, config, departure, before=[20, 55], after=[40])

    aggregate_day(conn, config, day)
    aggregate_day(conn, config, day)

    assert conn.execute("SELECT COUNT(*) FROM sailings").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM sailing_records").fetchone()[0] == 6


def test_frames_from_the_previous_sailing_do_not_leak_in(conn, config):
    """A 12:30 residual must not be mistaken for the 16:30's queue build-up."""
    day = date(2026, 8, 14)
    earlier = _departure(config, day, "12:30")
    # A big queue right after the 12:30 departure.
    add_observation(conn, config, "SLT", earlier + timedelta(minutes=15), 95)
    later = _departure(config, day, "16:30")
    add_observation(conn, config, "SLT", later - timedelta(minutes=15), 10)
    add_observation(conn, config, "SLT", later + timedelta(minutes=15), 0)

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.peak_queue, r.outcome FROM sailing_records r
             JOIN sailings s ON s.id = r.sailing_id
            WHERE s.depart_hhmm = '16:30' AND s.origin = 'SLT'"""
    ).fetchone()
    assert row["peak_queue"] == 10
    assert row["outcome"] == "boarded"


# ---- Late sailings ----------------------------------------------------------------------
#
# The offsets are relative to the *scheduled* departure, and this route runs late routinely:
# the live board showed a 9:25 leaving at 9:56. Reading the "residual" at scheduled + 12 min
# photographs a compound that is still loading, and every vehicle about to board looks like
# a vehicle left behind.


def test_a_late_sailing_is_not_read_as_an_overload(conn, config):
    """The compound is full at scheduled+12 because the vessel has not gone yet."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")

    # Queue builds, vessel berths and is STILL there 15 minutes after the scheduled time.
    for minutes, count, docked in [(-45, 30, False), (-15, 80, True), (15, 78, True)]:
        add_observation(
            conn, config, "SLT", departure + timedelta(minutes=minutes), count,
            ferry_at_dock=docked,
        )
    # It leaves late; by the time it has, the compound is nearly clear.
    add_observation(conn, config, "SLT", departure + timedelta(minutes=35), 2)

    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()

    assert row["outcome"] == "boarded", "everyone got on; the sailing was simply late"
    assert row["residual_queue"] == 2, "residual must come from after it actually left"


def test_a_vessel_still_at_the_dock_is_not_a_cancellation(conn, config):
    """The old rule was 'queue persists and no departure seen -> cancelled'. A vessel sitting
    at the berth is the opposite of a cancellation, and the two must not share an outcome."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    for minutes, count in [(-45, 30), (-15, 80), (15, 78), (30, 75)]:
        add_observation(
            conn, config, "SLT", departure + timedelta(minutes=minutes), count,
            ferry_at_dock=True,
        )

    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()

    assert row["outcome"] == "unknown"
    assert row["cancelled"] == 0


def test_a_vessel_that_never_arrives_is_still_a_cancellation(conn, config):
    """The genuine case the rule was written for: no vessel at all, queue never clears —
    and a camera that can actually see the berth, so its absence means something."""
    from dataclasses import replace as _replace

    config = _replace(
        config,
        routes=tuple(
            _replace(
                r,
                terminals=tuple(_replace(t, camera_sees_berth=True) for t in r.terminals),
            )
            for r in config.routes
        ),
    )
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    for minutes, count in [(-45, 30), (-15, 80), (15, 78), (30, 75)]:
        add_observation(
            conn, config, "SLT", departure + timedelta(minutes=minutes), count,
            ferry_at_dock=False,
        )

    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()

    assert row["outcome"] == "cancelled"


def test_an_on_time_sailing_still_settles_the_normal_way(conn, config):
    """The late-departure rule must not shift the reading on a sailing that ran to time."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(
        conn, config, departure, before=[20, 55, 90], after=[40, 45], dock_before=True
    )
    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()
    assert row["outcome"] == "waited_1"
    assert row["residual_queue"] == 40


def _board_row(conn, config, day, hhmm, departed, origin="SLT"):
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                departed_hhmm, status_text, fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, 'Departed', 'ok')""",
        (
            config.route.id,
            origin,
            combine_local(day, parse_hhmm(hhmm), config.tz).isoformat(),
            day.isoformat(),
            hhmm,
            departed,
        ),
    )
    conn.commit()


def test_the_board_departure_rescues_a_berth_blind_camera(conn, config):
    """Earls Cove's camera never shows a vessel, so frames alone can never establish that a
    sailing ran — and an overload is exactly "it ran and left people behind". The board's
    published departure is what makes that outcome reachable at all."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "13:30")
    for minutes, count in [(-45, 40), (-15, 95), (30, 60)]:
        add_observation(
            conn, config, "ERL", departure + timedelta(minutes=minutes), count,
            ferry_at_dock=False,
        )
    _board_row(conn, config, day, "13:30", "13:35", origin="ERL")

    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'ERL' AND s.depart_hhmm = '13:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()

    assert row["outcome"] == "waited_1"
    assert row["carryover"] == 60


def test_the_board_departure_beats_the_scheduled_time_for_a_late_sailing(conn, config):
    """A 30-minute delay, and the camera cannot see the berth to notice."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "13:30")
    for minutes, count in [(-45, 40), (-15, 95), (15, 90), (45, 3)]:
        add_observation(
            conn, config, "ERL", departure + timedelta(minutes=minutes), count,
            ferry_at_dock=False,
        )
    _board_row(conn, config, day, "13:30", "14:00", origin="ERL")

    aggregate_day(conn, config, day)
    row = conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'ERL' AND s.depart_hhmm = '13:30' AND s.service_date = ?""",
        (day.isoformat(),),
    ).fetchone()

    # The +15 frame shows 90 vehicles still waiting — but it had not left yet.
    assert row["residual_queue"] == 3
    assert row["outcome"] == "boarded"


# --- bands: what the cameras can actually support -------------------------------------

def _band_frames(
    conn, config, departure, bands, *, terminal="SLT", dock_before=True, dock_until=0
):
    """Lay down a band trace around a departure: (minutes relative to departure, band).

    `dock_until` is where the vessel stops being berthed, for the late sailings this route
    actually runs. It defaults to the scheduled minute, which is the on-time case.
    """
    for offset, band in bands:
        at = departure + timedelta(minutes=offset)
        cur = conn.execute(
            """INSERT INTO frames (route, terminal, captured_at, service_date, path, status)
               VALUES (?, ?, ?, ?, ?, 'ok')""",
            (
                config.route.id,
                terminal,
                iso(at),
                departure.date().isoformat(),
                f"{terminal}-{offset}.jpg",
            ),
        )
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, fullness, ferry_at_dock,
                    visibility, confidence, usable, created_at)
               VALUES (?, ?, 'claude-sonnet-5', NULL, ?, ?, 'clear', 0.8, 1, ?)""",
            (
                cur.lastrowid,
                config.vision.prompt_version,
                band,
                1 if (dock_before and offset < dock_until) else 0,
                iso(now_utc()),
            ),
        )
    conn.commit()


def _record_for(conn, hhmm="12:30"):
    return conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = ?""",
        (hhmm,),
    ).fetchone()


def test_a_compound_that_empties_means_everyone_boarded(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn,
        config,
        departure,
        [(-45, "light"), (-30, "moderate"), (-15, "heavy"), (20, "empty")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "boarded"
    assert row["peak_fullness"] == "heavy"
    assert row["fullness_at_departure"] == "heavy"
    assert row["residual_fullness"] == "empty"


def test_a_compound_still_occupied_after_departure_is_filled_not_waited(conn, config):
    """A band says somebody was left behind. It cannot say for how many sailings.

    `waited_1` asserts a carryover that fits one vessel, which needs a count. Claiming it
    from "moderate" would be inventing precision the measurement does not have.
    """
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn,
        config,
        departure,
        [(-45, "moderate"), (-15, "overflowing"), (20, "moderate")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "filled"
    assert row["overload"] == 1
    assert row["carryover"] is None
    assert row["residual_fullness"] == "moderate"


def test_peak_is_the_fullest_band_not_the_alphabetical_one(conn, config):
    # "light" > "heavy" as strings; ordering has to come from the scale.
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure, [(-45, "light"), (-30, "heavy"), (-15, "light"), (20, "empty")]
    )

    aggregate_day(conn, config, day)

    assert _record_for(conn)["peak_fullness"] == "heavy"


def test_the_clear_and_the_build_are_both_recorded(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn,
        config,
        departure,
        [(-60, "empty"), (-45, "light"), (-15, "heavy"), (20, "empty")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    # The queue started when something first appeared, not at the first frame.
    assert row["queue_started_at"] == iso(departure + timedelta(minutes=-45))
    assert row["cleared_at"] == iso(departure + timedelta(minutes=20))


def test_an_empty_sailing_never_started_a_queue(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(conn, config, departure, [(-45, "empty"), (-15, "empty"), (20, "empty")])

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "boarded"
    assert row["queue_started_at"] is None
    assert row["peak_fullness"] == "empty"


def test_counts_still_work_for_frames_read_under_the_old_prompt(conn, config):
    """v1 observations carry no band. They must keep classifying exactly as before."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    build_sailing_frames(conn, config, departure, before=[20, 55, 90], after=[40, 45])

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "waited_1"
    assert row["peak_queue"] == 90
    assert row["carryover"] == 40
    assert row["peak_fullness"] is None


def test_bands_are_preferred_when_a_frame_carries_both():
    outcome, overload, _, carryover = classify_from_bands(
        residual_fullness="empty",
        fullness_at_departure="overflowing",
        departure_seen=True,
    )
    assert (outcome, overload, carryover) == ("boarded", False, 0)


def test_a_compound_that_was_bare_when_the_vessel_left_took_everyone_in_it():
    """The 2026-08-15 05:35 shape: cleared at 05:28, sailed, two lanes filled again by 05:48.

    Those two lanes are the 07:25's queue starting to form, not vehicles that failed to get
    on. Read as a residual they put "provably left vehicles behind" on the emptiest sailing of
    the day, on a morning the departures board never posted a capacity notice at all.
    """
    outcome, overload, _, carryover = classify_from_bands(
        residual_fullness="light",
        fullness_at_departure="empty",
        departure_seen=True,
        empty_when_it_left=True,
    )
    assert (outcome, overload, carryover) == ("boarded", False, 0)


def test_a_residual_lighter_than_the_departure_band_is_still_an_overload():
    """"The residual got lighter" is not the discriminator, and must not be mistaken for one.

    Draining from heavy to light is precisely what a real overload looks like: on 2026-08-14 the
    16:55 went heavy → light and the board independently posted its capacity notice. A rule that
    cleared a sailing whenever the compound thinned out would erase exactly those.
    """
    outcome, overload, *_ = classify_from_bands(
        residual_fullness="light",
        fullness_at_departure="heavy",
        departure_seen=True,
        empty_when_it_left=False,
    )
    assert (outcome, overload) == ("filled", True)


def test_an_emptied_compound_with_no_vessel_ever_seen_is_not_a_boarding():
    """A compound emptying only says a sailing carried it away if something saw a sailing.

    Where nothing did, bare asphalt is equally consistent with a cancellation nobody queued
    for — so the clear must not outrank the departure check that sits above it.
    """
    blind, *_ = classify_from_bands(
        residual_fullness="light",
        fullness_at_departure="empty",
        departure_seen=False,
        berth_visible=False,
        empty_when_it_left=True,
    )
    seeing, _, cancelled, _ = classify_from_bands(
        residual_fullness="light",
        fullness_at_departure="empty",
        departure_seen=False,
        berth_visible=True,
        empty_when_it_left=True,
    )
    assert blind == "unknown"
    assert (seeing, cancelled) == ("cancelled", True)


def test_a_vessel_still_berthed_is_unknown_not_an_overload():
    outcome, *_ = classify_from_bands(
        residual_fullness="heavy",
        fullness_at_departure="heavy",
        departure_seen=False,
        still_at_dock=True,
    )
    assert outcome == "unknown"


def test_a_queue_with_no_departure_is_only_a_cancellation_where_the_berth_is_visible():
    blind, *_ = classify_from_bands(
        residual_fullness="heavy",
        fullness_at_departure="heavy",
        departure_seen=False,
        berth_visible=False,
    )
    seeing, _, cancelled, _ = classify_from_bands(
        residual_fullness="heavy",
        fullness_at_departure="heavy",
        departure_seen=False,
        berth_visible=True,
    )
    assert blind == "unknown"
    assert (seeing, cancelled) == ("cancelled", True)


def test_the_board_saying_full_fills_the_sailing_without_leaving_anyone_behind(conn, config):
    """Two claims that must not be blurred. "We loaded as many as would fit" describes the
    deck, which no camera sees. "Somebody was left on the tarmac" describes the compound,
    which no board sees. Only the second is an overload — and a sailing that filled AND
    cleared is the one you would have made by a margin nothing else can show, which needs
    both axes to be sayable at once."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure,
        [(-45, "moderate"), (-15, "heavy"), (20, "empty")],
    )
    for minutes in (40, 10):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    status_text, departed_hhmm, fetch_status)
               VALUES (?, 'SLT', ?, ?, '12:30', ?, '12:41', 'ok')""",
            (
                config.route.id,
                iso(departure - timedelta(minutes=minutes)),
                day.isoformat(),
                "Departed 12:41 pm Malaspina Sky Peak travel. Loading maximum "
                "number of vehicles",
            ),
        )
    conn.commit()

    aggregate_day(conn, config, day)
    row = _record_for(conn)

    assert row["left_full"] == 1
    assert row["filled"] == 1
    # The compound cleared, so everyone got on. The vessel still ran out of room.
    assert row["left_behind"] == 0
    assert row["overload"] == 0
    assert row["outcome"] == "filled"


def _geom_frames(conn, config, departure, readings, *, terminal="SLT"):
    """Lay down geometric readings: (minutes relative to departure, lanes, band)."""
    for offset, lanes, band in readings:
        at = departure + timedelta(minutes=offset)
        cur = conn.execute(
            """INSERT INTO frames (route, terminal, captured_at, service_date, path, status)
               VALUES (?, ?, ?, ?, ?, 'ok')""",
            (config.route.id, terminal, iso(at), departure.date().isoformat(),
             f"g{terminal}-{offset}.jpg"),
        )
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                    fullness, ferry_at_dock, visibility, confidence, usable, created_at)
               VALUES (?, 'geom-v1', 'lane-geometry', NULL, ?, ?, 0, 'clear', 0.8, 1, ?)""",
            (cur.lastrowid, lanes, band, iso(now_utc())),
        )
    conn.commit()


def test_geometric_readings_reach_aggregation(conn, config):
    """They did not, and nothing said so. `_load_observations` filtered on the configured
    *vision* prompt version, so every geometrically-read frame was invisible here and every
    record stayed `unknown` — a whole extraction path feeding nothing."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _geom_frames(
        conn, config, departure,
        [(-45, 3, "light"), (-15, 8, "heavy"), (20, 0, "empty")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "boarded"
    assert row["peak_fullness"] == "heavy"
    assert row["method"] == "frames:geom-v1"


def test_geometry_is_preferred_over_the_model_for_the_same_frame(conn, config):
    """Both extractors can read one frame. Geometry wins: it is measured against known lane
    positions rather than judged, and the model path's failure was calling a compound clear
    while a queue stood in the lanes whose numbers are not painted in view."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _geom_frames(conn, config, departure, [(-15, 8, "heavy"), (20, 0, "empty")])
    # the model, reading the same frames, saw an empty compound before departure
    for frame_id, in conn.execute("SELECT id FROM frames").fetchall():
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, fullness,
                    ferry_at_dock, visibility, confidence, usable, created_at)
               VALUES (?, ?, 'claude-sonnet-5', NULL, 'empty', 0, 'clear', 0.9, 1, ?)""",
            (frame_id, config.vision.prompt_version, iso(now_utc())),
        )
    conn.commit()

    aggregate_day(conn, config, day)
    row = _record_for(conn)

    assert row["peak_fullness"] == "heavy"  # not the model's "empty"
    assert row["method"] == "frames:geom-v1"


def test_a_capacity_notice_alone_is_enough_to_say_a_sailing_filled(conn, config):
    """No percentages, no frames, one line of board text — and it is affirmative evidence,
    so it does not need a reading near departure to back it up. The notice appears on the
    board's second line once the vessel has gone, after the last percentage would have been
    published, which is exactly the shape of series the "had space" rule refuses to read."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                status_text, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30',
                   'Departed 12:41 pm Peak travel. Loading maximum number of vehicles', 'ok')""",
        (config.route.id, iso(departure + timedelta(minutes=15)), day.isoformat()),
    )
    conn.commit()

    aggregate_day(conn, config, day)
    row = _record_for(conn)

    assert row["filled"] == 1
    # Nobody watched the approach road, so the page must not claim either way.
    assert row["left_behind"] is None
    assert row["outcome"] == "filled"
    assert row["method"] == "deck_space"


def test_a_sailing_with_no_board_note_records_left_full_false_not_null(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(conn, config, departure, [(-30, "light"), (20, "empty")])
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                status_text, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30', 'Malaspina Sky Upcoming', 'ok')""",
        (config.route.id, iso(departure - timedelta(minutes=20)), day.isoformat()),
    )
    conn.commit()

    aggregate_day(conn, config, day)
    assert _record_for(conn)["left_full"] == 0


def test_the_clear_is_the_transition_not_a_later_confirmation(conn, config):
    """The compound empties while the vessel loads, before it goes. On 2026-08-12 the tarmac
    was bare at 12:00 and the 11:45 departed at 12:03.

    This searched from twelve minutes *after* departure, so it could only ever return a time
    later than the sailing left — a page showing both read as nonsense. At a 15-minute cadence
    the error hid, because no frame fell between the last occupied one and departure.
    """
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure,
        # busy, then bare five minutes BEFORE departure, and still bare afterwards
        [(-20, "heavy"), (-10, "heavy"), (-5, "empty"), (10, "empty"), (25, "empty")],
    )

    aggregate_day(conn, config, day)

    cleared = _record_for(conn)["cleared_at"]
    assert cleared == iso(departure - timedelta(minutes=5))
    assert parse_iso(cleared) < departure


def test_a_compound_that_never_empties_has_no_clear(conn, config):
    """Which is what an overload looks like, and what the residual is there to catch."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure,
        [(-30, "heavy"), (-10, "heavy"), (20, "light"), (35, "light")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["cleared_at"] is None
    assert row["residual_fullness"] == "light"
    # The 2026-08-14 16:55 shape, and the board posted its capacity notice on that one. Pinned
    # here so the clear gate below can never be widened into clearing a real overload.
    assert row["outcome"] == "filled"
    assert row["overload"] == 1


def test_the_first_sailing_of_the_day_is_not_an_overload_because_the_next_queue_formed(
    conn, config
):
    """A queue that forms after the compound went bare belongs to the *next* sailing.

    On 2026-08-15 the 05:35 out of Saltery Bay cleared at 05:28, sailed, and two lanes filled
    again at 05:48 — arrivals for the 07:25, which duly built to heavy and boarded. The record
    read "ran out of room" and "left vehicles behind" anyway: `residual_fullness` is the single
    first frame after the settle, and the band path called any band above "empty" an overload
    where the count path would have wanted five vehicles.

    The dawn sailing is the most exposed on the timetable — first of the day, and 110 minutes
    of clear water before the next, so the post-window is never clamped — but nothing here is
    dawn-specific. The geometry was right about every frame; the reading of it was not.
    """
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure,
        # a queue, bare seven minutes before departure, still bare as it goes, and then the
        # next sailing's arrivals
        [(-45, "light"), (-20, "light"), (-7, "empty"), (5, "empty"), (25, "light")],
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["outcome"] == "boarded"
    assert row["overload"] == 0
    assert row["left_behind"] == 0
    assert row["cleared_at"] == iso(departure - timedelta(minutes=7))
    # The measurement is kept and only its reading changes: the compound really was occupied
    # again afterwards, and a later report about that queue must still find the number.
    assert row["residual_fullness"] == "light"


def test_a_compound_that_refilled_before_a_late_vessel_left_still_left_people_behind(
    conn, config
):
    """The gate is anchored to the ship, not to the timetable, and this is why.

    A sailing 40 minutes late is routine on this route — the live board showed 9:25 departing
    at 9:56. So a compound can be bare at the scheduled minute, fill again while the vessel is
    still loading, and strand everyone who joined in between. Keying the clear off
    `fullness_at_departure`, which stops at the scheduled time, would read that as "boarded"
    and state outright that nobody was left behind — a false denial on a real overload, which
    is worse than the false positive this change removes and in the one direction this app is
    not allowed to be wrong in.
    """
    day = date(2026, 8, 14)
    departure = _departure(config, day, "12:30")
    _band_frames(
        conn, config, departure,
        [(-20, "light"), (-5, "empty"), (10, "heavy"), (25, "heavy"), (40, "light")],
        dock_until=30,
    )

    aggregate_day(conn, config, day)

    row = _record_for(conn)
    assert row["fullness_at_departure"] == "empty"
    assert row["outcome"] == "filled"
    assert row["left_behind"] == 1


# --- the highway camera: overflow evidence for the direction with no board ---------------


def _extent_frames(conn, config, departure, readings, *, terminal="ERL"):
    """Lay down highway readings: (minutes relative to departure, band-or-None).

    Mirrors what `extent.extract_frames` stores: bands only, `vehicle_count` NULL even when
    the reader counted vehicles standing on the highway, because that count is not a
    compound residual. A None band is a clear road — trustworthy, and attesting nothing.
    """
    for offset, band in readings:
        at = departure + timedelta(minutes=offset)
        cur = conn.execute(
            """INSERT INTO frames (route, terminal, camera, captured_at, service_date,
                                   path, status)
               VALUES (?, ?, 'drivebc244', ?, ?, ?, 'ok')""",
            (config.route.id, terminal, iso(at), departure.date().isoformat(),
             f"hw{terminal}-{offset}.jpg"),
        )
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                    fullness, ferry_at_dock, visibility, confidence, usable, created_at)
               VALUES (?, 'extent-v1', 'queue-extent', NULL, NULL, ?, 0, 'clear',
                       0.9, 1, ?)""",
            (cur.lastrowid, band, iso(now_utc())),
        )
    conn.commit()


def _tracked_departure(conn, config, departure):
    """The vessel tracker seeing the sailing go — Earls Cove's only departure source."""
    from ferrycast.vessels import VesselReading, store_readings

    for offset, status, heading in [
        (-10, "In Port", "S"), (5, "Under Way", "W"), (25, "Under Way", "NW"),
    ]:
        moment = departure + timedelta(minutes=offset)
        store_readings(
            conn, config, moment,
            [VesselReading("Malaspina Sky", status, heading,
                           moment.astimezone(config.tz).strftime("%H:%M"))],
        )


def _erl_record(conn, hhmm="13:30"):
    return conn.execute(
        """SELECT r.* FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = 'ERL' AND s.depart_hhmm = ?""",
        (hhmm,),
    ).fetchone()


def test_a_highway_overflow_with_a_tracked_departure_reads_filled(conn, config):
    """Earls Cove's whole free evidence chain in one record. No board, no percentages —
    the tracker says when the sailing went, and the highway camera says the compound had
    run out of room before it went and still had a tail standing after. That pair is
    `filled`: the first outcome the homeward direction can produce without a human."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "13:30")
    _extent_frames(conn, config, departure,
                   [(-30, "heavy"), (-10, "overflowing"), (20, "heavy")])
    _tracked_departure(conn, config, departure)

    aggregate_day(conn, config, day)
    row = _erl_record(conn)

    assert row["outcome"] == "filled"
    assert row["filled"] == 1
    assert row["left_behind"] == 1
    assert row["peak_fullness"] == "overflowing"
    assert row["method"] == "frames:extent-v1"


def test_a_clear_highway_is_not_evidence_anyone_boarded(conn, config):
    """The false all-clear, end to end. A clear road means only that the queue has not
    overflowed — the compound behind it can be anywhere from bare to brim-full — so a day
    of clear readings plus a tracked departure must stay `unknown`, never `boarded`."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "13:30")
    _extent_frames(conn, config, departure, [(-30, None), (-10, None), (20, None)])
    _tracked_departure(conn, config, departure)

    aggregate_day(conn, config, day)
    row = _erl_record(conn)

    assert row["outcome"] == "unknown"
    assert row["filled"] is None
    assert row["left_behind"] is None


def test_the_compound_and_highway_cameras_witness_one_sailing_together(conn, config):
    """Two free geometric readers, one window: the terminal camera saw the lanes full
    before departure and the highway camera saw a tail still standing after it."""
    day = date(2026, 8, 14)
    departure = _departure(config, day, "13:30")
    _geom_frames(conn, config, departure, [(-15, 2, "heavy")], terminal="ERL")
    _extent_frames(conn, config, departure, [(20, "heavy")])
    _tracked_departure(conn, config, departure)

    aggregate_day(conn, config, day)
    row = _erl_record(conn)

    assert row["outcome"] == "filled"
    assert row["method"] == "frames:extent-v1+geom-v1"
