"""Geometric queue-extent reading, for a camera pointed down the approach road.

Two failures matter here and they pull in opposite directions. The first is the one `lanes`
guards too: reporting less queue than there is, which tells someone they would have got on.
The second is peculiar to this camera — it is a kilometre from the compound and can see only
the overflow, so an empty road must never be reported as an empty terminal. Most of what is
pinned below is that second property and the refusals that protect it.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ferrycast.extent import (
    MIN_VEHICLES,
    CalibrationError,
    QueueExtentCalibration,
    illumination_ratio,
    occupancy,
    read_frame,
    tail_row,
)

REPO = Path(__file__).resolve().parents[1]
ERL_CAL = REPO / "config" / "calibration" / "ERL_drivebc244.json"
ERL_BG = REPO / "config" / "calibration" / "ERL_drivebc244_background.jpg"

pytestmark = pytest.mark.skipif(
    not (ERL_CAL.exists() and ERL_BG.exists()),
    reason="Earls Cove highway calibration not present",
)


@pytest.fixture
def cal():
    return QueueExtentCalibration.load(ERL_CAL)


def _road(cal, tail=None, every=1, colours=((40, 40, 45), (105, 105, 110))):
    """The reference road with a queue painted back to `tail`.

    Vehicles alternate two tones, both well clear of asphalt at every depth — the far end
    of the road sits around luma 150 and the near end around 235, so a "pale vehicle" light
    enough to look realistic would be invisible against the near asphalt and the fixture
    would be testing the paint rather than the reader. `every` paints only some rows, which
    is what dappled tree shadow looks like and what the density rule exists to reject.
    """
    img = Image.open(ERL_BG).convert("RGB")
    d = ImageDraw.Draw(img)
    if tail is not None:
        for i, row in enumerate(range(cal.near_row, tail - 1, -1)):
            if i % every:
                continue
            x0, x1 = cal.band(row)
            d.line([(x0, row), (x1 - 1, row)], fill=colours[(i // 9) % len(colours)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_the_shipped_calibration_describes_the_stretch_the_camera_can_measure(cal):
    assert cal.terminal == "ERL"
    assert cal.camera == "drivebc244"
    assert cal.image_size == (480, 288)
    # The visible road holds about ten vehicles. That is the ceiling on anything this camera
    # can say, and it sits just short of the capacity line, so the strongest claim available
    # is a bound rather than a number.
    assert 9.5 < cal.max_vehicles < 11.0
    assert cal.max_vehicles < cal.capacity_vehicles
    # ...but far enough along that saturation already reaches the bottom of the capacity band.
    assert cal.max_vehicles >= cal.capacity_vehicles - cal.capacity_slack


def test_a_clear_road_is_not_reported_as_an_empty_terminal(cal):
    """The failure this module must never commit. This camera cannot see the compound, so
    clear pavement is consistent with a compound anywhere from bare to brim-full."""
    reading = read_frame(ERL_BG, ERL_BG, cal)
    assert reading["vehicle_count"] == 0
    assert reading["fullness"] is None          # not "empty"
    assert "ays nothing about how full the compound is" in reading["notes"]


def test_the_reading_grows_as_the_queue_reaches_further_back(cal):
    readings = [
        read_frame(_road(cal, tail=t), ERL_BG, cal)["vehicles_beyond_camera"]
        for t in (205, 185, 160, 130)
    ]
    assert readings == sorted(readings), readings
    assert readings[0] >= MIN_VEHICLES
    assert readings[-1] > readings[0] + 2


def test_a_queue_on_the_highway_is_read_as_a_full_compound(cal):
    """Vehicles do not stand on Highway 101 while there are lanes free at the terminal, so
    an overflow of any length is already evidence the compound is full. Full and no more:
    the first ten to fifteen vehicles visible here usually make the sailing anyway
    (maintainer, 2026-08-16), and the visible stretch saturates below that, so no queue —
    however long — reads past "heavy". The overload claim belongs to a tail still standing
    after the vessel has gone, not to how far a pre-departure queue reaches."""
    reading = read_frame(_road(cal, tail=190), ERL_BG, cal)
    assert reading["fullness"] == "heavy"
    assert reading["compound_visible"] is False


def test_a_queue_running_off_the_top_of_frame_is_a_bound_not_a_measurement(cal):
    reading = read_frame(_road(cal, tail=cal.far_row), ERL_BG, cal)
    assert reading["queue_extends_beyond_frame"]
    # Still only "heavy": a bound of ~ten vehicles sits below where the boat stops
    # swallowing the road, so even a saturated reading cannot prove an overload.
    assert reading["fullness"] == "heavy"
    assert "at least" in reading["notes"]
    # Marked down against a queue whose end was actually seen: this is a floor on the answer.
    assert reading["confidence"] < read_frame(_road(cal, tail=190), ERL_BG, cal)["confidence"]


def test_a_couple_of_vehicles_at_the_near_edge_are_not_called_an_overflow(cal):
    """Someone stopped on the shoulder looks exactly like the start of a queue, and at this
    end of the scale the difference changes nobody's decision."""
    reading = read_frame(_road(cal, tail=cal.near_row - 12), ERL_BG, cal)
    assert reading["vehicle_count"] == 0
    assert reading["fullness"] is None


def test_dappled_shade_is_not_read_as_a_queue(cal):
    """The confuser that actually fired in the archive. Tree shadow drifting across the
    asphalt lights up alternating rows and never sustains a run; a real queue occupies
    essentially every row behind its tail."""
    shares = occupancy(_road(cal, tail=140, every=2), ERL_BG, cal)
    assert tail_row(shares, cal) is None


def test_a_frame_of_the_wrong_size_is_refused(cal):
    small = Image.new("RGB", (240, 144), (150, 150, 150))
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    with pytest.raises(CalibrationError, match="calibrated at"):
        occupancy(buf.getvalue(), buf.getvalue(), cal)


def test_a_lane_calibration_is_refused(tmp_path):
    """Saltery Bay's camera overlooks the marshalling lanes and has no tail to find. Reading
    it here would report the compound's near edge as a queue extent."""
    other = tmp_path / "SLT_cam1.json"
    other.write_text(json.dumps({"terminal": "SLT", "kind": "lanes"}), encoding="utf-8")
    with pytest.raises(CalibrationError, match="queue_extent"):
        QueueExtentCalibration.load(other)


def test_a_frame_in_different_light_is_refused_rather_than_guessed(cal):
    """Differencing across illumination is not validated, and it fails in the dangerous
    direction: against a midday reference, an empty road at dusk reads as solid queue."""
    dusk = Image.open(ERL_BG).convert("RGB").point(lambda v: int(v * 0.45))
    buf = io.BytesIO()
    dusk.save(buf, format="PNG")
    reading = read_frame(buf.getvalue(), ERL_BG, cal)
    assert reading["fullness"] is None
    assert reading["vehicle_count"] is None
    assert reading["confidence"] == 0.0
    assert "light differs" in reading["notes"]
    assert illumination_ratio(buf.getvalue(), ERL_BG, cal) < 0.85


def test_the_probe_band_stays_inside_the_frame(cal):
    width = cal.image_size[0]
    for row in cal.rows:
        x0, x1 = cal.band(row)
        assert 0 <= x0 < x1 <= width, row
