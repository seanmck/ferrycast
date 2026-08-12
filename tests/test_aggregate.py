"""R4 is the inferential heart of the project — these tests pin the overload rule down."""

from datetime import date, datetime, timedelta

from ferrycast.aggregate import aggregate_day, classify, classify_from_bands
from ferrycast.timeutil import combine_local, iso, now_utc, parse_hhmm

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

def _band_frames(conn, config, departure, bands, *, terminal="SLT", dock_before=True):
    """Lay down a band trace around a departure: (minutes relative to departure, band)."""
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
                1 if (dock_before and offset < 0) else 0,
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
