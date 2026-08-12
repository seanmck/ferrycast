"""Geometric lane reading.

The failure this module exists to prevent is a false "empty": reporting a clear compound
while a queue stands in it. That is the one direction the project must not be wrong in, so
most of what is pinned here is refusal — that an uncalibrated camera, a moved camera, or an
obscured frame produces no answer rather than a confident wrong one.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from ferrycast.lanes import (
    MAX_DRIFT_PX,
    CalibrationError,
    LaneCalibration,
    drift_px,
    fullness_from_lanes,
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


def _paint(cal, occupied=(), size=(320, 240)):
    """Synthesise a frame: bare pavement, with a dark block filling each occupied lane."""
    img = Image.new("RGB", size, (180, 178, 176))
    d = ImageDraw.Draw(img)
    for lane in occupied:
        for y, x0, x1 in cal.lane_spans[lane]:
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
