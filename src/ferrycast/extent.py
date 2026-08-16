"""How far back the queue reaches, from a camera pointed down the approach road.

`lanes` answers "how much of the compound is occupied" by counting lanes at a camera that
overlooks the marshalling area. Earls Cove has no such view worth fitting, and — more to the
point — the question at Earls Cove is a different one. There is no departures board for the
homeward direction and never has been, so nothing free says whether an ERL sailing filled.
What *is* observable is the overflow: when the compound is full, the queue backs out onto
Highway 101, and a highway camera 0.7-0.9 km down the road sees it coming.

That inverts the usual reading. A queue standing on the highway is not a measure of how busy
the terminal is; it is proof the terminal has already run out of room, because cars only
spill onto the road once the lanes are full. Full is still not *over*: the maintainer's own
count (2026-08-16) is that the first ten to fifteen vehicles visible here usually make the
sailing anyway, so how far a pre-departure queue reaches decides nothing — the claim this
camera can settle is a tail still standing after the vessel has gone.

Three things follow from the geometry rather than from any judgement about the scene:

* **Position, not count.** Segmenting a queue into vehicles is fragile — an RV is three cars
  long and a towed trailer is ambiguous — so the tail's *position* is measured and divided by
  a mean headway. That degrades gracefully: a queue of unusual vehicles shifts the estimate a
  little, where a miscount would drop or invent a whole vehicle.
* **An empty road is not an empty terminal.** This camera cannot see the compound. Clear
  pavement here means the queue has not overflowed, which is consistent with a compound
  anywhere from bare to brim-full. `fullness` is therefore left unknown, never "empty" — a
  false all-clear is the worst answer this project can give.
* **It saturates below the fill boundary.** The visible stretch holds only about ten
  vehicles, and the boat usually swallows more than that from this road, so no reading here
  can prove "more than a boatload": the band caps at "heavy". Once the tail leaves the top
  of frame the honest answer is a bound ("at least this many"), not a number, and
  `read_frame` says so.

Calibration lives in `config/calibration/<TERMINAL>_<CAMERA>.json` with `kind` set to
`queue_extent`, and is fitted per camera at one pan and tilt. Unlike the compound camera
there is no lane paint to re-measure, so drift cannot be detected from the frame itself; the
guard here is the illumination gate plus the density test below, both of which fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .lanes import _luma  # same pure-Pillow greyscale reader; no reason to have two

#: Share of a probe band that must differ from the reference before that row counts as
#: holding a vehicle. Fitted against the 2026-08-14 replay day: real queues sit at 0.7-1.0
#: across their length, while the worst confuser — tree shadow drifting across the asphalt —
#: produces a patchy 0.3-0.6.
OCCUPIED_SHARE = 0.60

#: Per-pixel luma difference that counts as "not the same as the empty road".
DIFF_THRESHOLD = 30

#: What share of the rows between the tail and the near edge must read occupied. This is the
#: test that separates a queue from shadow: a queue is *continuous* — every row behind the
#: tail has a vehicle in it — whereas dappled shade under moving trees lights up alternating
#: rows and never sustains a run. Contiguity alone was tried first and is not enough; it
#: cannot tell a real inter-vehicle gap from a gap in the evidence.
RUN_DENSITY = 0.70

#: Rows of evidence needed before any tail is reported. Below this a single passing vehicle
#: in the frame could pass the density test on its own.
MIN_RUN_ROWS = 8

#: A queue shorter than this is not reported at all. A few vehicles at the near edge is as
#: likely to be someone pulled onto the shoulder as an overflow — and, on this camera, as
#: likely again to be sun. Looking south into a low afternoon sun it throws a lens flare
#: clean across the near carriageway, which brightens the probe band the way a pale vehicle
#: would; on the day this was fitted that cost one reading of exactly 3.0 vehicles on bare
#: asphalt. Nothing in this range changes a decision anyway, with capacity at 12 or 13.
MIN_VEHICLES = 4.0

#: How far this frame's illumination may sit from the reference's before differencing them
#: stops meaning anything. Same rationale as `lanes.ILLUMINATION_BAND`, and the same failure
#: it guards against: differenced against midday, an empty road at dusk reads as solid queue.
ILLUMINATION_BAND = (0.85, 1.20)


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class QueueExtentCalibration:
    """Where the queue stands, and how far back each row of the image is.

    The distance model is the flat-ground pinhole relation, which puts equally spaced ground
    stations at rows `origin - scale / (row - horizon)`. It is written in units of *vehicles*
    rather than metres on purpose: metres would need a survey to anchor, while vehicles can
    be fitted from a photograph of a standing queue, which is the one thing this camera
    reliably supplies. Headway is carried alongside for anyone who wants the distance back.
    """

    terminal: str
    camera: str
    image_size: tuple[int, int]
    #: Left flank of the queue as a quadratic in row: c0 + c1*row + c2*row^2. Vehicles are
    #: probed along their near edge against the shoulder, which is the part of them that no
    #: oncoming traffic in the other lane can ever project onto.
    flank: tuple[float, float, float]
    #: Probe band: offset from the flank, and its width at `near_row`. The width is scaled
    #: with depth, since a fixed pixel count covers more ground the further off it is.
    band_offset: int
    band_width: int
    #: A strip of the far carriageway, clear of the queue, used only to measure the light.
    #: Reading brightness off the probe band instead is circular and fails in the dangerous
    #: direction: a queue of pale vehicles drags the band's own statistic up, the frame is
    #: scaled down to match the reference, and the pale vehicles difference away to nothing.
    #: Offset and width are from the outer edge of the band, and scale with depth as it does.
    light_offset: int
    light_width: int
    #: Rows nearer than this only — further off, the carriageway is too narrow to sample
    #: without running into the treeline.
    light_from_row: int
    #: Rows the model is fitted over. Beyond `far_row` the road bends out of sight and the
    #: station spacing collapses below one row per vehicle, so nothing there is resolvable.
    far_row: int
    near_row: int
    #: Where the road passes the camera's own station. Distances are measured from here, so
    #: "vehicles beyond the camera" means exactly what it says on the map.
    camera_row: float
    #: Station model: row(k) = horizon + scale / (origin - k).
    horizon: float
    scale: float
    origin: float
    #: Vehicles past the camera at which the vessel was first believed full, kept for the
    #: fitting record. Local knowledge has since moved the boundary out of reach: the first
    #: ten to fifteen vehicles visible here usually board (maintainer, 2026-08-16), which
    #: is more than the visible stretch holds — so no reading is compared against this line
    #: any more, and the band caps at "heavy". `capacity_slack` is the width of the band
    #: the original figure was given as.
    capacity_vehicles: float
    capacity_slack: float
    #: Metres per vehicle, for converting a reading back into a distance. Not used in the
    #: reading itself.
    headway_m: float = 6.5
    fitted_from: str = ""

    @classmethod
    def load(cls, path: str | Path) -> QueueExtentCalibration:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("kind") != "queue_extent":
            raise CalibrationError(
                f"{path} is a {raw.get('kind')!r} calibration; this reader wants "
                "'queue_extent'. Cameras differ — a compound camera is read by `lanes`, "
                "which counts lanes, and has no tail to find."
            )
        return cls(
            terminal=raw["terminal"],
            camera=raw["camera"],
            image_size=tuple(raw["image_size"]),
            flank=tuple(float(v) for v in raw["flank"]),
            band_offset=int(raw["band_offset"]),
            band_width=int(raw["band_width"]),
            light_offset=int(raw["light_offset"]),
            light_width=int(raw["light_width"]),
            light_from_row=int(raw["light_from_row"]),
            far_row=int(raw["far_row"]),
            near_row=int(raw["near_row"]),
            camera_row=float(raw["camera_row"]),
            horizon=float(raw["horizon"]),
            scale=float(raw["scale"]),
            origin=float(raw["origin"]),
            capacity_vehicles=float(raw["capacity_vehicles"]),
            capacity_slack=float(raw["capacity_slack"]),
            headway_m=float(raw.get("headway_m", 6.5)),
            fitted_from=raw.get("fitted_from", ""),
        )

    @property
    def rows(self) -> range:
        return range(self.far_row, self.near_row + 1)

    def _depth(self, row: float) -> float:
        return (row - self.horizon) / (self.near_row - self.horizon)

    def _clip(self, x0: int, x1: int) -> tuple[int, int]:
        w = self.image_size[0]
        return max(0, min(w, x0)), max(0, min(w, x1))

    def band(self, row: int) -> tuple[int, int]:
        """The pixels probed on one row: a window hugging the queue's left flank."""
        c0, c1, c2 = self.flank
        x0 = int(c0 + c1 * row + c2 * row * row) + self.band_offset
        width = max(8, int(self.band_width * self._depth(row)))
        return self._clip(x0, x0 + width)

    def light_span(self, row: int) -> tuple[int, int] | None:
        """Where to read the light on this row, clear of anything the queue stands on."""
        if row < self.light_from_row:
            return None
        depth = self._depth(row)
        x0 = self.band(row)[1] + int(self.light_offset * depth)
        x0, x1 = self._clip(x0, x0 + int(self.light_width * depth))
        return (x0, x1) if x1 - x0 >= 8 else None

    def vehicles_at(self, row: float) -> float:
        """Vehicles standing between the camera and `row`."""
        return self._station(self.camera_row) - self._station(row)

    def _station(self, row: float) -> float:
        return self.origin - self.scale / (row - self.horizon)

    @property
    def max_vehicles(self) -> float:
        """The longest queue this camera can put a number on."""
        return self.vehicles_at(self.far_row)

    def metres(self, vehicles: float) -> float:
        return vehicles * self.headway_m


