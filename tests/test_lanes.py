"""Geometric lane reading.

The failure this module exists to prevent is a false "empty": reporting a clear compound
while a queue stands in it. That is the one direction the project must not be wrong in, so
most of what is pinned here is refusal — that an uncalibrated camera, a moved camera, or an
obscured frame produces no answer rather than a confident wrong one.
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from ferrycast.lanes import (
    ILLUMINATION_BAND,
    MAX_DRIFT_PX,
    CalibrationError,
    LaneCalibration,
    drift_px,
    fullness_from_lanes,
    illumination_ratio,
    occupancy,
    occupied_lanes,
    queue_reaches_last_visible_lane,
    read_frame,
)

REPO = Path(__file__).resolve().parents[1]
SLT_CAL = REPO / "config" / "calibration" / "SLT.json"
SLT_BG = REPO / "config" / "calibration" / "SLT_background.jpg"


@pytest.fixture
def cal():
    return LaneCalibration.load(SLT_CAL)


def _paint(cal, occupied=(), size=(320, 240), fill=0.7):
    """Synthesise a frame: bare pavement, with vehicles standing in each occupied lane.

    Only part of each lane is darkened, because that is what a real queue looks like — there
    is asphalt between and around the vehicles. A lane blacked out end to end would also
    flatten the brightness statistic that distinguishes a full compound from a dark one, so
    a fully-filled fixture would test something the camera never sees.
    """
    img = Image.new("RGB", size, (180, 178, 176))
    d = ImageDraw.Draw(img)
    for lane in occupied:
        rows = cal.lane_spans[lane]
        for i, (y, x0, x1) in enumerate(rows):
            if i % 10 >= fill * 10:
                continue
            d.line([(x0, y), (x1 - 1, y)], fill=(40, 40, 45))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_the_shipped_calibration_covers_the_lanes_the_camera_can_see(cal):
    # Lanes 1 and 2 leave the frame before they reach the pavement the camera sees, so a
    # calibration claiming to measure them would be claiming coverage it does not have.
    assert cal.lanes == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert cal.terminal == "SLT"
    assert cal.image_size == (320, 240)
    # The far lanes are slivers; the near ones are not. Both are honest, but a caller
    # weighting them equally should know.
    assert cal.area(3) < cal.area(12) / 5


def test_an_empty_compound_reads_as_empty(cal):
    bare = _paint(cal)
    shares = occupancy(bare, bare, cal)
    assert occupied_lanes(shares) == []
    assert fullness_from_lanes(shares, cal) == "empty"


def test_a_vehicle_in_an_unnumbered_lane_is_still_seen(cal):
    """The failure that motivated this module: only lanes 9-12 have numbers painted in view,
    and a reader anchored on the paint reported an empty compound while lane 4 held a queue."""
    shares = occupancy(_paint(cal, occupied=[4]), _paint(cal), cal)
    assert occupied_lanes(shares) == [4]
    assert fullness_from_lanes(shares, cal) != "empty"


def test_bands_follow_how_many_lanes_are_in_use(cal):
    bare = _paint(cal)
    bands = [
        fullness_from_lanes(occupancy(_paint(cal, occupied=lanes), bare, cal), cal)
        for lanes in ([], [3], [3, 4, 5], [3, 4, 5, 6, 7, 8, 9], cal.lanes)
    ]
    assert bands == ["empty", "light", "moderate", "heavy", "overflowing"]


def test_overflowing_requires_every_visible_lane(cal):
    """The band that tells a traveller to give up needs to mean something. One free lane is
    not overflowing, however full everything else is."""
    almost = [k for k in cal.lanes if k != 12]
    shares = occupancy(_paint(cal, occupied=almost), _paint(cal), cal)
    assert fullness_from_lanes(shares, cal) == "heavy"


def test_a_queue_in_the_lowest_visible_lane_may_run_off_frame(cal):
    bare = _paint(cal)
    assert queue_reaches_last_visible_lane(occupancy(_paint(cal, occupied=[3]), bare, cal), cal)
    # Vehicles nearest the camera say nothing about what is past the far edge.
    assert not queue_reaches_last_visible_lane(
        occupancy(_paint(cal, occupied=[12]), bare, cal), cal
    )


def test_a_frame_of_the_wrong_size_is_refused(cal):
    small = Image.new("RGB", (160, 120), (180, 178, 176))
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    with pytest.raises(CalibrationError, match="calibrated at"):
        occupancy(buf.getvalue(), buf.getvalue(), cal)


def test_a_calibration_for_a_different_kind_of_scene_is_refused(tmp_path):
    """Earls Cove faces the approach road. Reading it as a lane grid would invent lanes."""
    other = tmp_path / "ERL.json"
    other.write_text(json.dumps({"terminal": "ERL", "kind": "queue_extent"}), encoding="utf-8")
    with pytest.raises(CalibrationError, match="queue_extent"):
        LaneCalibration.load(other)


@pytest.mark.skipif(not SLT_BG.exists(), reason="background reference not present")
def test_drift_is_small_on_the_frames_the_calibration_was_fitted_against(cal):
    assert drift_px(SLT_BG, cal) < MAX_DRIFT_PX


# --- Earls Cove: a camera that sees only part of its compound ----------------------------

ERL_CAL = REPO / "config" / "calibration" / "ERL.json"
ERL_BG = REPO / "config" / "calibration" / "ERL_background.jpg"

erl_only = pytest.mark.skipif(
    not (ERL_CAL.exists() and ERL_BG.exists()), reason="Earls Cove calibration not present"
)


@pytest.fixture
def erl():
    return LaneCalibration.load(ERL_CAL)


@erl_only
def test_earls_cove_covers_the_two_lanes_whose_paint_can_be_fitted(erl):
    """Four lanes are in frame and two are fittable. Three and a slice of two lie under
    trees, cones and the vehicles themselves, and a boundary guessed to ten pixels would
    put vehicles in the wrong lane rather than admit it could not tell."""
    assert erl.terminal == "ERL"
    assert erl.lanes == [4, 5]
    assert erl.image_size == (320, 240)


@erl_only
def test_earls_cove_does_not_claim_to_see_the_whole_compound(erl):
    assert erl.covers_compound is False


@erl_only
def test_a_partly_seen_compound_never_reports_empty(erl):
    """The failure this flag exists to stop. This camera faces away from the dock down the
    tail ends of the lanes, and arriving traffic queues in a lane that is not fitted — on
    2026-08-15 at 14:57, five vehicles stood in frame with both calibrated lanes clear.
    Reported as "empty" that is a confident all-clear for a compound nobody can see."""
    bare = _paint(erl)
    shares = occupancy(bare, bare, erl)

    assert occupied_lanes(shares) == []
    assert fullness_from_lanes(shares, erl) is None      # not "empty"


@erl_only
def test_a_saltery_bay_style_camera_still_reports_empty(cal):
    """The distinction is per calibration, not a global softening. Where the lanes account
    for the whole compound, bare pavement really is an empty compound."""
    bare = _paint(cal)
    assert cal.covers_compound is True
    assert fullness_from_lanes(occupancy(bare, bare, cal), cal) == "empty"


@erl_only
def test_occupancy_at_the_far_end_is_the_trustworthy_direction(erl):
    """Bare pavement says nothing, but a vehicle says a lot: these lanes are the last
    pavement to fill, so anything standing here has everything ahead of it already taken."""
    bare = _paint(erl)
    one = fullness_from_lanes(occupancy(_paint(erl, occupied=[5]), bare, erl), erl)
    both = fullness_from_lanes(occupancy(_paint(erl, occupied=[4, 5]), bare, erl), erl)

    assert one == "heavy"
    assert both == "overflowing"


@erl_only
def test_a_clear_reading_carries_its_own_caveat(erl):
    reading = read_frame(ERL_BG, ERL_BG, erl)
    assert reading["fullness"] is None
    assert "says nothing about how full the rest of it is" in reading["notes"]


@erl_only
def test_earls_cove_drift_is_small_on_the_frame_it_was_fitted_against(erl):
    assert drift_px(ERL_BG, erl) < MAX_DRIFT_PX


@pytest.mark.skipif(not SLT_BG.exists(), reason="background reference not present")
def test_a_moved_camera_is_detected_rather_than_mislabelled(cal):
    """A knock or a maintenance visit re-aims the camera and every lane number shifts. Silent
    mislabelling is worse than no reading, so this has to be caught."""
    base = Image.open(SLT_BG).convert("RGB")
    buf = io.BytesIO()
    ImageChops.offset(base, 6, 0).save(buf, format="JPEG", quality=92)
    assert drift_px(buf.getvalue(), cal) > MAX_DRIFT_PX


@pytest.mark.skipif(not SLT_BG.exists(), reason="background reference not present")
def test_a_moved_camera_produces_no_reading_at_all(cal):
    base = Image.open(SLT_BG).convert("RGB")
    buf = io.BytesIO()
    ImageChops.offset(base, 6, 0).save(buf, format="JPEG", quality=92)
    out = read_frame(buf.getvalue(), SLT_BG, cal)
    assert out["compound_visible"] is False
    assert out["fullness"] is None
    assert "may have moved" in out["notes"]


def test_a_blank_frame_yields_no_reading_rather_than_an_empty_compound(cal):
    """Fog, snow or a lens cover means the paint cannot be found. Reporting "clear" there
    would tell a traveller the lot was empty on exactly the days it is least likely to be."""
    blank = Image.new("RGB", (320, 240), (200, 200, 200))
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    out = read_frame(buf.getvalue(), buf.getvalue(), cal)
    assert out["compound_visible"] is False
    assert out["fullness"] is None


# --- illumination: the failure that looks most like an answer ---------------------------


def _lit(cal, scale, occupied=()):
    """The same scene at a different brightness, as dusk or floodlight would give."""
    img = Image.new("RGB", (320, 240), tuple(int(180 * scale) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for lane in occupied:
        rows = cal.lane_spans[lane]
        for i, (y, x0, x1) in enumerate(rows):
            if i % 10 >= 7:
                continue
            d.line([(x0, y), (x1 - 1, y)], fill=(40, 40, 45))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_a_darker_frame_is_refused_rather_than_read_as_full(cal):
    """A *bare* floodlit compound at 04:08, differenced against a midday reference, reports
    every lane occupied. Confidently claiming "overflowing" for an empty lot would have
    filled the archive with phantom night overloads."""
    out = read_frame(_lit(cal, 0.65), _lit(cal, 1.0), cal)
    assert out["compound_visible"] is False
    assert out["fullness"] is None
    assert "illumination" in out["notes"]


def test_illumination_is_judged_independently_of_how_full_the_compound_is(cal):
    """The whole difficulty: a packed midday compound and a bare dusk one have similar
    average brightness. Only one of them should be refused."""
    reference = _lit(cal, 1.0)
    packed = illumination_ratio(_lit(cal, 1.0, occupied=cal.lanes), reference, cal)
    dusk_but_empty = illumination_ratio(_lit(cal, 0.65), reference, cal)
    assert packed > ILLUMINATION_BAND[0]
    assert dusk_but_empty < ILLUMINATION_BAND[0]


def test_a_packed_daylight_compound_still_reads(cal):
    out = read_frame(_lit(cal, 1.0, occupied=cal.lanes), _lit(cal, 1.0), cal)
    assert out["compound_visible"] is True
    assert out["fullness"] == "overflowing"


# --- rolling backgrounds ----------------------------------------------------------------


def _bg_from(cal, frames):
    """What build_backgrounds does to a bucket: the per-pixel median."""
    from ferrycast.lanes import _median_image

    paths = []
    for i, data in enumerate(frames):
        p = Path(tempfile.mkdtemp()) / f"{i}.png"
        p.write_bytes(data)
        paths.append(p)
    out = io.BytesIO()
    _median_image(paths).save(out, format="PNG")
    return out.getvalue()


def test_the_median_recovers_pavement_without_being_told_which_frames_were_empty(cal):
    """The property that makes this practical. You would otherwise need a bare reference in
    order to find bare frames, and there is no way in."""
    frames = [_paint(cal, occupied=[]) for _ in range(6)]
    frames += [_paint(cal, occupied=[5]), _paint(cal, occupied=[9]), _paint(cal, occupied=[11])]
    background = _bg_from(cal, frames)

    # A lane occupied in only one of nine frames must not survive into "empty".
    assert occupied_lanes(occupancy(_paint(cal), background, cal)) == []
    assert occupied_lanes(occupancy(_paint(cal, occupied=[9]), background, cal)) == [9]


def test_a_lane_occupied_most_of_the_time_poisons_its_own_background(cal):
    """The documented failure, pinned so it is not discovered again by surprise. Hour 08 sits
    inside the build for the 09:25 sailing, so the compound is busy most mornings — and a
    background built from too few such frames keeps the vehicles and calls them ground."""
    frames = [_paint(cal, occupied=[6]) for _ in range(7)] + [_paint(cal) for _ in range(2)]
    background = _bg_from(cal, frames)

    # Lane 6 is occupied in the frame AND in the background, so it cancels: the reading says
    # clear when it is not. This is why the sample floor matters.
    assert 6 not in occupied_lanes(occupancy(_paint(cal, occupied=[6]), background, cal))


def test_brightness_is_matched_before_differencing(cal):
    """Gating on illumination is not enough. Two frames can pass the gate and still sit far
    enough apart that ordinary asphalt crosses the threshold everywhere — a haze of "changed"
    over lanes that are visibly bare. Measured on a real 08:04 frame, lanes 11 and 12 read
    0.36 and 0.44 on empty pavement before this, and 0.03 and 0.02 after."""
    reference = _paint(cal)
    slightly_dimmer = _lit(cal, 0.88)  # inside the gate, but not identical
    shares = occupancy(slightly_dimmer, reference, cal)
    assert occupied_lanes(shares) == []


def test_a_thin_bucket_is_refused_rather_than_used(tmp_path, cal):
    from ferrycast.lanes import MIN_BACKGROUND_SAMPLES, Background, BackgroundLibrary

    root = tmp_path / "backgrounds" / "SLT"
    root.mkdir(parents=True)
    (root / "08.png").write_bytes(_paint(cal))
    (root / "08.json").write_text(
        json.dumps(
            {
                "terminal": "SLT",
                "hour": 8,
                "samples": MIN_BACKGROUND_SAMPLES - 1,
                "from": "x",
                "to": "y",
                "built_at": "z",
            }
        ),
        encoding="utf-8",
    )
    library = BackgroundLibrary(tmp_path, "SLT")
    assert library.for_hour(8) is None
    assert library.hours() == []
    assert Background(root / "08.png", "SLT", 8, 4, ("x", "y"), "z").thin


def test_an_hour_with_no_background_is_not_read_against_the_wrong_one(tmp_path, cal):
    """A single fixed reference reads a bare floodlit compound at 04:00 as every lane
    occupied. Having no reference for an hour has to mean no reading, not the nearest one."""
    from ferrycast.lanes import BackgroundLibrary

    (tmp_path / "backgrounds" / "SLT").mkdir(parents=True)
    assert BackgroundLibrary(tmp_path, "SLT").for_hour(4) is None


# ------------------------------------------------------------- the scheduler's entry point


def _install_calibration(config):
    import shutil

    cal_dir = Path(config.source_path).parent / "calibration"
    cal_dir.mkdir(exist_ok=True)
    shutil.copyfile(SLT_CAL, cal_dir / "SLT.json")


def _install_background(config, cal, hour):
    from ferrycast.lanes import MIN_BACKGROUND_SAMPLES, backgrounds_dir

    root = backgrounds_dir(config.data_dir, "SLT")
    root.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(_paint(cal))).convert("L").save(root / f"{hour:02d}.png")
    (root / f"{hour:02d}.json").write_text(
        json.dumps(
            {
                "terminal": "SLT",
                "hour": hour,
                "samples": MIN_BACKGROUND_SAMPLES,
                "from": "x",
                "to": "y",
                "built_at": "z",
            }
        ),
        encoding="utf-8",
    )


def _add_frame(conn, config, cal, terminal, local_hour, local_minute):
    """A frame on disk and in the database, captured at a chosen local wall-clock time."""
    from datetime import date, time

    from ferrycast.timeutil import combine_local, iso, local

    at = combine_local(date(2026, 8, 14), time(local_hour, local_minute), config.tz)
    rel = f"{terminal}/{local_hour:02d}{local_minute:02d}.jpg"
    path = Path(config.data_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_paint(cal))
    conn.execute(
        """INSERT INTO frames (route, terminal, captured_at, service_date, path, status)
           VALUES (?, ?, ?, ?, ?, 'ok')""",
        (config.route.id, terminal, iso(at), local(at, config.tz).date().isoformat(), rel),
    )
    conn.commit()


def _geom_observations(conn):
    from ferrycast.lanes import PROMPT_VERSION

    return {
        row["terminal"]: row["n"]
        for row in conn.execute(
            """SELECT f.terminal, COUNT(*) AS n FROM observations o
                 JOIN frames f ON f.id = o.frame_id
                WHERE o.prompt_version = ?
                GROUP BY f.terminal""",
            (PROMPT_VERSION,),
        )
    }


def test_an_uncalibrated_backlog_cannot_starve_a_calibrated_terminal(conn, config, cal):
    """The production failure this exists to prevent. Skipped frames write no observation,
    so they stay pending; with the queue read oldest-first under a hard limit, forty
    Earls Cove frames — a terminal with no lane grid to calibrate — occupied the head of
    the queue and no Saltery Bay frame was read for three days."""
    from ferrycast.lanes import extract_pending

    _install_calibration(config)
    _install_background(config, cal, 10)
    for minute in range(0, 60, 10):  # six older ERL frames head the queue
        _add_frame(conn, config, cal, "ERL", 9, minute)
    _add_frame(conn, config, cal, "SLT", 10, 0)
    _add_frame(conn, config, cal, "SLT", 10, 5)

    stats = extract_pending(conn, config, max_reads=2)

    assert stats.read == 2
    # The uncalibrated terminal is never queried at all, not skipped one frame at a time.
    assert stats.considered == 2
    assert stats.skipped_no_calibration == 0
    assert _geom_observations(conn) == {"SLT": 2}


def test_frames_without_a_background_yet_do_not_eat_the_read_quota(conn, config, cal):
    """No-background is transient — backgrounds build as history accumulates — so those
    frames wait in the queue. Waiting must cost nothing: the cap is on reads, and a run
    that skips five unreadable frames still reads everything it came for."""
    from ferrycast.lanes import extract_pending

    _install_calibration(config)
    _install_background(config, cal, 10)
    for minute in range(0, 50, 10):  # five older frames from an hour with no reference
        _add_frame(conn, config, cal, "SLT", 4, minute)
    for minute in (0, 5, 10):
        _add_frame(conn, config, cal, "SLT", 10, minute)

    stats = extract_pending(conn, config, max_reads=3)

    assert stats.read == 3
    assert stats.skipped_no_background == 5
    assert _geom_observations(conn) == {"SLT": 3}


def test_no_calibrated_terminal_means_no_queue_scan(conn, config, cal):
    from ferrycast.lanes import extract_pending

    _add_frame(conn, config, cal, "SLT", 10, 0)

    stats = extract_pending(conn, config, max_reads=40)

    assert (stats.considered, stats.read) == (0, 0)
