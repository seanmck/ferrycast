"""FerryCast command line.

Designed for cron: every command is non-interactive, exits non-zero on real failure, and
prints a single-line summary suitable for a log.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .db import connect, init_db


def _config(args) -> Config:
    return load_config(args.config)


def _open(config: Config, create: bool = False):
    return connect(config.db_path, create=create)


def _parse_day(value: str) -> date:
    if value == "today":
        return date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


def _print_line_bounds(bounds: dict | None) -> None:
    """The bounds people in the line established, pooled over comparable sailings.

    Indented under the distribution like the deck-space line above it, but kept as its own
    two sentences: one is a claim about what has already failed, the other is not advice at
    all. Collapsing them into a single "arrive by" would lose that distinction.
    """
    if not bounds:
        return
    if bounds["turned_away_at"]:
        print(
            f"  someone who joined at {bounds['turned_away_at']} did not get on "
            f"({bounds['turned_away_date']}), so that has been too late"
        )
    if bounds["boarded_by"]:
        print(
            f"  everyone who did get on had joined by {bounds['boarded_by']} "
            f"(across {bounds['n_sailings']} sailing(s) with reports; not a target)"
        )


# --------------------------------------------------------------------------- commands


def cmd_init(args) -> int:
    config = _config(args)
    init_db(config.db_path)
    config.frames_dir.mkdir(parents=True, exist_ok=True)
    print(f"initialised {config.db_path}")
    print(f"frames will be stored under {config.frames_dir}")
    return 0


def cmd_doctor(args) -> int:
    from .deckspace import parse_deck_space
    from .fetching import fetch

    problems: list[str] = []
    try:
        config = _config(args)
    except ConfigError as exc:
        print(f"FAIL  config: {exc}")
        return 1
    print(f"ok    config: {config.source_path}")

    try:
        from .schedule import load_schedule

        blocks = load_schedule(config.schedule_path)
        departures = sum(len(b.departures) for b in blocks)
        print(f"ok    schedule: {len(blocks)} block(s), {departures} departure times")
    except ConfigError as exc:
        problems.append(f"schedule: {exc}")
        print(f"FAIL  schedule: {exc}")

    if config.db_path.exists():
        print(f"ok    database: {config.db_path}")
    else:
        problems.append("database missing — run `ferrycast init`")
        print(f"FAIL  database: not found at {config.db_path}")

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("ok    ANTHROPIC_API_KEY is set")
    else:
        print("warn  ANTHROPIC_API_KEY not set — extraction will fail (capture is unaffected)")

    for terminal in config.route.terminals:
        if not terminal.webcam_url:
            problems.append(f"{terminal.code}: no webcam_url configured")
            print(f"FAIL  {terminal.code} webcam: not configured")
        elif args.offline:
            print(f"skip  {terminal.code} webcam: {terminal.webcam_url}")
        else:
            result = fetch(
                terminal.webcam_url,
                user_agent=config.capture.user_agent,
                timeout=config.capture.timeout_seconds,
                max_retries=0,
            )
            if not result.ok:
                problems.append(f"{terminal.code} webcam unreachable: {result.error}")
                print(f"FAIL  {terminal.code} webcam: {result.error}")
            else:
                from .capture import sniff_extension

                ext = sniff_extension(result.content or b"")
                if ext:
                    print(
                        f"ok    {terminal.code} webcam: {ext}, "
                        f"{len(result.content or b'') // 1024} KiB"
                    )
                else:
                    problems.append(
                        f"{terminal.code} webcam URL returns "
                        f"{result.content_type or 'unknown content'}, not an image"
                    )
                    print(f"FAIL  {terminal.code} webcam: not an image ({result.content_type})")

        if not terminal.deck_space_url:
            print(f"warn  {terminal.code} deck space: not configured")
        elif args.offline:
            print(f"skip  {terminal.code} deck space: {terminal.deck_space_url}")
        else:
            result = fetch(
                terminal.deck_space_url,
                user_agent=config.capture.user_agent,
                timeout=config.capture.timeout_seconds,
                max_retries=0,
            )
            if not result.ok or not result.text:
                print(f"warn  {terminal.code} deck space: {result.error or 'no text'}")
            else:
                rows = parse_deck_space(result.text)
                if rows:
                    print(f"ok    {terminal.code} deck space: parsed {len(rows)} sailing row(s)")
                else:
                    print(f"warn  {terminal.code} deck space: page fetched but nothing parsed")

    if problems:
        print(f"\n{len(problems)} problem(s) need attention:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nall checks passed")
    return 0


def cmd_capture(args) -> int:
    from .capture import capture_once

    config = _config(args)
    conn = _open(config, create=True)
    outcomes = capture_once(conn, config)
    for outcome in outcomes:
        if outcome.ok:
            print(f"ok    {outcome.terminal}: {outcome.path}")
        elif outcome.skipped:
            print(f"skip  {outcome.terminal}: {outcome.error}")
        else:
            print(f"fail  {outcome.terminal}: {outcome.error}")
    attempted = [o for o in outcomes if not o.skipped]
    # A partial failure is still a working pipeline; only a total loss is an error exit.
    return 0 if any(o.ok for o in attempted) or not attempted else 1


def cmd_scrape(args) -> int:
    from .deckspace import scrape_once

    config = _config(args)
    conn = _open(config, create=True)
    results = scrape_once(conn, config)
    for result in results:
        if result.get("skipped"):
            print(f"skip  {result['terminal']}: no deck_space_url configured")
        elif result["ok"]:
            print(f"ok    {result['terminal']}: {result['rows']} row(s)")
        else:
            print(f"fail  {result['terminal']}: {result.get('error')}")
    attempted = [r for r in results if not r.get("skipped")]
    return 0 if any(r["ok"] for r in attempted) or not attempted else 1


def cmd_marine(args) -> int:
    from .db import init_db
    from .marine import refresh

    config = _config(args)
    if config.route.marine is None:
        print("skip  no [route.marine] configured; the marine forecast is off")
        return 0
    # `init_db` rather than `_open`: this command introduced a table, and an install that
    # predates it would otherwise have to be told to run `init` first. `ferrycast run` does
    # the same before it serves, so the two paths agree.
    conn = init_db(config.db_path)
    result = refresh(conn, config)
    if not result["ok"]:
        print(f"fail  {result.get('error')}")
        return 1
    print(
        f"ok    {result['area']} ({result['location']}) issued {result['issued_at']}: "
        f"{result['rows']} new day(s) of {result['days']}"
    )
    if result.get("warning"):
        print(f"WARN  {result['warning']}")
    return 0


def cmd_extract(args) -> int:
    from .timeutil import iso, now_utc
    from .vision import extract_frames, extract_pending

    config = _config(args)
    conn = _open(config)
    since = None
    if args.since:
        from datetime import datetime
        from datetime import time as time_cls

        since = iso(
            datetime.combine(_parse_day(args.since), time_cls.min, tzinfo=config.tz)
        )
    elif args.days:
        since = iso(now_utc() - timedelta(days=args.days))

    if args.essential or args.for_date:
        from .selection import essential_frames_for_day, frames_for_sailing
        from .timeutil import combine_local, parse_hhmm

        day = _parse_day(args.for_date or "yesterday")
        if args.time:
            if not args.origin:
                print("--time needs --origin as well", file=sys.stderr)
                return 2
            departure = combine_local(day, parse_hhmm(args.time), config.tz)
            frames = frames_for_sailing(conn, config, origin=args.origin, departure=departure)
            scope = f"the {args.origin} {args.time} sailing on {day}"
        else:
            frames = essential_frames_for_day(conn, config, day, origin=args.origin)
            scope = f"key frames for {day}" + (f" from {args.origin}" if args.origin else "")
        if args.limit:
            frames = frames[: args.limit]
        print(f"selected {len(frames)} frame(s) — {scope}")
        stats = extract_frames(conn, config, frames, dry_run=args.dry_run)
    else:
        stats = extract_pending(
            conn,
            config,
            limit=args.limit,
            since=since,
            terminal=args.terminal,
            dry_run=args.dry_run,
        )
    if args.dry_run:
        print(f"{stats.considered} frame(s) awaiting extraction (dry run, nothing sent)")
        return 0
    print(
        f"extracted {stats.extracted}, dark-skipped {stats.skipped_dark}, "
        f"flagged unusable {stats.flagged_unusable}, failed {stats.failed} "
        f"— ${stats.cost_usd:.4f} this run"
    )
    for error in stats.errors[:10]:
        print(f"  ! {error}")
    if len(stats.errors) > 10:
        print(f"  ! ... and {len(stats.errors) - 10} more")
    return 1 if stats.failed and not stats.extracted else 0


def cmd_check(args) -> int:
    """On-demand: read the queue right now and pair it with days like this one."""
    from .query import OUTCOME_LABELS
    from .status import check_and_compare

    config = _config(args)
    conn = _open(config)

    origin = args.origin
    if not origin:
        from .query import upcoming_sailings

        upcoming = upcoming_sailings(config, limit=1)
        if not upcoming:
            print("no upcoming sailings; pass --origin", file=sys.stderr)
            return 1
        origin = upcoming[0].origin

    result = check_and_compare(
        conn,
        config,
        origin=origin,
        target_date=_parse_day(args.date) if args.date else None,
        depart_hhmm=args.time,
        capture_fresh=not args.no_capture,
    )

    status = result["status"]
    sailing = result["sailing"]
    terminal = config.route.terminal(origin)
    print(f"{terminal.name} -> {config.route.terminal(sailing['destination']).name}")

    latest = status["latest"]
    if latest and latest["usable"]:
        beyond = "+" if latest["queue_beyond_frame"] else ""
        dock = ", vessel at dock" if latest["ferry_at_dock"] else ""
        print(
            f"now:  {latest['vehicle_count']}{beyond} vehicles waiting "
            f"(seen {latest['observed_at_local']}, {latest['age_minutes']} min ago{dock})"
        )
        trend = [r for r in status["trend"] if r["usable"]][::-1]
        if len(trend) > 1:
            counts = " -> ".join(f"{r['vehicle_count']}" for r in trend)
            print(f"trend: {counts} (oldest to newest)")
    else:
        print("now:  no usable reading")

    history = result.get("history")
    if history and history["n"]:
        print(f"\nhistory for the {sailing['depart_hhmm']} on days like this (n={history['n']}):")
        for outcome, count in history["counts"].items():
            share = history["shares"][outcome]
            print(f"  {OUTCOME_LABELS[outcome]:<26} {count:>3}  {share:>5.0%}")
        if history.get("typical_fill_minutes_before") is not None:
            print(
                f"  typically ran out of room {history['typical_fill_minutes_before']} min "
                f"before departure (about {history['typical_fill_local']})"
            )
        _print_line_bounds(result.get("line_bounds"))
    elif history:
        print(f"\nno comparable history yet for the {sailing['depart_hhmm']}")

    for note in status["notes"]:
        print(f"note: {note}")
    print(f"\ncost: ${result['cost_usd']:.4f} ({status['frames_read']} frame(s) read)")
    return 0


def cmd_aggregate(args) -> int:
    from .aggregate import aggregate_range, observed_date_range

    config = _config(args)
    conn = _open(config)

    if args.all:
        observed = observed_date_range(conn, config)
        if not observed:
            print("no frames or deck-space readings collected yet; nothing to aggregate")
            return 0
        start, end = observed
    elif args.start or args.end:
        start = _parse_day(args.start) if args.start else _parse_day(args.end)
        end = _parse_day(args.end) if args.end else start
    else:
        target = _parse_day(args.date or "today")
        start = end = target

    counts = aggregate_range(conn, config, start, end)
    total = sum(counts.values())
    print(f"{start} to {end}: {total} sailing(s)")
    for outcome, count in counts.items():
        print(f"  {outcome:<13} {count}")
    return 0


def cmd_query(args) -> int:
    from .query import (
        OUTCOME_LABELS,
        comparable_report_bounds,
        default_sailing_time,
        query_distribution,
        sailing_times,
        upcoming_sailings,
    )

    config = _config(args)
    conn = _open(config)

    if args.origin:
        origin = args.origin
        target = _parse_day(args.date or "today")
        times = sailing_times(config, origin, target)
        chosen = args.time or default_sailing_time(config, times, target)
    else:
        upcoming = upcoming_sailings(config, limit=1)
        if not upcoming:
            print("no upcoming sailings in the schedule")
            return 1
        nxt = upcoming[0]
        origin, target, chosen = nxt.origin, nxt.service_date, nxt.depart_hhmm

    if not chosen:
        print("no sailings scheduled for that terminal and date")
        return 1

    result = query_distribution(
        conn, config, origin=origin, target_date=target, depart_hhmm=chosen
    )
    terminal = config.route.terminal(origin)
    print(f"{terminal.name} -> {config.route.terminal(terminal.destination).name}")
    print(f"{target} at {chosen} ({result.day_type}, {result.season})")
    print(f"n = {result.n} comparable sailing(s), match: {result.match_level}")
    for relaxation in result.relaxations:
        print(f"  note: {relaxation}")
    if result.n:
        for outcome, count in result.counts.items():
            share = result.shares[outcome]
            bar = "#" * round(share * 30)
            print(f"  {OUTCOME_LABELS[outcome]:<26} {count:>3}  {share:>5.0%} {bar}")
        if result.typical_fill_minutes_before is not None:
            print(
                f"\n  typically ran out of room {result.typical_fill_minutes_before} min "
                f"before departure (about {result.typical_fill_local})"
            )
        _print_line_bounds(
            comparable_report_bounds(
                conn,
                config,
                origin=origin,
                target_date=target,
                depart_hhmm=chosen,
                cutline_minutes_before=result.typical_fill_minutes_before,
            )
        )
        if result.evidence_note:
            print(f"  {result.evidence_note}")
        if args.verbose:
            print("  dates:")
            for sample in result.samples:
                print(
                    f"    {sample.service_date} {sample.depart_hhmm} {sample.outcome}"
                    f" peak={sample.peak_queue} carryover={sample.carryover}"
                )
    else:
        print("  no comparable history recorded yet")
    return 0


def cmd_next(args) -> int:
    from .query import upcoming_sailings

    config = _config(args)
    for sailing in upcoming_sailings(config, origin=args.origin, limit=args.limit):
        print(
            f"{sailing.scheduled_departure:%Y-%m-%d %H:%M}  {sailing.origin} -> "
            f"{sailing.destination}  ({sailing.day_type}, {sailing.season})"
        )
    return 0


def cmd_export(args) -> int:
    from .export import export

    config = _config(args)
    conn = _open(config)
    body = export(conn, args.dataset, args.format, since=args.since, until=args.until)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(body)
    return 0


def cmd_health(args) -> int:
    from .maintenance import format_health, health_report

    config = _config(args)
    conn = _open(config)
    report = health_report(conn, config, window_days=args.window)
    print(format_health(report))
    return 1 if (args.strict and not report.healthy) else 0


def cmd_prune(args) -> int:
    from .maintenance import prune_frames

    config = _config(args)
    conn = _open(config)
    result = prune_frames(conn, config, dry_run=args.dry_run)
    verb = "would remove" if args.dry_run else "removed"
    if result.deleted:
        print(f"{verb} {result.deleted} frame file(s) past the retention horizon")
    if result.downsampled:
        print(f"{verb} {result.downsampled} frame file(s) thinning older days to 1/hour")
    if result.thinned_unextracted:
        print(
            f"{verb} {result.thinned_unextracted} aged unread frame(s), keeping the ones "
            "an extraction would use — every sailing is still analysable"
        )
    total = result.deleted + result.downsampled + result.thinned_unextracted
    if total:
        print(f"{'would free' if args.dry_run else 'freed'} {result.bytes_freed / 1e6:.1f} MB")
    else:
        print("nothing to prune")
    if result.kept_unextracted:
        print(
            f"holding {result.kept_unextracted} unread frame(s) past their retention date "
            "so they can still be analysed — extract them, or set "
            "retention.keep_unextracted = false"
        )
    return 0


def cmd_tag(args) -> int:
    from .maintenance import add_tag, list_tags

    config = _config(args)
    conn = _open(config)
    if args.tag_command == "add":
        created = add_tag(conn, _parse_day(args.date), args.tag, args.note)
        print("added" if created else "already present")
    else:
        rows = list_tags(conn, _parse_day(args.date) if args.date else None)
        for row in rows:
            print(f"{row['service_date']}  {row['tag']}  {row['note'] or ''}".rstrip())
        if not rows:
            print("no tags recorded")
    return 0


def cmd_import_records(args) -> int:
    """Seed history from a hand-built CSV (known overload days, spreadsheet exports)."""
    import csv

    from .aggregate import SailingRecord, store_record, upsert_sailings
    from .schedule import Sailing, day_type, season
    from .timeutil import combine_local, parse_hhmm

    config = _config(args)
    conn = _open(config)
    destinations = {t.code: t.destination for t in config.route.terminals}

    imported = 0
    with open(args.path, newline="", encoding="utf-8") as fh:
        for line_no, row in enumerate(csv.DictReader(fh), start=2):
            try:
                service_date = date.fromisoformat(row["service_date"].strip())
                origin = row["origin"].strip()
                hhmm = row["depart_hhmm"].strip()
                outcome = row["outcome"].strip()
            except (KeyError, ValueError) as exc:
                print(f"line {line_no}: skipped ({exc})")
                continue
            if origin not in destinations:
                print(f"line {line_no}: skipped (unknown terminal {origin!r})")
                continue
            if outcome not in ("boarded", "waited_1", "waited_2plus", "cancelled"):
                print(f"line {line_no}: skipped (unknown outcome {outcome!r})")
                continue

            departure = combine_local(service_date, parse_hhmm(hhmm), config.tz)
            upsert_sailings(
                conn,
                [
                    Sailing(
                        route=config.route.id,
                        origin=origin,
                        destination=destinations[origin],
                        service_date=service_date,
                        scheduled_departure=departure,
                        depart_hhmm=hhmm,
                        day_type=day_type(service_date),
                        season=season(service_date),
                    )
                ],
            )
            sailing_id = conn.execute(
                "SELECT id FROM sailings WHERE origin = ? AND scheduled_departure = ?",
                (origin, departure.isoformat()),
            ).fetchone()["id"]
            carryover = row.get("carryover")
            store_record(
                conn,
                SailingRecord(
                    sailing_id=sailing_id,
                    peak_queue=int(row["peak_queue"]) if row.get("peak_queue") else None,
                    queue_at_departure=None,
                    residual_queue=None,
                    carryover=int(carryover) if carryover else None,
                    overload=outcome in ("waited_1", "waited_2plus"),
                    cancelled=outcome == "cancelled",
                    outcome=outcome,
                    n_frames=0,
                    confidence=None,
                    queue_truncated=False,
                    deck_space_min=None,
                    method=f"import:{row.get('source', 'manual')}",
                ),
            )
            imported += 1
    print(f"imported {imported} record(s) from {args.path}")
    return 0


def cmd_fill(args) -> int:
    """Pay for one slot's history, on request.

    The frames are already archived; this is the step that costs money, so it is a
    deliberate command rather than something the schedule does to the whole backlog.
    """
    from .backfill import (
        DEFAULT_MAX_SAILINGS,
        describe,
        estimate_frames,
        fill_for_slot,
        fillable_sailings,
    )

    config = _config(args)
    conn = _open(config)
    day = _parse_day(args.date or "today")
    limit = args.max or DEFAULT_MAX_SAILINGS

    if args.dry_run:
        candidates = estimate_frames(
            conn,
            config,
            fillable_sailings(
                conn,
                config,
                origin=args.origin,
                target_date=day,
                depart_hhmm=args.time,
                limit=limit,
            ),
        )
        frames = sum(c.frames for c in candidates)
        print(f"{len(candidates)} comparable sailing(s) without a record, {frames} frame(s) to read")
        for candidate in candidates:
            note = f"{candidate.frames} frame(s)" if candidate.frames else "no archived frames"
            print(f"  {candidate.service_date} {candidate.depart_hhmm}  {note}")
        return 0

    result = fill_for_slot(
        conn,
        config,
        origin=args.origin,
        target_date=day,
        depart_hhmm=args.time,
        max_sailings=limit,
    )
    print(describe(result))
    for error in result.errors:
        print(f"  {error}", file=sys.stderr)
    return 0


def _serve(args, *, with_scheduler: bool) -> int:
    import uvicorn

    if args.config:
        os.environ["FERRYCAST_CONFIG"] = str(args.config)
    config = _config(args)  # fail fast on a bad config rather than inside the worker

    # Container hosts hand the port over in the environment.
    port = args.port or int(os.environ.get("PORT", 8000))

    if with_scheduler:
        from .db import init_db
        from .scheduler import start_background

        init_db(config.db_path)
        config.frames_dir.mkdir(parents=True, exist_ok=True)
        start_background(config)

    uvicorn.run(
        "ferrycast.web.app:get_app",
        factory=True,
        host=args.host,
        port=port,
        reload=getattr(args, "reload", False),
        log_level="info",
    )
    return 0


def cmd_serve(args) -> int:
    return _serve(args, with_scheduler=args.with_scheduler)


def cmd_run(args) -> int:
    """Web UI plus the scheduler in one process — for hosts without cron."""
    return _serve(args, with_scheduler=True)


def cmd_schedule(args) -> int:
    from .scheduler import describe, run_due

    config = _config(args)
    conn = _open(config, create=True)
    if args.once:
        ran = run_due(conn, config)
        print(f"ran: {', '.join(ran) if ran else 'nothing was due'}")
        return 0
    for row in describe(conn, config):
        marker = "due now" if row["due"] else f"next {row['next_due']}"
        print(
            f"{row['job']:<10} every {row['interval_minutes']:>5} min  "
            f"last {row['last_run'] or 'never':<22} {marker}"
        )
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ferrycast", description="Historical ferry wait tracker."
    )
    parser.add_argument("--version", action="version", version=f"ferrycast {__version__}")
    parser.add_argument("-c", "--config", help="path to ferrycast.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and data directories").set_defaults(
        func=cmd_init
    )

    doctor = sub.add_parser("doctor", help="check configuration and remote sources")
    doctor.add_argument("--offline", action="store_true", help="skip network checks")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("capture", help="capture one frame per terminal (R1)").set_defaults(
        func=cmd_capture
    )
    sub.add_parser("scrape", help="scrape current deck space (R2)").set_defaults(func=cmd_scrape)

    sub.add_parser(
        "marine", help="fetch the ECCC marine forecast for this route's waters"
    ).set_defaults(func=cmd_marine)

    extract = sub.add_parser("extract", help="run vision extraction over stored frames (R3)")
    extract.add_argument("--limit", type=int, help="maximum frames this run")
    extract.add_argument("--since", help="only frames on or after this date")
    extract.add_argument("--days", type=int, help="only frames from the last N days")
    extract.add_argument("--terminal", help="restrict to one terminal code")
    extract.add_argument("--dry-run", action="store_true", help="report the backlog only")
    extract.add_argument(
        "--essential",
        action="store_true",
        help="read only the few frames per sailing that determine its outcome",
    )
    extract.add_argument(
        "--for-date", help="target one service date (implies --essential; default yesterday)"
    )
    extract.add_argument("--origin", help="with --for-date, restrict to one terminal")
    extract.add_argument(
        "--time", help="with --for-date and --origin, read one sailing's full window"
    )
    extract.set_defaults(func=cmd_extract)

    check = sub.add_parser(
        "check", help="on-demand: read the queue now and compare with days like this"
    )
    check.add_argument("--origin", help="terminal code you are leaving from")
    check.add_argument("--date", help="target date (default: today)")
    check.add_argument("--time", help="sailing time HH:MM (default: next departure)")
    check.add_argument(
        "--no-capture",
        action="store_true",
        help="use stored frames only; do not fetch a fresh one",
    )
    check.set_defaults(func=cmd_check)

    fill = sub.add_parser(
        "fill",
        help="on-demand: analyse the archived frames that answer one slot's history",
    )
    fill.add_argument("--origin", required=True, help="terminal code you are leaving from")
    fill.add_argument("--date", help="target date (default: today)")
    fill.add_argument("--time", required=True, help="sailing time HH:MM")
    fill.add_argument(
        "--max", type=int, default=None, help="most comparable sailings to read (default 6)"
    )
    fill.add_argument(
        "--dry-run", action="store_true", help="report what it would read and cost, spend nothing"
    )
    fill.set_defaults(func=cmd_fill)

    aggregate = sub.add_parser("aggregate", help="roll frames up into sailing records (R4)")
    aggregate.add_argument("--date", help="a single service date (default: today)")
    aggregate.add_argument("--start", help="start of a date range")
    aggregate.add_argument("--end", help="end of a date range")
    aggregate.add_argument("--all", action="store_true", help="every day with captured frames")
    aggregate.set_defaults(func=cmd_aggregate)

    query = sub.add_parser("query", help="the day-like-today distribution (R5)")
    query.add_argument("--origin", help="terminal code to depart from")
    query.add_argument("--date", help="target date (default: today)")
    query.add_argument("--time", help="sailing time HH:MM")
    query.add_argument("-v", "--verbose", action="store_true", help="list the sample dates")
    query.set_defaults(func=cmd_query)

    nxt = sub.add_parser("next", help="list upcoming scheduled sailings")
    nxt.add_argument("--origin")
    nxt.add_argument("--limit", type=int, default=6)
    nxt.set_defaults(func=cmd_next)

    exporter = sub.add_parser("export", help="export raw observations")
    exporter.add_argument("dataset", choices=["frames", "sailings", "deck_space"])
    exporter.add_argument("--format", choices=["csv", "json"], default="csv")
    exporter.add_argument("--since")
    exporter.add_argument("--until")
    exporter.add_argument("--out", help="write to this file instead of stdout")
    exporter.set_defaults(func=cmd_export)

    health = sub.add_parser("health", help="capture uptime and coverage report")
    health.add_argument("--window", type=int, default=7, help="days to look back")
    health.add_argument("--strict", action="store_true", help="exit non-zero if unhealthy")
    health.set_defaults(func=cmd_health)

    prune = sub.add_parser("prune", help="apply the frame retention policy")
    prune.add_argument("--dry-run", action="store_true")
    prune.set_defaults(func=cmd_prune)

    tag = sub.add_parser("tag", help="manual event tags (festivals, closures)")
    tag_sub = tag.add_subparsers(dest="tag_command", required=True)
    tag_add = tag_sub.add_parser("add")
    tag_add.add_argument("date")
    tag_add.add_argument("tag")
    tag_add.add_argument("--note")
    tag_list = tag_sub.add_parser("list")
    tag_list.add_argument("--date")
    tag.set_defaults(func=cmd_tag)

    importer = sub.add_parser("import-records", help="seed history from a CSV of known sailings")
    importer.add_argument("path")
    importer.set_defaults(func=cmd_import_records)

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=None, help="defaults to $PORT or 8000")
    serve.add_argument("--reload", action="store_true")
    serve.add_argument(
        "--with-scheduler",
        action="store_true",
        help="also run capture/scrape/aggregate on a timer in this process",
    )
    serve.set_defaults(func=cmd_serve)

    run = sub.add_parser(
        "run", help="web UI plus the scheduler in one process (for hosts without cron)"
    )
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=None, help="defaults to $PORT or 8000")
    run.set_defaults(func=cmd_run)

    schedule = sub.add_parser("schedule", help="show or run the scheduled jobs")
    schedule.add_argument(
        "--once", action="store_true", help="run whatever is due now, then exit"
    )
    schedule.set_defaults(func=cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