def occupancy(
    frame: str | Path | bytes,
    background: str | Path | bytes,
    cal: QueueExtentCalibration,
    *,
    threshold: int = DIFF_THRESHOLD,
) -> dict[int, float]:
    """Share of each row's probe band that differs from the empty-road reference.

    `background` must be a frame of the *same* camera with the road clear and the sun in
    much the same place. That second condition is not decoration: measured over a full replay
    day, referencing against a clear frame within 45 minutes gave no false queue in 59
    frames, while reaching two hours across the sun's arc invented one about every ten. Tree
    shadow on asphalt moves faster than the light does.
    """
    fg, fw, fh = _luma(frame)
    bg, bw, bh = _luma(background)
    if (fw, fh) != (bw, bh):
        raise CalibrationError(f"frame is {fw}x{fh} but background is {bw}x{bh}")
    if (fw, fh) != tuple(cal.image_size):
        raise CalibrationError(
            f"frame is {fw}x{fh} but {cal.terminal}/{cal.camera} was calibrated at "
            f"{cal.image_size[0]}x{cal.image_size[1]}"
        )

    scale = _brightness_scale(fg, bg, cal, fw)
    out: dict[int, float] = {}
    for row in cal.rows:
        x0, x1 = cal.band(row)
        base = row * fw
        changed = total = 0
        for i in range(base + x0, base + x1):
            total += 1
            if abs(min(255, int(fg[i] * scale)) - bg[i]) > threshold:
                changed += 1
        out[row] = (changed / total) if total else 0.0
    return out


