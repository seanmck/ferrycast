"""R3 — vision extraction.

Each stored frame becomes one structured observation. Three properties the PRD asks for:

* **Idempotent** — observations are keyed by (frame, prompt_version), so re-running over the
  backlog after improving the prompt adds a new generation instead of overwriting history.
* **Cheap** — frames are downscaled before upload (image tokens scale with pixel area), dark
  frames are flagged without spending a call, and a monthly budget stops the run.
* **Honest about uncertainty** — low-confidence and obscured frames are marked unusable
  rather than guessed at, so aggregation can exclude them.
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import MAIN_CAMERA, Config
from .db import JobRun
from .timeutil import iso, now_utc

PROMPT_VERSION = "v2"

#: How full the compound is, weakest to fullest. Ordered, so aggregation can compare.
FULLNESS_LEVELS = ("empty", "light", "moderate", "heavy", "overflowing")

SYSTEM_PROMPT_V1 = """\
You are reading a fixed webcam frame of a BC Ferries terminal holding compound, where
vehicles queue for a first-come-first-served sailing.

Report only what is visible in this frame. Do not infer from what a terminal usually looks
like, and do not round toward a "typical" number.

Counting rules:
- vehicle_count: every vehicle waiting in the marshalling lanes or approach queue. Include
  cars, trucks, RVs and trailers; count a towed trailer as part of its towing vehicle.
  Exclude vehicles that are clearly moving through, parked in staff or public parking, or
  already aboard the vessel.
- lanes_occupied: how many marshalling lanes hold at least one waiting vehicle.
- queue_extends_beyond_frame: true if the queue runs to the edge of the image or up the
  approach road, so the true count is higher than what you can see.
- ferry_at_dock: true only if a vessel is berthed at this terminal in this frame.
- visibility: "clear" normally; "dim" at dawn or dusk; "obscured" for fog, rain or spray on
  the lens; "dark" if you cannot make out the compound at all.
- confidence: your confidence in vehicle_count specifically, from 0 to 1. Be honest — a low
  score is far more useful than a confident guess. Anything below 0.4 is treated as unusable.

If the compound is not visible, return vehicle_count 0, visibility "dark" or "obscured",
and a confidence at or below 0.2.\
"""

OBSERVATION_SCHEMA_V1 = {
    "type": "object",
    "properties": {
        "vehicle_count": {
            "type": "integer",
            "description": "Vehicles waiting in the compound and approach queue.",
        },
        "lanes_occupied": {
            "type": "integer",
            "description": "Marshalling lanes holding at least one waiting vehicle.",
        },
        "queue_extends_beyond_frame": {
            "type": "boolean",
            "description": "True if the queue continues past the edge of the frame.",
        },
        "ferry_at_dock": {
            "type": "boolean",
            "description": "True if a vessel is berthed at this terminal.",
        },
        "visibility": {
            "type": "string",
            "enum": ["clear", "dim", "obscured", "dark"],
        },
        "confidence": {"type": "number", "description": "Confidence in vehicle_count, 0 to 1."},
        "notes": {
            "type": "string",
            "description": "Short note on anything unusual. Empty string if nothing to add.",
        },
    },
    "required": [
        "vehicle_count",
        "lanes_occupied",
        "queue_extends_beyond_frame",
        "ferry_at_dock",
        "visibility",
        "confidence",
        "notes",
    ],
    "additionalProperties": False,
}

# v2 asks how full the compound is, not how many vehicles are in it.
#
# Measured over four sailings (38 frames, Saltery Bay, 2026-08-11/12), against fullness
# read off the frames by hand:
#
#   * The band is reliable. Sonnet landed within one band on 39/39 frames and called all
#     15 genuinely empty frames empty, with rank correlation 0.86-0.98 per sailing. It
#     tracks both the fill and the collapse after departure.
#   * Counts are not. One model returned an identical count on four consecutive frames
#     spanning an hour; across models the spread on a single frame was 11 to 34. A count
#     is also the wrong unit — an RV and a hatchback are not interchangeable.
#   * `lanes_occupied` and `queue_extends_beyond_frame` were dropped here, but NOT because
#     the camera cannot support them. The experiment that rejected them asked models which
#     lane *numbers* they could read, and only 9-12 are painted in view — a question about
#     paint, not geometry. Asked instead to count lines outward from the numbered lanes, the
#     same model identified lanes 5-8 correctly. What it could not do was answer consistently:
#     on some frames it collapsed back to the numbered lanes and reported an empty compound
#     while a queue stood in the unnumbered ones, and its confidence was *highest* on exactly
#     those frames. So this prompt does not ask for lanes — but `lanes.py` measures them from
#     fitted geometry, which cannot fail that way, and it is the better source.
#   * The known bias is at the top of the scale: "overflowing" was returned for a sailing
#     that had two lanes free and cleared completely. Treat the top band as suspect until
#     a genuinely full sailing exists to calibrate against.
SYSTEM_PROMPT_V2 = """\
You are reading a fixed webcam frame of a BC Ferries terminal marshalling compound, where
vehicles queue for a first-come-first-served sailing.

