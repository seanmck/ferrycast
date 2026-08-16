"""Per-lane occupancy from a calibrated fixed camera.

A terminal camera never moves, so the lane geometry is a constant of the installation
rather than something to re-derive from every frame. Fit it once, and reading a frame stops
being a perception problem: each lane is a known set of pixels, and the question is whether
those pixels differ from the same lane when the compound was empty.

Why this exists alongside `vision`:

* **It cannot hallucinate a lane.** Asking a model "which lane is that vehicle in" failed in
  a specific and dangerous way — on some frames it reported only the lanes whose numbers are
  painted in view and declared the compound empty while a queue was plainly standing in the
  unnumbered ones. A false "empty" is the worst answer this project can give, because it
  tells someone they would have got on. Geometry removes the failure mode rather than
  reducing its frequency.
* **It is free.** No API call, so it can run over every frame rather than the four per
  sailing the budget allows.
* **It measures capacity, not crowding.** Empty lanes are direct evidence of room left, which
  is the question a traveller is actually asking.

What it does not do is judge. Fog, glare, snow and roadworks are all cases where a model
reading the scene is worth paying for. This handles the ordinary case cheaply and leaves the
awkward ones to `vision`.

Calibration lives in `config/calibration/<TERMINAL>.json` and is per camera, per pan/tilt. If
the camera is nudged the geometry is silently wrong, so `drift_px` re-measures the painted
lines against the fitted ones and callers are expected to check it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .config import MAIN_CAMERA

#: A lane counts as occupied when this share of its visible pavement differs from the
#: empty reference. Well clear of both the noise floor (~0.06 on a bare compound) and the
#: level a single vehicle produces in the smallest lanes (~0.13).
OCCUPIED_SHARE = 0.15

#: Per-pixel luma difference that counts as "not the same as the empty compound".
DIFF_THRESHOLD = 22

#: Fitted lane lines this far from the painted ones mean the camera has moved and the
#: calibration can no longer be trusted.
MAX_DRIFT_PX = 3.0

#: How far a frame's illumination may sit from the background reference's before
#: differencing them stops meaning anything. Measured on real frames: midday sits at
#: 0.96-0.97 whether the compound is bare, building or packed, while every evening, dusk,
#: dawn and night frame falls between 0.59 and 0.73. The band below is deliberately wider
#: than the daylight spread and still nowhere near the night cluster.
ILLUMINATION_BAND = (0.85, 1.20)


class CalibrationError(Exception):
    pass


@dataclass(frozen=True)
class LaneCalibration:
    """Where each numbered lane is, in pixels, for one camera at one pan and tilt."""

    terminal: str
    image_size: tuple[int, int]
    #: lane number -> [(row, x_start, x_end), ...], already clipped to visible pavement.
    lane_spans: dict[int, list[tuple[int, int, int]]]
    vanishing_point: tuple[float, float]
    y_ref: float
    mobius: tuple[float, float, float]
    fitted_from: str = ""
    #: Whether the calibrated lanes account for the whole compound.
    #:
    #: True at Saltery Bay, where the camera looks across the apron and what is not in a
    #: calibrated lane is not in the compound. False at Earls Cove, where it looks *away*
    #: from the dock along four lanes, only two of which have paint clear enough to fit —
    #: so most of the compound, and the lane the arriving queue actually forms in, are out
    #: of frame. The distinction decides whether bare pavement means "empty" or means
    #: nothing at all, and getting that wrong is the false all-clear this module exists to
    #: prevent. Defaults True so a calibration that has not thought about it behaves as
    #: before; set it false deliberately.
    covers_compound: bool = True

    @property
    def lanes(self) -> list[int]:
        return sorted(self.lane_spans)

    def area(self, lane: int) -> int:
        return sum(x1 - x0 for _, x0, x1 in self.lane_spans.get(lane, ()))

    def boundary_x(self, k: float, y: float) -> float:
        """x of the line between lane k-1 and lane k, at row y."""
        a, b, c = self.mobius
        xv, yv = self.vanishing_point
        x_at_ref = (a * k + b) / (c * k + 1.0)
        return xv + (x_at_ref - xv) * ((y - yv) / (self.y_ref - yv))

    @classmethod
    def load(cls, path: str | Path) -> LaneCalibration:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("kind") != "lanes":
            raise CalibrationError(
                f"{path} is a {raw.get('kind')!r} calibration; this reader wants 'lanes'. "
                "Cameras differ — a 'queue_extent' calibration belongs to one pointed down "
                "the approach road, where there is no lane grid and the measurement is how "
                "far back the tail reaches. `extent` reads those."
            )
        spans = {
            int(k): [(int(r[0]), int(r[1]), int(r[2])) for r in v]
            for k, v in raw["lane_spans"].items()
        }
        return cls(
            terminal=raw["terminal"],
            image_size=tuple(raw["image_size"]),
            lane_spans=spans,
            vanishing_point=tuple(raw["vanishing_point"]),
            y_ref=float(raw["y_ref"]),
            mobius=tuple(raw["mobius"]),
            fitted_from=raw.get("fitted_from", ""),
            covers_compound=bool(raw.get("covers_compound", True)),
        )


def _luma(path_or_bytes) -> tuple[list[int], int, int]:
    from io import BytesIO

    from PIL import Image

    src = BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
    with Image.open(src) as img:
        grey = img.convert("L")
        return list(grey.tobytes()), grey.width, grey.height


def occupancy(
    frame: str | Path | bytes,
    background: str | Path | bytes,
    cal: LaneCalibration,
    *,
    threshold: int = DIFF_THRESHOLD,
) -> dict[int, float]:
    """Share of each lane's visible pavement that differs from the empty reference.

    `background` must be a frame of the *same* camera with the compound empty and in
    comparable light. Differencing across very different illumination — a night frame against
    a midday reference — is not something this has been shown to survive.
    """
    fg, fw, fh = _luma(frame)
    bg, bw, bh = _luma(background)
    if (fw, fh) != (bw, bh):
        raise CalibrationError(f"frame is {fw}x{fh} but background is {bw}x{bh}")
    if (fw, fh) != tuple(cal.image_size):
        raise CalibrationError(
            f"frame is {fw}x{fh} but {cal.terminal} was calibrated at "
            f"{cal.image_size[0]}x{cal.image_size[1]}"
        )

    # Match the frame's overall brightness to the reference before comparing. Gating on
    # illumination is not enough: two frames can pass the gate and still sit far enough apart
    # that ordinary asphalt crosses the threshold everywhere, which shows up as a haze of
    # "changed" pixels over lanes that are visibly bare. Scaling removes the offset and
    # leaves only differences in what is actually standing on the ground.
    scale = _brightness_scale(fg, bg, cal, fw)

    out: dict[int, float] = {}
    for lane, spans in cal.lane_spans.items():
        changed = total = 0
        for y, x0, x1 in spans:
            base = y * fw
            for i in range(base + x0, base + x1):
                total += 1
                if abs(min(255, int(fg[i] * scale)) - bg[i]) > threshold:
                    changed += 1
        out[lane] = (changed / total) if total else 0.0
    return out


def _brightness_scale(fg, bg, cal: LaneCalibration, width: int) -> float:
    """Factor bringing the frame's pavement brightness onto the reference's."""
    idx = [
        y * width + x for spans in cal.lane_spans.values() for y, x0, x1 in spans
        for x in range(x0, x1)
    ]
    f = sorted(fg[i] for i in idx)
    b = sorted(bg[i] for i in idx)
    if not f:
        return 1.0
    k = int(0.90 * (len(f) - 1))
    return (b[k] / f[k]) if f[k] else 1.0


def _lane_percentile(frame: str | Path | bytes, cal: LaneCalibration, q: float) -> float:
    px, w, _ = _luma(frame)
    vals = sorted(
        px[y * w + x] for spans in cal.lane_spans.values() for y, x0, x1 in spans
        for x in range(x0, x1)
    )
    return float(vals[int(q * (len(vals) - 1))]) if vals else 0.0


def illumination_ratio(
    frame: str | Path | bytes, background: str | Path | bytes, cal: LaneCalibration
) -> float:
    """How this frame's light compares with the reference's, ignoring how full it is.

    Uses the 90th percentile of lane brightness rather than the median, because the median
    tracks occupancy as much as illumination — a packed midday compound and a bare dusk one
    both sit near 0.7 of the reference, and telling those apart is the entire job. The top
    decile is asphalt highlights and paint, which stay bright however many vehicles are
    parked on the rest of it.
    """
    ref = _lane_percentile(background, cal, 0.90)
    return (_lane_percentile(frame, cal, 0.90) / ref) if ref else 0.0


def occupied_lanes(shares: dict[int, float], *, cutoff: float = OCCUPIED_SHARE) -> list[int]:
    return sorted(lane for lane, share in shares.items() if share > cutoff)


def fullness_from_lanes(shares: dict[int, float], cal: LaneCalibration) -> str | None:
    """Map per-lane occupancy onto the band vocabulary the archive already speaks.

    Deliberately weighted by lane count rather than by area. A compound with two of ten lanes
    in use has eight lanes of room whether or not those two happen to be the ones nearest the
    camera, and it is the room that decides whether the next arrival gets on.

    Where the calibration does not cover the whole compound the scale is one-sided, and
    returns None rather than "empty" for bare lanes. Earls Cove is why: the camera faces away
    from the dock down the tail ends of the lanes, which are the last pavement to fill, and
    the queue arriving forms in a lane that is not fitted. Clear pavement there is consistent
    with a compound anywhere from bare to nearly full — measured on a real afternoon queue,
    five vehicles stood in shot while both calibrated lanes read clear. Occupancy still means
    something, and means it strongly: vehicles at the far end are only there once everything
    ahead of them is taken.
    """
    lanes = cal.lanes
    used = len(occupied_lanes(shares))
    if not cal.covers_compound:
        if not lanes or used == 0:
            return None
        return "overflowing" if used == len(lanes) else "heavy"
    if not lanes:
        return "empty"
    if used == 0:
        return "empty"
    share = used / len(lanes)
    if share <= 0.25:
        return "light"
    if share <= 0.55:
        return "moderate"
    if share < 1.0:
        return "heavy"
    return "overflowing"


def queue_reaches_last_visible_lane(shares: dict[int, float], cal: LaneCalibration) -> bool:
    """Whether the lowest-numbered lane in view holds vehicles.

    At Saltery Bay the compound continues past the left edge of frame, so this is the honest
    form of "the queue may run further than we can see" — unlike a flag that fires whenever
    anything stands in the nearest lane.
    """
    lanes = cal.lanes
    return bool(lanes) and shares.get(lanes[0], 0.0) > OCCUPIED_SHARE


def drift_px(frame: str | Path | bytes, cal: LaneCalibration, *, search: int = 6) -> float:
    """How far the painted lane lines sit from where the calibration expects them.

    A fixed camera is an assumption, not a guarantee: a knock or a maintenance visit re-aims
    it and every lane number silently shifts. This re-finds the brightest pixel near each
    predicted boundary and reports the largest offset, so a caller can refuse to trust a
    calibration that has come adrift instead of quietly mislabelling lanes.
    """
    px, w, h = _luma(frame)
    offsets: list[float] = []
    # Above the painted lane numbers. The digits are far brighter and wider than the lines,
    # so a search window that includes them locks onto a numeral and reports drift that is
    # not there — measured 5-6px on a calibration whose true residual is under 1px.
    rows = [y for y in range(int(cal.y_ref) - 70, int(cal.y_ref) - 34) if 0 <= y < h]
    for k in cal.lanes:
        for y in rows:
            expected = cal.boundary_x(k, y)
            lo, hi = int(expected) - search, int(expected) + search + 1
            if lo < 1 or hi >= w:
                continue
            best_x, best_v = None, -1
            for x in range(lo, hi):
                v = px[y * w + x]
                if v > best_v:
                    best_v, best_x = v, x
            neighbourhood = [px[y * w + x] for x in range(lo, hi)]
            typical = sorted(neighbourhood)[len(neighbourhood) // 2]
            if best_v - typical < 8:  # no paint found here; nothing to measure against
                continue
            offsets.append(abs(best_x - expected))
    if len(offsets) < 8:
        # Too little paint found to say anything. Report unusable rather than "no drift",
        # so a fogged or snow-covered frame cannot pass a check it was never able to run.
        return float("inf")
    offsets.sort()
    return offsets[len(offsets) // 2]


def calibration_stem(terminal: str, camera: str = MAIN_CAMERA) -> str:
    """How a camera names its files. The main one is bare, so nothing existing moves."""
    return terminal if camera == MAIN_CAMERA else f"{terminal}_{camera}"


def calibration_path(
    config_dir: str | Path, terminal: str, camera: str = MAIN_CAMERA
) -> Path:
    return Path(config_dir) / "calibration" / f"{calibration_stem(terminal, camera)}.json"


def background_path(
    config_dir: str | Path, terminal: str, camera: str = MAIN_CAMERA
) -> Path:
    stem = calibration_stem(terminal, camera)
    return Path(config_dir) / "calibration" / f"{stem}_background.jpg"


def load_for(
    config_dir: str | Path, terminal: str, camera: str = MAIN_CAMERA
) -> LaneCalibration | None:
    path = calibration_path(config_dir, terminal, camera)
    return LaneCalibration.load(path) if path.exists() else None


def cameras_with_calibration(config_dir: str | Path, terminal) -> list[str]:
    """Which of a terminal's cameras have a calibration to be read against.

    The gate on building a reference: a camera without a calibration gets no background,
    because the reference would be built, stored, and never differenced against anything.
    Kept here rather than at each call site so the scheduler and the CLI cannot drift into
    disagreeing about which cameras exist — they did, and the one running in production was
    the one that had never heard of the second camera.
    """
    from .extent import load_for as load_extent

    found = []
    if load_for(config_dir, terminal.code) is not None:
        found.append(MAIN_CAMERA)
    for camera in terminal.cameras:
        loader = load_extent if camera.kind == "queue_extent" else load_for
        if loader(config_dir, terminal.code, camera.id) is not None:
            found.append(camera.id)
    return found


PROMPT_VERSION = "geom-v1"
MODEL = "lane-geometry"


@dataclass
class LaneStats:
    considered: int = 0
    read: int = 0
    unusable: int = 0
    skipped_no_calibration: int = 0
    skipped_no_background: int = 0
    failed: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def absorb(self, other: LaneStats) -> None:
        """Fold another sweep's counts into this one — the capture job reads several
        cameras in one run and reports them as one job outcome."""
        self.considered += other.considered
        self.read += other.read
        self.unusable += other.unusable
        self.skipped_no_calibration += other.skipped_no_calibration
        self.skipped_no_background += other.skipped_no_background
        self.failed += other.failed
        self.errors.extend(other.errors)


def read_frame(
    frame: str | Path,
    background: str | Path,
    cal: LaneCalibration,
) -> dict:
    """One frame, read geometrically. Same shape of answer as a `vision` observation.

    `usable` is false when the calibration cannot be trusted for this frame — the camera has
    moved, or there is too little paint visible to tell. Refusing to answer is the point: an
    unusable frame is excluded from aggregation, where a confidently wrong lane assignment
    would not be.
    """
    # Illumination first, because it is the failure that looks most like a real answer.
    # Differenced against a midday reference, a *bare* floodlit compound at 04:08 reports
    # every lane occupied — a confident "overflowing" for an empty lot. Refusing costs a
    # reading; not refusing would have filled the archive with phantom night overloads.
    lit = illumination_ratio(frame, background, cal)
    low, high = ILLUMINATION_BAND
    if not (low <= lit <= high):
        return {
            "compound_visible": False,
            "fullness": None,
            "vehicle_count": None,
            "lanes_occupied": None,
            "queue_extends_beyond_frame": False,
            "confidence": 0.0,
            "notes": (
                f"light differs from the reference (x{lit:.2f}); differencing across "
                "illumination is not validated"
            ),
            "drift_px": None,
            "illumination_ratio": round(lit, 3),
            "lane_shares": {},
        }

    drift = drift_px(frame, cal)
    if drift > MAX_DRIFT_PX:
        reason = (
            "no lane paint found (obscured?)"
            if drift == float("inf")
            else f"lane paint {drift:.1f}px from calibration — camera may have moved"
        )
        return {
            "compound_visible": False,
            "fullness": None,
            "vehicle_count": None,
            "lanes_occupied": None,
            "queue_extends_beyond_frame": False,
            "confidence": 0.0,
            "notes": reason,
            "drift_px": None if drift == float("inf") else round(drift, 2),
            "illumination_ratio": round(lit, 3),
            "lane_shares": {},
        }

    shares = occupancy(frame, background, cal)
    occupied = occupied_lanes(shares)
    if occupied:
        notes = f"lanes {occupied} of {cal.lanes} occupied"
        if not cal.covers_compound:
            notes += " — the far end of the compound, so everything ahead of them is taken"
    elif cal.covers_compound:
        notes = "compound clear"
    else:
        notes = (
            f"lanes {cal.lanes} clear, but they are the far end of a compound this camera "
            "only partly sees — says nothing about how full the rest of it is"
        )
    return {
        "compound_visible": True,
        "fullness": fullness_from_lanes(shares, cal),
        "vehicle_count": None,  # geometry measures space, not vehicles; see `fullness`.
        "lanes_occupied": len(occupied),
        "queue_extends_beyond_frame": queue_reaches_last_visible_lane(shares, cal),
        # Deterministic given the calibration, so the only real uncertainty is whether the
        # calibration still holds. Scale confidence by how well the paint lines up.
        "confidence": round(max(0.5, 1.0 - drift / MAX_DRIFT_PX * 0.5), 3),
        "notes": notes,
        "drift_px": round(drift, 2),
        "illumination_ratio": round(lit, 3),
        "lane_shares": {str(k): round(v, 3) for k, v in sorted(shares.items())},
    }


def pending_frames(
    conn,
    limit: int,
    *,
    since: str | None = None,
    terminal: str | None = None,
    terminals: list[str] | None = None,
    camera: str | None = MAIN_CAMERA,
    prompt_version: str | None = None,
):
    """Frames with no geometric reading yet. Mirrors `vision.pending_frames`.

    Scoped to the terminal's own camera. A lane calibration is fitted to one view, and a
    frame from a different camera at the same terminal is not a harder version of the same
    problem — it is a different scene, at a different size, with no lane grid in it at all.
    `prompt_version` names which reader's absence makes a frame pending — the lane grid's
    by default, `extent`'s when sweeping a highway camera.
    """
    sql = """
        SELECT f.id, f.terminal, f.camera, f.captured_at, f.path
          FROM frames f
          LEFT JOIN observations o ON o.frame_id = f.id AND o.prompt_version = ?
         WHERE f.status = 'ok' AND f.path IS NOT NULL AND o.id IS NULL
    """
    params: list = [prompt_version or PROMPT_VERSION]
    if camera is not None:
        sql += " AND f.camera = ?"
        params.append(camera)
    if since:
        sql += " AND f.captured_at >= ?"
        params.append(since)
    if terminal:
        sql += " AND f.terminal = ?"
        params.append(terminal)
    if terminals is not None:
        placeholders = ",".join("?" for _ in terminals)
        sql += f" AND f.terminal IN ({placeholders})"
        params.extend(terminals)
    sql += " ORDER BY f.captured_at LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, tuple(params)).fetchall())


def extract_frames(
    conn, config, frames, *, dry_run: bool = False, max_reads: int | None = None
) -> LaneStats:
    """Read frames geometrically and store them as observations.

    These land in the same table as `vision`'s, under their own prompt version, so the two
    are directly comparable on identical frames and neither overwrites the other. Nothing
    here costs money, so unlike the vision path there is no budget to stop against.

    `max_reads` caps actual reads (~19ms each), not frames considered: a skip writes no
    observation and costs microseconds, so skipped frames must not eat the quota. Letting
    them do exactly that is how extraction starved in production — the 40 oldest pending
    frames were all unreadable, every run spent its whole budget re-skipping them, and no
    new frame was ever reached.
    """
    from .timeutil import iso, now_utc

    stats = LaneStats()
    if not frames or dry_run:
        stats.considered = len(frames)
        return stats

    from .timeutil import local, parse_iso

    config_dir = Path(config.source_path).parent
    cals: dict[str, LaneCalibration | None] = {}
    libraries: dict[str, BackgroundLibrary] = {}
    # One json read per (terminal, hour) per run, not one per frame: a backlog sweep may
    # consider thousands of frames, and `for_hour` goes to disk every call.
    backgrounds: dict[tuple[str, int], Background | None] = {}

    for frame in frames:
        if max_reads is not None and stats.read >= max_reads:
            break
        stats.considered += 1
        terminal = frame["terminal"]
        if terminal not in cals:
            cals[terminal] = load_for(config_dir, terminal)
            libraries[terminal] = BackgroundLibrary(config.data_dir, terminal)
        cal = cals[terminal]
        if cal is None:
            stats.skipped_no_calibration += 1
            continue

        # The reference has to match the hour, not just the camera. A single fixed frame
        # reads a bare floodlit compound at 04:00 as every lane occupied, so an hour with no
        # background yet is skipped rather than differenced against the wrong light.
        hour = local(parse_iso(frame["captured_at"]), config.tz).hour
        if (terminal, hour) not in backgrounds:
            backgrounds[terminal, hour] = libraries[terminal].for_hour(hour)
        background = backgrounds[terminal, hour]
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

        usable = bool(parsed["compound_visible"]) and parsed["fullness"] is not None
        conn.execute(
            """INSERT OR REPLACE INTO observations
                   (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                    queue_beyond_frame, ferry_at_dock, visibility, fullness, confidence,
                    usable, notes, input_tokens, output_tokens, cost_usd, created_at, raw)
               VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
            (
                frame["id"],
                PROMPT_VERSION,
                MODEL,
                parsed["lanes_occupied"],
                int(bool(parsed["queue_extends_beyond_frame"])),
                "clear" if parsed["compound_visible"] else "obscured",
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


#: How many pending frames one `extract_pending` sweep may pull from the database. A bound
#: on queue-scan work, not on reads — far above any real backlog's readable share, and it
#: keeps a pathological queue (every frame skipped) from making a single run unbounded.
PENDING_SCAN_LIMIT = 5000


def extract_pending(
    conn,
    config,
    *,
    max_reads: int,
    dry_run: bool = False,
    since: str | None = None,
    terminal: str | None = None,
) -> LaneStats:
    """Read up to `max_reads` of the oldest pending frames that can actually be read.

    The queue discipline matters more than it looks. Frames from a terminal with no
    calibration can never be read, and a skip writes no observation, so such frames stay
    pending forever. Querying pending frames oldest-first with a hard LIMIT then handed
    every run the same unreadable head — in production, forty Earls Cove frames blocked
    every Saltery Bay frame for three days. So: only calibrated terminals are queried at
    all, and the read cap is enforced by `extract_frames` against reads rather than against
    frames considered, which lets no-background frames (a transient condition — backgrounds
    build as history accumulates) wait in the queue without blocking anything behind them.

    One sweep covers every camera a geometric reader exists for — the lane grids first,
    then any `queue_extent` camera with a calibration, on whatever budget the grids left.
    That order is deliberate: the terminal cameras are live and a frame not read soon is a
    frame someone's "how is the queue now" cannot use, while the highway camera arrives in
    twice-daily replay batches that were already hours old when fetched — another few
    minutes in the queue costs those nothing. This is the single place that decides which
    cameras get read; the scheduler and the CLI both call it, because when each kept its
    own list the one running in production was the one that had never heard of the second
    camera.
    """
    from .extent import PROMPT_VERSION as EXTENT_PROMPT_VERSION
    from .extent import extract_frames as extract_extent_frames
    from .extent import load_for as load_extent

    config_dir = Path(config.source_path).parent
    stats = LaneStats()

    calibrated = [
        t.code
        for t in config.route.terminals
        if (terminal is None or t.code == terminal)
        and calibration_path(config_dir, t.code).exists()
    ]
    if calibrated:
        frames = pending_frames(conn, PENDING_SCAN_LIMIT, since=since, terminals=calibrated)
        stats = extract_frames(conn, config, frames, dry_run=dry_run, max_reads=max_reads)

    for t in config.route.terminals:
        if terminal is not None and t.code != terminal:
            continue
        for camera in t.cameras:
            if camera.kind != "queue_extent":
                continue
            if load_extent(config_dir, t.code, camera.id) is None:
                continue
            remaining = max_reads - stats.read
            if remaining <= 0:
                return stats
            frames = pending_frames(
                conn,
                PENDING_SCAN_LIMIT,
                since=since,
                terminal=t.code,
                camera=camera.id,
                prompt_version=EXTENT_PROMPT_VERSION,
            )
            stats.absorb(
                extract_extent_frames(
                    conn, config, frames, dry_run=dry_run, max_reads=remaining
                )
            )
    return stats


# --- rolling backgrounds ----------------------------------------------------------------

#: Frames per hour bucket below which a background is not built.
#:
#: Was 8, which proved far too low. Hour 08 sits inside the build for the 09:25 sailing, so
#: the compound is occupied most mornings at that time; with eight samples the vehicles
#: present in half of them survived the median and became part of "empty", and the resulting
#: reference read a nearly-bare compound as eight lanes occupied. The median only recovers
#: the ground when the ground is what is usually there, and at a busy hour that needs enough
#: days for the queue to have moved around. At a 5-minute cadence a fortnight gives ~168.
MIN_BACKGROUND_SAMPLES = 40

#: Ceiling on samples per bucket. The median converges long before this; the rest is
#: sorting cost.
MAX_BACKGROUND_SAMPLES = 48

#: How far back to draw samples. Long enough that vehicles average out, short enough that
#: the sun has not moved much — two weeks is about 4 degrees of solar declination, while
#: giving well over a hundred frames per bucket at a 5-minute cadence.
BACKGROUND_WINDOW_DAYS = 14


@dataclass(frozen=True)
class Background:
    """One hour's worth of "what this camera sees when nothing is queued"."""

    path: Path
    terminal: str
    hour: int
    samples: int
    window: tuple[str, str]
    built_at: str

    @property
    def thin(self) -> bool:
        return self.samples < MIN_BACKGROUND_SAMPLES


def backgrounds_dir(
    data_dir: str | Path, terminal: str, camera: str = MAIN_CAMERA
) -> Path:
    """Where one camera's references live.

    The main camera keeps the bare terminal directory it has always used, so an existing
    library needs no rebuild. Anything else gets a sibling, because a reference is a picture
    of one view: build it from two cameras' frames mixed together and the median is a picture
    of nowhere.
    """
    stem = terminal if camera == MAIN_CAMERA else f"{terminal}_{camera}"
    return Path(data_dir) / "backgrounds" / stem


class BackgroundLibrary:
    """Per-hour references for one camera.

    A single fixed reference cannot work across a day, let alone a year: differenced against
    a midday frame, a bare floodlit compound at 04:00 reports every lane occupied. Shadows
    move within the day and the sun's whole arc moves across the year, so the reference has
    to follow. Keeping one per hour, rebuilt from recent frames, tracks both without anyone
    deciding when the seasons change.
    """

    def __init__(
        self, data_dir: str | Path, terminal: str, camera: str = MAIN_CAMERA
    ) -> None:
        self.root = backgrounds_dir(data_dir, terminal, camera)
        self.terminal = terminal
        self.camera = camera

    def for_hour(self, hour: int) -> Background | None:
        meta = self.root / f"{hour:02d}.json"
        image = self.root / f"{hour:02d}.png"
        if not (meta.exists() and image.exists()):
            return None
        raw = json.loads(meta.read_text(encoding="utf-8"))
        bg = Background(
            path=image,
            terminal=raw["terminal"],
            hour=int(raw["hour"]),
            samples=int(raw["samples"]),
            window=(raw["from"], raw["to"]),
            built_at=raw["built_at"],
        )
        return None if bg.thin else bg

    def hours(self) -> list[int]:
        return sorted(
            int(p.stem) for p in self.root.glob("[0-9][0-9].json") if self.for_hour(int(p.stem))
        )


@dataclass
class BackgroundStats:
    built: int = 0
    thin: int = 0
    per_hour: dict[int, int] | None = None

    def __post_init__(self) -> None:
        if self.per_hour is None:
            self.per_hour = {}


def _median_image(paths: list[Path]):
    """Per-pixel median across frames — "what is usually here", which is the pavement.

    The point of the median is that it needs no idea which frames were empty. For any given
    pixel, asphalt is what is there most of the time and a vehicle is a passing event, so the
    middle value is the ground. That dissolves the bootstrapping problem: you would otherwise
    need a bare reference in order to detect bare frames.

    It fails where a pixel is covered more than half the time. At this terminal the compound
    is empty for most of most days, but an hour bucket that sits inside a reliably busy
    period is exactly where that assumption is thinnest.
    """
    from PIL import Image

    frames = []
    for path in paths:
        with Image.open(path) as img:
            grey = img.convert("L")
            frames.append(grey.tobytes())
            size = grey.size
    middle = len(frames) // 2
    data = bytes(sorted(values)[middle] for values in zip(*frames, strict=True))
    return Image.frombytes("L", size, data)


def build_backgrounds(
    conn,
    config,
    terminal: str,
    *,
    camera: str = MAIN_CAMERA,
    days: int = BACKGROUND_WINDOW_DAYS,
    at: object | None = None,
) -> BackgroundStats:
    """Rebuild this camera's per-hour references from recent frames.

    Scoped to one camera. Two cameras at a terminal see different scenes at possibly
    different sizes, and a median taken across both is a reference for neither — the pixel
    at (100, 100) is compound in one and treeline in the other.
    """
    from .timeutil import iso, local, now_utc, parse_iso

    stats = BackgroundStats()
    since = (at or now_utc()) - timedelta(days=days)
    rows = conn.execute(
        """SELECT captured_at, path FROM frames
            WHERE terminal = ? AND camera = ? AND status = 'ok' AND path IS NOT NULL
              AND captured_at >= ?
            ORDER BY captured_at""",
        (terminal, camera, iso(since)),
    ).fetchall()

    by_hour: dict[int, list[Path]] = {}
    for row in rows:
        path = Path(config.data_dir) / row["path"]
        if not path.exists():
            continue
        hour = local(parse_iso(row["captured_at"]), config.tz).hour
        by_hour.setdefault(hour, []).append(path)

    out = backgrounds_dir(config.data_dir, terminal, camera)
    out.mkdir(parents=True, exist_ok=True)
    for hour, paths in sorted(by_hour.items()):
        stats.per_hour[hour] = len(paths)
        if len(paths) < MIN_BACKGROUND_SAMPLES:
            stats.thin += 1
            continue
        # Spread the sample across the whole window rather than taking the most recent run,
        # so one busy afternoon cannot dominate a bucket.
        if len(paths) > MAX_BACKGROUND_SAMPLES:
            step = len(paths) / MAX_BACKGROUND_SAMPLES
            paths = [paths[int(i * step)] for i in range(MAX_BACKGROUND_SAMPLES)]
        try:
            image = _median_image(paths)
        except Exception:
            stats.thin += 1
            continue
        image.save(out / f"{hour:02d}.png")
        (out / f"{hour:02d}.json").write_text(
            json.dumps(
                {
                    "terminal": terminal,
                    "hour": hour,
                    "samples": len(paths),
                    "from": rows[0]["captured_at"],
                    "to": rows[-1]["captured_at"],
                    "built_at": iso(now_utc()),
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        stats.built += 1
    return stats