def _light_pixels(cal: QueueExtentCalibration, width: int) -> list[int]:
    out = []
    for row in cal.rows:
        span = cal.light_span(row)
        if span:
            out.extend(row * width + x for x in range(*span))
    return out


def _p90(values: list[int], idx: list[int]) -> float:
    """The 90th percentile of the sampled pixels — sunlit asphalt, whatever else is going on.

    A high percentile rather than a median because shadow, not occupancy, is what moves this
    statistic on the far carriageway: a shadow darkens most of the strip and would drag a
    median with it, where the top decile stays on whatever is still in the sun.
    """
    if not idx:
        return 0.0
    sample = sorted(values[i] for i in idx)
    return float(sample[int(0.90 * (len(sample) - 1))])


def _brightness_scale(fg, bg, cal: QueueExtentCalibration, width: int) -> float:
    """Factor bringing the frame's road brightness onto the reference's."""
    idx = _light_pixels(cal, width)
    f = _p90(fg, idx)
    return (_p90(bg, idx) / f) if f else 1.0


def illumination_ratio(
    frame: str | Path | bytes,
    background: str | Path | bytes,
    cal: QueueExtentCalibration,
) -> float:
    """How this frame's light compares with the reference's, ignoring how busy the road is.

    Read off the far carriageway rather than the probe band, so the number is about the light
    and nothing else. Measured across a full replay day it tracks the sun and the cloud —
    154 at dawn, 233-239 through the middle of the day, 183 under passing shade — and does
    not budge when a queue arrives (237, 238, 238, 239 across the four frames of one).
    """
    fpx, fw, _ = _luma(frame)
    bpx, _, _ = _luma(background)
    idx = _light_pixels(cal, fw)
    b = _p90(bpx, idx)
    return (_p90(fpx, idx) / b) if b else 0.0