Report only what is visible in this frame. Do not infer from what a terminal usually looks
like at this hour, and do not smooth toward a "typical" level.

Judge how full the compound is, on this scale:

- empty        no vehicles queued at all
- light        a few vehicles; the great majority of the compound is bare
- moderate     roughly a third to a half of the compound holds vehicles
- heavy        most of it holds vehicles, but there is clear unused space remaining
- overflowing  essentially full; little or no usable space left

Judge the compound as a whole, including any part of it that runs past the edge of the
frame. Count only vehicles queued to board: ignore those on the access road, parked by the
buildings, moving through, or already aboard the vessel.

Reserve "overflowing" for a compound with no room left. If whole lanes are still bare, it
is "heavy" however many vehicles are present.

Other fields:
- compound_visible: false if fog, glare, rain on the lens or darkness stops you judging
  the paved queuing area. This is the gate — when it is false the frame is not used.
- vehicle_count: a count only if you can make one honestly; null otherwise. It is
  secondary to the band and never worth guessing at.
- confidence: your confidence in the band specifically, 0 to 1. A low score is far more
  useful than a confident guess.

If the compound is not visible, set compound_visible false, fullness null, and a
confidence at or below 0.2.\
"""

OBSERVATION_SCHEMA_V2 = {
    "type": "object",
    "properties": {
        "compound_visible": {
            "type": "boolean",
            "description": "True if the paved queuing area can be judged in this frame.",
        },
        "fullness": {
            "type": ["string", "null"],
            "enum": [*FULLNESS_LEVELS, None],
            "description": "How full the marshalling compound is; null if not visible.",
        },
        "vehicle_count": {
            "type": ["integer", "null"],
            "description": "Secondary and optional. Null unless an honest count is possible.",
        },
        "confidence": {"type": "number", "description": "Confidence in the band, 0 to 1."},
        "notes": {
            "type": "string",
            "description": "Short note on anything unusual. Empty string if nothing to add.",
        },
    },
    "required": ["compound_visible", "fullness", "vehicle_count", "confidence", "notes"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Prompt:
    """One generation of the extraction contract: what we ask, and what shape comes back."""

    system: str
    schema: dict
    instruction: str


PROMPTS: dict[str, Prompt] = {
    "v1": Prompt(
        system=SYSTEM_PROMPT_V1,
        schema=OBSERVATION_SCHEMA_V1,
        instruction="This is the {terminal} terminal. Report the queue visible in this frame.",
    ),
    "v2": Prompt(
        system=SYSTEM_PROMPT_V2,
        schema=OBSERVATION_SCHEMA_V2,
        instruction=(
            "This is the {terminal} terminal. How full is the marshalling compound in this "
            "frame?"
        ),
    ),
}

# The current generation, for callers that just want "the prompt".
SYSTEM_PROMPT = PROMPTS[PROMPT_VERSION].system
OBSERVATION_SCHEMA = PROMPTS[PROMPT_VERSION].schema


def prompt_for(version: str) -> Prompt:
    try:
        return PROMPTS[version]
    except KeyError:
        known = ", ".join(sorted(PROMPTS))
        raise VisionError(f"unknown prompt_version {version!r} (known: {known})") from None


@dataclass
class ExtractionStats:
    considered: int = 0
    extracted: int = 0
    skipped_dark: int = 0
    failed: int = 0
    flagged_unusable: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    budget_stopped: bool = False


class VisionError(Exception):
    pass


def month_start(day: date) -> str:
    return date(day.year, day.month, 1).isoformat()


def month_to_date_cost(conn: sqlite3.Connection, today: date | None = None) -> float:
    today = today or now_utc().date()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM observations WHERE created_at >= ?",
        (month_start(today),),
    ).fetchone()
    return float(row[0] or 0.0)


def estimate_cost(config: Config, input_tokens: int, output_tokens: int) -> float:
    v = config.vision
    return (input_tokens * v.input_usd_per_mtok + output_tokens * v.output_usd_per_mtok) / 1e6


def prepare_image(data: bytes, max_width: int) -> tuple[bytes, str, float | None]:
    """Downscale for cost, and measure mean brightness so night frames can be skipped.

    Image tokens scale with pixel area, so halving the long edge quarters the per-frame
    cost. A terminal compound at ~900px wide is still legible for counting vehicles.
    """
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return data, "image/jpeg", None

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        luma = ImageStat.Stat(img.convert("L")).mean[0]
        if img.width > max_width:
            height = max(1, round(img.height * max_width / img.width))
            img = img.resize((max_width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=80, optimize=True)
    return buffer.getvalue(), "image/jpeg", luma


def build_client(api_key: str | None = None):
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise VisionError("the `anthropic` package is required for extraction") from exc
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def extract_frame(client, config: Config, image: bytes, media_type: str, terminal_name: str):
    """One vision call. Returns (parsed dict, input_tokens, output_tokens)."""
    import base64

    prompt = prompt_for(config.vision.prompt_version)
    response = client.messages.create(
        model=config.vision.model,
        max_tokens=512,
        system=prompt.system,
        output_config={"format": {"type": "json_schema", "schema": prompt.schema}},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": prompt.instruction.format(terminal=terminal_name)},
                ],
            }
        ],
    )

    if response.stop_reason == "refusal":
        raise VisionError("model declined to analyse the frame")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise VisionError(f"no text block in response (stop_reason={response.stop_reason})")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionError(f"response was not valid JSON: {exc}") from exc

    return parsed, response.usage.input_tokens, response.usage.output_tokens


def pending_frames(
    conn: sqlite3.Connection,
    prompt_version: str,
    limit: int,
    *,
    since: str | None = None,
    terminal: str | None = None,
    camera: str | None = MAIN_CAMERA,
) -> list[sqlite3.Row]:
    """Frames that have no observation at this prompt version yet.

    Scoped to one camera, defaulting to the terminal's own, because this path costs money.
    An extra camera archived for a different purpose — a highway view up the approach road,
    say — would otherwise be posted to the model frame by frame and billed for, to answer a
    question about a compound that is not in the picture. Pass `camera=None` to mean every
    camera, deliberately.
    """
    sql = """
        SELECT f.id, f.terminal, f.camera, f.captured_at, f.path
          FROM frames f
          LEFT JOIN observations o
            ON o.frame_id = f.id AND o.prompt_version = ?
         WHERE f.status = 'ok' AND f.path IS NOT NULL AND o.id IS NULL
    """
    params: list = [prompt_version]
    if camera is not None:
        sql += " AND f.camera = ?"
        params.append(camera)
    if since:
        sql += " AND f.captured_at >= ?"
        params.append(since)
    if terminal:
        sql += " AND f.terminal = ?"
        params.append(terminal)
    sql += " ORDER BY f.captured_at LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, tuple(params)).fetchall())


def _visibility_of(parsed: dict) -> str | None:
    """v1 reported `visibility` directly; v2 reduces it to the one bit that gates a frame.

    Both land in the same column so `status` and the collection pages keep working across
    the version boundary.
    """
    if "visibility" in parsed:
        return parsed.get("visibility")
    if "compound_visible" in parsed:
        return "clear" if parsed.get("compound_visible") else "obscured"
    return None


def is_usable(config: Config, parsed: dict) -> bool:
    """Whether an observation is trustworthy enough for aggregation to use.

    v1 gated on `visibility` plus a count being present. v2 gates on `compound_visible`,
    because that is the question the model can actually answer about a frame — a dusk frame
    is "dim" but perfectly legible, and a count being absent says nothing about the band.
    """
    confidence = float(parsed.get("confidence") or 0.0)
    if confidence < config.vision.min_confidence:
        return False
    if "compound_visible" in parsed:
        return bool(parsed.get("compound_visible")) and parsed.get("fullness") is not None
    return (
        parsed.get("visibility") not in ("dark", "obscured")
        and parsed.get("vehicle_count") is not None
    )


def _insert_observation(
    conn: sqlite3.Connection,
    config: Config,
    frame_id: int,
    parsed: dict,
    *,
    usable: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO observations
               (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                queue_beyond_frame, ferry_at_dock, visibility, fullness, confidence, usable,
                notes, input_tokens, output_tokens, cost_usd, created_at, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            frame_id,
            config.vision.prompt_version,
            config.vision.model,
            parsed.get("vehicle_count"),
            parsed.get("lanes_occupied"),
            int(bool(parsed.get("queue_extends_beyond_frame"))),
            int(bool(parsed.get("ferry_at_dock"))),
            _visibility_of(parsed),
            parsed.get("fullness"),
            parsed.get("confidence"),
            int(usable),
            (parsed.get("notes") or None),
            input_tokens,
            output_tokens,
            round(cost, 8),
            iso(now_utc()),
            json.dumps(parsed, separators=(",", ":")),
        ),
    )
    conn.commit()


def extract_pending(
    conn: sqlite3.Connection,
    config: Config,
    *,
    limit: int | None = None,
    since: str | None = None,
    terminal: str | None = None,
    client=None,
    dry_run: bool = False,
) -> ExtractionStats:
    """Extract every frame that has no observation yet, oldest first."""
    limit = limit or config.vision.max_frames_per_run
    frames = pending_frames(
        conn, config.vision.prompt_version, limit, since=since, terminal=terminal
    )
    return extract_frames(conn, config, frames, client=client, dry_run=dry_run)


def extract_frames(
    conn: sqlite3.Connection,
    config: Config,
    frames: list[sqlite3.Row],
    *,
    client=None,
    dry_run: bool = False,
    job_name: str = "extract",
) -> ExtractionStats:
    """Extract a caller-chosen set of frames.

    Separating *which* frames to read from *how* to read them is what makes on-demand and
    sparse extraction possible: the expensive step is unchanged, only the selection differs.
    """
    stats = ExtractionStats()
    stats.considered = len(frames)
    if not frames or dry_run:
        return stats

    spent = month_to_date_cost(conn)
    budget = config.vision.monthly_budget_usd
    if budget and spent >= budget:
        stats.budget_stopped = True
        stats.errors.append(f"monthly budget of ${budget:.2f} already spent (${spent:.2f})")
        return stats

    client = client or build_client()
    terminal_names = {t.code: t.name for t in config.route.terminals}

    with JobRun(conn, job_name) as run:
        for frame in frames:
            if budget and spent + stats.cost_usd >= budget:
                stats.budget_stopped = True
                stats.errors.append(f"stopped: monthly budget of ${budget:.2f} reached")
                break

            run.attempted += 1
            path = Path(config.data_dir) / frame["path"]
            if not path.exists():
                stats.failed += 1
                stats.errors.append(f"frame {frame['id']}: file missing at {path}")
                continue

            try:
                image, media_type, luma = prepare_image(
                    path.read_bytes(), config.vision.max_image_width
                )
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"frame {frame['id']}: unreadable image ({exc})")
                continue

            # Night frames are flagged, not guessed at — and cost nothing to flag.
            if (
                config.vision.skip_dark_frames
                and luma is not None
                and luma < config.vision.dark_luma_threshold
            ):
                _insert_observation(
                    conn,
                    config,
                    frame["id"],
                    {
                        "vehicle_count": None,
                        "lanes_occupied": None,
                        "queue_extends_beyond_frame": False,
                        "ferry_at_dock": False,
                        "compound_visible": False,
                        "fullness": None,
                        "visibility": "dark",
                        "confidence": 0.0,
                        "notes": f"skipped without a model call: mean luma {luma:.1f}",
                    },
                    usable=False,
                )
                stats.skipped_dark += 1
                stats.flagged_unusable += 1
                run.succeeded += 1
                continue

            try:
                parsed, in_tok, out_tok = extract_frame(
                    client,
                    config,
                    image,
                    media_type,
                    terminal_names.get(frame["terminal"], frame["terminal"]),
                )
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"frame {frame['id']}: {exc}")
                continue

            cost = estimate_cost(config, in_tok, out_tok)
            usable = is_usable(config, parsed)
            _insert_observation(
                conn,
                config,
                frame["id"],
                parsed,
                usable=usable,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost=cost,
            )
            stats.extracted += 1
            stats.input_tokens += in_tok
            stats.output_tokens += out_tok
            stats.cost_usd += cost
            if not usable:
                stats.flagged_unusable += 1
            run.succeeded += 1

    return stats
