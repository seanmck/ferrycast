#!/usr/bin/env python3
"""Backfill model assessments for frames a watch_sailing run saved but never
read (e.g. because the usage window was exhausted mid-run). Appends to the
run's JSONL so the fill curve is complete."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from watch_sailing import MODELS, assess_frame_cli, log_event  # noqa: E402


def main() -> int:
    jsonl = Path(sys.argv[1])
    frames_dir = jsonl.parent / (jsonl.stem + "_frames")
    done: set[tuple[str, str]] = set()
    for line in jsonl.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("type") == "assessment":
            done.add((rec["frame_hash"], rec["model"]))

    frames = sorted(frames_dir.glob("*.jpg"))
    todo = [
        (p, model)
        for p in frames
        for model in MODELS
        if (p.stem.split("_", 1)[1], model) not in done
    ]
    print(f"{len(frames)} frames on disk, {len(todo)} (frame, model) pairs to backfill")

    def run(item):
        p, model = item
        hhmmss, digest = p.stem.split("_", 1)
        try:
            parsed, i, o = assess_frame_cli(model, p)
        except Exception as exc:
            return f"{hhmmss} {model}: ERR {exc}"
        log_event(
            jsonl,
            {
                "type": "assessment",
                "at": f"backfill:{hhmmss}",
                "model": model,
                "frame_hash": digest,
                "fullness": parsed.get("fullness"),
                "vehicle_count": parsed.get("vehicle_count"),
                "confidence": parsed.get("confidence"),
                "compound_visible": parsed.get("compound_visible"),
                "notes": str(parsed.get("notes") or ""),
                "input_tokens": i,
                "output_tokens": o,
            },
        )
        return (
            f"{hhmmss} {model.split('-')[1]}={parsed.get('fullness')}"
            f"({parsed.get('confidence')}) count={parsed.get('vehicle_count')}"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        for msg in pool.map(run, todo):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