def tail_row(shares: dict[int, float], cal: QueueExtentCalibration) -> int | None:
    """The furthest row the queue can be shown to reach, or None if there is no queue.

    Walks outward from the near edge and keeps the furthest row at which the rows behind it
    are *mostly* occupied. Density rather than contiguity, because a real queue has real gaps
    in it — a car length between vehicles, a motorcycle, a gap the camera cannot resolve —
    and a rule that stops at the first gap systematically reports the queue as shorter than
    it is, which is the direction that tells someone they would have got on.
    """
    found = None
    hits = seen = 0
    for row in range(cal.near_row, cal.far_row - 1, -1):
        seen += 1
        if shares.get(row, 0.0) > OCCUPIED_SHARE:
            hits += 1
        if seen >= MIN_RUN_ROWS and hits / seen >= RUN_DENSITY:
            found = row
    return found


def read_frame(
    frame: str | Path,
    background: str | Path,
    cal: QueueExtentCalibration,
) -> dict:
    """One frame, read geometrically. Same shape of answer as a `vision` observation.

    `compound_visible` is always false: this camera is a kilometre from the compound and has
    never seen it. What it can say is carried by `vehicle_count` — vehicles standing between
    the camera and the tail — and by `fullness`, which is deliberately left None when the road
    is clear. An empty highway is not an empty terminal, and saying so would be the one error
    that costs somebody a sailing.
    """
    lit = illumination_ratio(frame, background, cal)
    low, high = ILLUMINATION_BAND
    if not (low <= lit <= high):
        return _refusal(
            f"light differs from the reference (x{lit:.2f}); differencing across "
            "illumination is not validated",
            lit,
        )

    shares = occupancy(frame, background, cal)
    row = tail_row(shares, cal)
    if row is None:
        return {
            "compound_visible": False,
            "fullness": None,
            "vehicle_count": 0,
            "lanes_occupied": None,
            "queue_extends_beyond_frame": False,
            "confidence": 0.9,
            "notes": (
                "road clear past the camera — the queue has not overflowed onto the "
                "highway. Says nothing about how full the compound is."
            ),
            "drift_px": None,
            "illumination_ratio": round(lit, 3),
            "vehicles_beyond_camera": 0.0,
            "tail_row": None,
        }

    vehicles = cal.vehicles_at(row)
    if vehicles < MIN_VEHICLES:
        return {
            "compound_visible": False,
            "fullness": None,
            "vehicle_count": 0,
            "lanes_occupied": None,
            "queue_extends_beyond_frame": False,
            "confidence": 0.5,
            "notes": (
                f"{vehicles:.1f} vehicles at the near edge — too few to tell an overflow "
                "from someone stopped on the shoulder"
            ),
            "drift_px": None,
            "illumination_ratio": round(lit, 3),
            "vehicles_beyond_camera": round(vehicles, 1),
            "tail_row": row,
        }

    saturated = row <= cal.far_row
    if saturated:
        notes = (
            f"queue runs past the top of frame — at least {vehicles:.1f} vehicles beyond "
            "the camera"
        )
    else:
        notes = (
            f"{vehicles:.1f} vehicles beyond the camera "
            f"(~{cal.metres(vehicles):.0f} m of queue on the highway)"
        )

    return {
        "compound_visible": False,
        # A queue on the highway is proof the compound is full — cars do not stand on
        # Highway 101 while there are lanes free. It is NOT proof anyone will be left:
        # the maintainer's own count (2026-08-16) is that the first ten to fifteen
        # vehicles visible here usually still make it aboard, and the visible stretch
        # saturates below that, so the fill boundary lies past everything this camera can
        # measure. "heavy" is therefore both the weakest and the strongest honest band —
        # the left-behind claim belongs to a tail still standing after the vessel has
        # gone, not to how far a pre-departure queue reaches.
        "fullness": "heavy",
        "vehicle_count": int(round(vehicles)),
        "lanes_occupied": None,
        "queue_extends_beyond_frame": saturated,
        # Deterministic given the calibration. The uncertainty that remains is whether the
        # calibration still holds and whether the tail is really the tail, so a reading that
        # ran off the top of frame is marked down: it is a bound, not a measurement.
        "confidence": 0.7 if saturated else 0.9,
        "notes": notes,
        "drift_px": None,
        "illumination_ratio": round(lit, 3),
        "vehicles_beyond_camera": round(vehicles, 1),
        "tail_row": row,
    }


