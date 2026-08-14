#!/usr/bin/env python3
"""Viability experiment: can ferry load actually be read off the webcam?

Watches one Saltery Bay sailing live. Every minute for two hours it pulls the
terminal camera frame; every frame that is genuinely new (the camera republishes
the same JPEG between refreshes, so frames are deduped by content hash) is read
by BOTH claude-sonnet-5 and claude-opus-5 with the production v2 fullness
prompt, so the two models can be compared on identical inputs.

In parallel it polls the BC Ferries current-conditions board. Once the target
sailing is marked Departed, the compound is given `SETTLE_MINUTES` to drain and
the readings after that point become the verdict: if the settled compound still
holds vehicles, the sailing left people behind.

Everything is appended to a JSONL log as it happens, so a crash mid-run loses
nothing, and a summary (per-model timeline, agreement, verdict) prints at the
end.

Usage:
    python scripts/watch_sailing.py                 # 14:30 sailing, 2 hours
    python scripts/watch_sailing.py --sailing 16:35 --duration-minutes 90

Needs ANTHROPIC_API_KEY (or an `ant auth login` profile).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Works uninstalled too: put src/ on the path relative to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ferrycast.deckspace import parse_deck_space  # noqa: E402
from ferrycast.fetching import fetch  # noqa: E402
from ferrycast.vision import (  # noqa: E402
    FULLNESS_LEVELS,
    prompt_for,
    prepare_image,
)

WEBCAM_URL = "https://ccimg.bcferries.com/cc/support/terminals/cam1_SLT.jpg"
CONDITIONS_URL = "https://www.bcferries.com/current-conditions/SLT-ERL"
TERMINAL_NAME = "Saltery Bay"
TZ = ZoneInfo("America/Vancouver")

USER_AGENT = "ferrycast/0.1 (one-off viability experiment; contact: smckenna1981@hotmail.com)"

MODELS = ("claude-sonnet-5", "claude-opus-5")
# List prices, USD per MTok (input, output), for the running cost readout.
PRICES = {"claude-sonnet-5": (3.0, 15.0), "claude-opus-5": (5.0, 25.0)}

MAX_IMAGE_WIDTH = 896       # matches production: the main cost lever
DARK_LUMA_THRESHOLD = 24.0  # a night frame is flagged, not paid for

# Mirrors [aggregate] in ferrycast.toml: the residual is read only after the
# departing traffic has cleared the frame, and only counts as "left behind"
# when it is more than stragglers.
SETTLE_MINUTES = 12
POST_WINDOW_MINUTES = 45
RESIDUAL_VEHICLE_THRESHOLD = 5

STATUS_POLL_EVERY = 2  # poll the conditions page every Nth tick: be polite


@dataclass
class Assessment:
    at: datetime
    model: str
    frame_hash: str
    fullness: str | None
    vehicle_count: int | None
    confidence: float
    compound_visible: bool
    notes: str


@dataclass
class RunState:
    frames_seen: int = 0
    frames_new: int = 0
    frames_dark: int = 0
    assessments: list[Assessment] = field(default_factory=list)
    departed_at: datetime | None = None
    board_status: str | None = None
    cost_usd: dict[str, float] = field(default_factory=lambda: {m: 0.0 for m in MODELS})
    errors: list[str] = field(default_factory=list)


def _safe(fn, model):
    """Run one model's assessment, returning the exception instead of raising —
    one model failing must not cost the other model its reading of the frame."""
    try:
        return fn(model)
    except Exception as exc:
        return model, exc


def log_event(out: Path, record: dict) -> None:
    with out.open("a") as fh:
        fh.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")


def assess_frame_cli(model: str, frame_path: Path) -> tuple[dict, int, int]:
    """One vision call via headless Claude Code (`claude -p`).

    This is the subscription path: no API key is billed — the call runs under
    the same login as the interactive CLI. The trade-offs versus the SDK path
    are latency (each call is a full headless session) and no server-enforced
    schema, so the JSON is requested by prompt and extracted defensively.
    """
    prompt = prompt_for("v2")
    task = (
        prompt.system
        + f"\n\nUse the Read tool to view the webcam frame at {frame_path}\n"
        + prompt.instruction.format(terminal=TERMINAL_NAME)
        + "\n\nRespond with ONLY a JSON object with exactly these fields:"
        ' {"compound_visible": bool, "fullness": "empty"|"light"|"moderate"|"heavy"|'
        '"overflowing"|null, "vehicle_count": int|null, "confidence": number 0-1,'
        ' "notes": string}. No prose, no code fences.'
    )
    # --setting-sources project and an empty MCP config keep the user-level
    # CLAUDE.md framework files and MCP tool schemas out of the session. On the
    # 2026-08-12 run each call carried ~50k tokens of that overhead (vs ~1.5k
    # of actual work) and exhausted the subscription's usage window in 44
    # minutes; with these flags a call is a small fraction of that.
    proc = subprocess.run(
        [
            "claude", "-p", task,
            "--model", model,
            "--output-format", "json",
            "--allowedTools", "Read",
            "--max-turns", "4",
            "--setting-sources", "project",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p ({model}) exited {proc.returncode}: {proc.stderr[:200]}")
    envelope = json.loads(proc.stdout)
    text = envelope.get("result") or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"{model}: no JSON in response: {text[:120]!r}")
    parsed = json.loads(text[start : end + 1])
    usage = envelope.get("usage") or {}
    return parsed, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def assess_frame(client, model: str, image: bytes, media_type: str) -> tuple[dict, int, int]:
    """One vision call against one model. Returns (parsed, input_tokens, output_tokens)."""
    prompt = prompt_for("v2")
    response = client.messages.create(
        model=model,
        max_tokens=1024,
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
                    {"type": "text", "text": prompt.instruction.format(terminal=TERMINAL_NAME)},
                ],
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"{model} declined to analyse the frame")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"{model}: no text block (stop_reason={response.stop_reason})")
    return json.loads(text), response.usage.input_tokens, response.usage.output_tokens


def check_board(sailing_hhmm: str, now_local: datetime) -> tuple[str | None, datetime | None]:
    """Read the conditions board. Returns (status snippet, departure time if shown).

    The board's own "Departed h:mm" stamp is preferred over our polling clock —
    it is the actual departure, and we only poll every couple of minutes.
    """
    result = fetch(CONDITIONS_URL, user_agent=USER_AGENT, timeout=20.0, max_retries=1)
    if not result.ok or not result.text:
        return None, None
    rows = parse_deck_space(result.text, expected=[sailing_hhmm])
    row = next((r for r in rows if r.sailing_hhmm == sailing_hhmm), None)
    if row is None:
        return None, None

    sched_h, sched_m = (int(p) for p in sailing_hhmm.split(":"))
    scheduled = now_local.replace(hour=sched_h, minute=sched_m, second=0, microsecond=0)

    departed = None
    if row.departed_hhmm:
        hour, minute = (int(p) for p in row.departed_hhmm.split(":"))
        departed = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Plausibility gate. The page's "Last updated: h:mm" stamp inevitably
        # collides with the sailing time at exactly the scheduled minute (the
        # 2026-08-12 run was stopped by "Last updated: 2:30 pm" while watching
        # the 14:30), and that phantom row's segment can swallow ANOTHER
        # sailing's "Departed h:mm". A real departure sits near its schedule;
        # anything else is the wrong row.
        if not (scheduled - timedelta(minutes=30) <= departed <= scheduled + timedelta(hours=4)):
            return row.status_text, None
        if departed > now_local + timedelta(minutes=10):
            return row.status_text, None
    elif row.status_text and "departed" in row.status_text.lower():
        departed = now_local
    return row.status_text, departed


def post_settle(state: RunState, model: str) -> list[Assessment]:
    if state.departed_at is None:
        return []
    start = state.departed_at + timedelta(minutes=SETTLE_MINUTES)
    end = state.departed_at + timedelta(minutes=POST_WINDOW_MINUTES)
    return [
        a
        for a in state.assessments
        if a.model == model and a.compound_visible and a.fullness and start <= a.at <= end
    ]


def verdict_for(state: RunState, model: str) -> tuple[str, str]:
    """(verdict, reasoning) for one model: were vehicles left behind?"""
    readings = post_settle(state, model)
    if state.departed_at is None:
        return "unknown", "the sailing was never marked departed within the watch window"
    if not readings:
        return "unknown", (
            f"no usable frames between {SETTLE_MINUTES} and {POST_WINDOW_MINUTES} minutes "
            "after departure"
        )
    band = Counter(a.fullness for a in readings).most_common(1)[0][0]
    counts = [a.vehicle_count for a in readings if a.vehicle_count is not None]
    count_note = f", counts {sorted(counts)}" if counts else ""
    basis = f"settled compound read as '{band}' over {len(readings)} frame(s){count_note}"
    if FULLNESS_LEVELS.index(band) >= FULLNESS_LEVELS.index("moderate"):
        return "yes", basis
    if counts and max(counts) >= RESIDUAL_VEHICLE_THRESHOLD:
        return "yes", basis + f" — count at or above the {RESIDUAL_VEHICLE_THRESHOLD}-vehicle threshold"
    if band == "light" and counts and max(counts) < RESIDUAL_VEHICLE_THRESHOLD:
        return "no", basis + " — stragglers only, below the left-behind threshold"
    if band == "empty":
        return "no", basis
    return "probably not", basis + " — 'light' with no count; likely stragglers"


def print_summary(state: RunState, sailing_hhmm: str) -> None:
    print("\n" + "=" * 72)
    print(f"SUMMARY — Saltery Bay {sailing_hhmm} sailing")
    print("=" * 72)
    print(
        f"frames fetched: {state.frames_seen}   new: {state.frames_new}   "
        f"dark-skipped: {state.frames_dark}   model errors: {len(state.errors)}"
    )
    if state.departed_at:
        print(f"board marked the sailing departed at {state.departed_at.strftime('%H:%M')} local")
    else:
        print("the sailing was never marked departed while the script was watching")

    for model in MODELS:
        rows = [a for a in state.assessments if a.model == model]
        print(f"\n--- {model}  (${state.cost_usd[model]:.3f}) ---")
        for a in rows:
            marker = ""
            if state.departed_at and a.at >= state.departed_at:
                marker = "  <- after departure"
            band = a.fullness if a.compound_visible else f"unusable ({a.notes[:40]})"
            count = f" count={a.vehicle_count}" if a.vehicle_count is not None else ""
            print(f"  {a.at.strftime('%H:%M')}  {band:<12}{count}  conf={a.confidence:.2f}{marker}")
        verdict, why = verdict_for(state, model)
        print(f"  VERDICT — vehicles left behind: {verdict.upper()}  ({why})")

    # The viability question is really about agreement: if the two models read
    # the same frames differently, the premise is shaky whatever either says.
    by_frame: dict[str, dict[str, str | None]] = {}
    for a in state.assessments:
        if a.compound_visible and a.fullness:
            by_frame.setdefault(a.frame_hash, {})[a.model] = a.fullness
    paired = [v for v in by_frame.values() if len(v) == len(MODELS)]
    if paired:
        exact = sum(1 for v in paired if len(set(v.values())) == 1)
        within_one = sum(
            1
            for v in paired
            if max(FULLNESS_LEVELS.index(b) for b in v.values())
            - min(FULLNESS_LEVELS.index(b) for b in v.values())
            <= 1
        )
        print(
            f"\nmodel agreement over {len(paired)} shared frames: "
            f"exact {exact}/{len(paired)}, within one band {within_one}/{len(paired)}"
        )
    if state.errors:
        print("\nerrors:")
        for err in state.errors[-10:]:
            print(f"  {err}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sailing", default="14:30", help="scheduled departure, HH:MM local")
    parser.add_argument("--duration-minutes", type=int, default=120)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument(
        "--assess-every",
        type=int,
        default=2,
        help="read every Nth new frame with the models (frames are still fetched and "
        "saved every interval — this only throttles the paid/quota-bound calls)",
    )
    parser.add_argument("--out", default=None, help="JSONL log path")
    args = parser.parse_args()

    # Two backends. With an API key (env or repo-root .env, same shape as
    # .env.example) the SDK is used and billed per token. Without one, calls go
    # through headless Claude Code (`claude -p`) under the user's subscription.
    import os
    import shutil

    if not os.environ.get("ANTHROPIC_API_KEY"):
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            for raw in env_file.read_text().splitlines():
                if raw.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = raw.split("=", 1)[1].strip().strip('"')

    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        backend = "sdk"
        import anthropic

        client = anthropic.Anthropic()
    elif shutil.which("claude"):
        backend = "cli"
    else:
        print("no ANTHROPIC_API_KEY and no `claude` CLI on PATH — cannot assess frames")
        return 1

    started = datetime.now(TZ)
    deadline = started + timedelta(minutes=args.duration_minutes)
    out = Path(
        args.out or f"viability_{started.strftime('%Y%m%d_%H%M')}_{args.sailing.replace(':', '')}.jsonl"
    )
    state = RunState()
    seen_hashes: set[str] = set()
    frames_dir = out.with_suffix("").parent / (out.stem + "_frames")
    frames_dir.mkdir(parents=True, exist_ok=True)

    print(f"watching {TERMINAL_NAME} for the {args.sailing} sailing  [backend: {backend}]")
    print(f"{started.strftime('%H:%M')} -> {deadline.strftime('%H:%M')} local, "
          f"one frame per {args.interval_seconds}s, log: {out}")

    tick = 0
    while datetime.now(TZ) < deadline:
        tick += 1
        now = datetime.now(TZ)
        line = [now.strftime("%H:%M:%S")]

        # --- the camera ---
        result = fetch(WEBCAM_URL, user_agent=USER_AGENT, timeout=20.0, max_retries=1)
        if not result.ok or not result.content:
            line.append(f"camera fetch failed ({result.error})")
            state.errors.append(f"{now:%H:%M} camera: {result.error}")
        else:
            state.frames_seen += 1
            digest = hashlib.sha256(result.content).hexdigest()[:16]
            if digest in seen_hashes:
                line.append("frame unchanged")
            else:
                seen_hashes.add(digest)
                state.frames_new += 1
                image, media_type, luma = prepare_image(result.content, MAX_IMAGE_WIDTH)
                frame_path = frames_dir / f"{now:%H%M%S}_{digest}.jpg"
                frame_path.write_bytes(image)
                if luma is not None and luma < DARK_LUMA_THRESHOLD:
                    state.frames_dark += 1
                    line.append(f"frame too dark to read (luma {luma:.0f})")
                elif (state.frames_new - 1) % args.assess_every != 0:
                    line.append("frame saved, not assessed (throttle)")
                else:

                    def run_one(model):
                        if backend == "cli":
                            return model, assess_frame_cli(model, frame_path)
                        return model, assess_frame(client, model, image, media_type)

                    with ThreadPoolExecutor(max_workers=len(MODELS)) as pool:
                        results = list(pool.map(lambda m: _safe(run_one, m), MODELS))
                    for model, outcome in results:
                        if isinstance(outcome, Exception):
                            state.errors.append(f"{now:%H:%M} {model}: {outcome}")
                            line.append(f"{model.split('-')[1]}=ERR")
                            continue
                        parsed, in_tok, out_tok = outcome
                        in_price, out_price = PRICES[model]
                        state.cost_usd[model] += (in_tok * in_price + out_tok * out_price) / 1e6
                        a = Assessment(
                            at=now,
                            model=model,
                            frame_hash=digest,
                            fullness=parsed.get("fullness"),
                            vehicle_count=parsed.get("vehicle_count"),
                            confidence=float(parsed.get("confidence") or 0.0),
                            compound_visible=bool(parsed.get("compound_visible")),
                            notes=str(parsed.get("notes") or ""),
                        )
                        state.assessments.append(a)
                        log_event(out, {"type": "assessment", **a.__dict__,
                                        "input_tokens": in_tok, "output_tokens": out_tok})
                        line.append(f"{model.split('-')[1]}={a.fullness or 'n/a'}"
                                    f"({a.confidence:.2f})")

        # --- the board ---
        if tick % STATUS_POLL_EVERY == 1:
            try:
                status, departed = check_board(args.sailing, now)
            except Exception as exc:
                state.errors.append(f"{now:%H:%M} board: {exc}")
                status, departed = None, None
            if status:
                state.board_status = status
                line.append(f"board: {status[:48]}")
            if departed and state.departed_at is None:
                state.departed_at = departed
                line.append(f"** DEPARTED {departed.strftime('%H:%M')} **")
                log_event(out, {"type": "departure", "at": departed, "board": status})

        print("  ".join(line), flush=True)

        # Once the post-departure window has fully elapsed there is nothing
        # left to observe — end early rather than burn another hour of calls.
        if state.departed_at and datetime.now(TZ) > state.departed_at + timedelta(
            minutes=POST_WINDOW_MINUTES + 5
        ):
            print("post-departure window complete — stopping early")
            break

        elapsed = (datetime.now(TZ) - now).total_seconds()
        time.sleep(max(0.0, args.interval_seconds - elapsed))

    print_summary(state, args.sailing)
    log_event(
        out,
        {
            "type": "summary",
            "sailing": args.sailing,
            "departed_at": state.departed_at,
            "verdicts": {m: verdict_for(state, m) for m in MODELS},
            "cost_usd": state.cost_usd,
            "frames_seen": state.frames_seen,
            "frames_new": state.frames_new,
        },
    )
    print(f"\nfull log: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
