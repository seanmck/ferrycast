"""The calibration pass scores the reader without anyone labelling a frame.

Each test lays down a queue trace whose *shape* is known to be right or wrong, and checks
the score reflects it. That is the whole premise: the sailing cycle constrains what a
correct reading looks like, so the archive supplies its own ground truth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ferrycast.aggregate import aggregate_day
from ferrycast.calibrate import calibrate, spearman
from ferrycast.timeutil import combine_local, iso, parse_hhmm

from .conftest import add_observation, build_sailing_frames

DAY = date(2026, 6, 9)


def _departure(config, hhmm: str = "08:30") -> datetime:
    return combine_local(DAY, parse_hhmm(hhmm), config.tz)


def _board_says_departed(conn, config, hhmm: str, departed_hhmm: str, terminal: str = "SLT"):
    """A deck-space row carrying the board's actual departure time.

    Route 7 publishes no deck-space percentage, but it does publish this — which is what
    makes the post-departure drop an independent check rather than a circular one.
    """
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm, departed_hhmm,
                fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(combine_local(DAY, parse_hhmm(departed_hhmm), config.tz)),
            DAY.isoformat(),
            hhmm,
            departed_hhmm,
        ),
    )
    conn.commit()


def test_spearman_reads_direction_not_magnitude():
    rising = [1.0, 2.0, 3.0, 4.0]
    assert spearman(rising, [2.0, 9.0, 11.0, 40.0]) == 1.0
    assert spearman(rising, [40.0, 11.0, 9.0, 2.0]) == -1.0
    # A flat series is not evidence of a bad reader — an empty compound stays empty.
    assert spearman(rising, [3.0, 3.0, 3.0, 3.0]) is None


def test_a_clean_fill_and_clear_scores_well(conn, config):
    departure = _departure(config)
    build_sailing_frames(
        conn, config, departure, before=[2, 6, 11, 19, 28], after=[1, 0]
    )
    _board_says_departed(conn, config, "08:30", "08:34")
    aggregate_day(conn, config, DAY)

    report = calibrate(conn, config, origin="SLT")

    assert report.trend_score == 1.0
    assert report.median_rho is not None and report.median_rho > 0.9
    assert report.drop_score == 1.0
    assert report.noise_score == 1.0


def test_a_wandering_reader_fails_the_trend(conn, config):
    departure = _departure(config)
    # The compound cannot empty and refill in the hour before a sailing; a reader that
    # says it did is changing its mind, not watching a queue.
    build_sailing_frames(
        conn, config, departure, before=[24, 3, 27, 5, 22], after=[2, 1]
    )
    _board_says_departed(conn, config, "08:30", "08:34")
    aggregate_day(conn, config, DAY)

    report = calibrate(conn, config, origin="SLT")

    assert report.trend_score == 0.0
    assert report.noise_score is not None and report.noise_score < 0.5


def test_a_queue_that_never_clears_fails_the_drop(conn, config):
    departure = _departure(config)
    # Readings stay high long after the board says the vessel left. Either the reader is
    # hallucinating a queue, or it is reading a stale frame.
    build_sailing_frames(
        conn, config, departure, before=[4, 9, 15, 22, 26], after=[25, 24]
    )
    _board_says_departed(conn, config, "08:30", "08:34")
    aggregate_day(conn, config, DAY)

    report = calibrate(conn, config, origin="SLT")

    assert report.trend_score == 1.0      # it did track the fill
    assert report.drop_score == 0.0       # but the compound never cleared


def test_an_overloaded_sailing_still_counts_as_cleared(conn, config):
    departure = _departure(config)
    # The vessel filled and left people behind. That residual is the outcome the archive
    # exists to record, so a partial drop must not be scored as a reader failure.
    build_sailing_frames(
        conn, config, departure, before=[40, 80, 120, 160, 180], after=[55, 52]
    )
    _board_says_departed(conn, config, "08:30", "08:34")
    aggregate_day(conn, config, DAY)

    report = calibrate(conn, config, origin="SLT")

    assert report.drop_score == 1.0
    assert report.drops[0].ratio is not None
    assert report.drops[0].ratio < 0.5


def test_coverage_counts_what_the_usable_gate_removed(conn, config):
    departure = _departure(config)
    build_sailing_frames(conn, config, departure, before=[3, 8, 14], after=[1])
    # Lens dew: the frame is still perfectly readable, but the gate drops it.
    add_observation(
        conn,
        config,
        "SLT",
        departure - timedelta(minutes=20),
        18,
        usable=False,
    )
    conn.execute("UPDATE observations SET visibility = 'obscured' WHERE usable = 0")
    conn.commit()
    aggregate_day(conn, config, DAY)

    report = calibrate(conn, config, origin="SLT")

    assert report.coverage.observed == 5
    assert report.coverage.usable == 4
    assert report.coverage.unusable_reasons == {"obscured": 1}
    assert report.usable_share == 0.8

    # The same archive, scored without the gate, has the frame back.
    ungated = calibrate(conn, config, origin="SLT", usable_only=False)
    assert len(ungated.steps) > len(report.steps)


def test_missing_extraction_is_reported_not_crashed(conn, config):
    aggregate_day(conn, config, DAY)
    report = calibrate(conn, config, prompt_version="v-nonexistent")

    assert report.coverage.observed == 0
    assert report.trend_score is None
    assert any("no observations" in note for note in report.notes)