def _refusal(reason: str, lit: float) -> dict:
    return {
        "compound_visible": False,
        "fullness": None,
        "vehicle_count": None,
        "lanes_occupied": None,
        "queue_extends_beyond_frame": False,
        "confidence": 0.0,
        "notes": reason,
        "drift_px": None,
        "illumination_ratio": round(lit, 3),
        "vehicles_beyond_camera": None,
        "tail_row": None,
    }


def calibration_path(config_dir: str | Path, terminal: str, camera: str) -> Path:
    from .lanes import calibration_stem

    return Path(config_dir) / "calibration" / f"{calibration_stem(terminal, camera)}.json"


def load_for(
    config_dir: str | Path, terminal: str, camera: str
) -> QueueExtentCalibration | None:
    path = calibration_path(config_dir, terminal, camera)
    return QueueExtentCalibration.load(path) if path.exists() else None


PROMPT_VERSION = "extent-v1"
MODEL = "queue-extent"


def extract_frames(conn, config, frames, *, dry_run: bool = False, max_reads: int | None = None):
    """Read highway frames geometrically and store them as observations.

    Same table and same discipline as `lanes.extract_frames` — its own prompt version, no
    cost, `max_reads` capping reads rather than frames considered so a skip cannot eat the
    budget. Two deliberate differences in what gets stored:

    * `vehicle_count` is always NULL. The reader's count is vehicles standing on the
      *highway*, and aggregation treats a stored count as a compound residual — a clear
      road stored as `0` would have classified the sailing `boarded` off a camera that
      cannot see whether anyone boarded. The number survives in `raw` for calibration work.
    * A clear road is stored usable but with no claims (`fullness` NULL): the reading is
      trustworthy, it just attests nothing about the compound, and aggregation must see
      silence rather than "empty". Only a refusal — light the differencing is not
      validated across — is unusable.
    """
    from .lanes import BackgroundLibrary, LaneStats
    from .timeutil import iso, local, now_utc, parse_iso

    stats = LaneStats()
    if not frames or dry_run:
        stats.considered = len(frames)
        return stats

    config_dir = Path(config.source_path).parent
    cals: dict[tuple[str, str], QueueExtentCalibration | None] = {}
    libraries: dict[tuple[str, str], BackgroundLibrary] = {}
    backgrounds: dict[tuple[str, str, int], object] = {}

    for frame in frames:
        if max_reads is not None and stats.read >= max_reads:
            break
        stats.considered += 1
        key = (frame["terminal"], frame["camera"])
        if key not in cals:
            cals[key] = load_for(config_dir, *key)
            libraries[key] = BackgroundLibrary(config.data_dir, *key)
        cal = cals[key]
        if cal is None:
            stats.skipped_no_calibration += 1
            continue

        # The reference must match the hour — this camera's docstring is stricter than the
        # compound one's about how far the sun may move, and the per-hour library is what
        # satisfies it. An hour with no reference yet waits rather than differencing badly.
        hour = local(parse_iso(frame["captured_at"]), config.tz).hour
        if key + (hour,) not in backgrounds:
            backgrounds[key + (hour,)] = libraries[key].for_hour(hour)
        background = backgrounds[key + (hour,)]
        if background is None:
            stats.skipped_no_background += 1
            continue

        path = Path(config.data_dir) / frame["path"]
        if not path.exists():
            stats.failed += 1
            stats.errors.append(f"frame {frame['id']}: file missing at {path}")
            continue
        try:
            parsed = read_frame(path, background.path, cal)
        except Exception as exc:
            stats.failed += 1
            stats.errors.append(f"frame {frame['id']}: {exc}")
            continue

        usable = parsed["vehicle_count"] is not None
        conn.execute(
            """INSERT OR REPLACE INTO observations
                   (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                    queue_beyond_frame, ferry_at_dock, visibility, fullness, confidence,
                    usable, notes, input_tokens, output_tokens, cost_usd, created_at, raw)
               VALUES (?, ?, ?, NULL, NULL, ?, 0, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
            (
                frame["id"],
                PROMPT_VERSION,
                MODEL,
                int(bool(parsed["queue_extends_beyond_frame"])),
                "clear" if usable else "obscured",
                parsed["fullness"],
                parsed["confidence"],
                int(usable),
                parsed["notes"] or None,
                iso(now_utc()),
                json.dumps(parsed, separators=(",", ":")),
            ),
        )
        stats.read += 1
        if not usable:
            stats.unusable += 1
    conn.commit()
    return stats
