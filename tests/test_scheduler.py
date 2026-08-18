"""The in-process scheduler used on container hosts.

Due-ness lives in the database rather than in memory, so a redeploy neither re-runs
everything nor silently skips a cycle. And a job that throws must never take the loop down
with it — an unattended collector that dies on one bad scrape is worse than useless.
"""

from datetime import timedelta

from ferrycast.config import load_config
from ferrycast.scheduler import (
    Job,
    describe,
    due_jobs,
    is_due,
    jobs_for,
    last_run,
    run_due,
)
from ferrycast.timeutil import iso, now_utc


def _record_run(conn, job: str, *, minutes_ago: int):
    conn.execute(
        "INSERT INTO job_runs (job, started_at, ok) VALUES (?, ?, 1)",
        (job, iso(now_utc() - timedelta(minutes=minutes_ago))),
    )
    conn.commit()


def test_everything_is_due_on_a_fresh_database(conn, config):
    names = {job.name for job in due_jobs(conn, config)}
    assert "deckspace" in names
    assert "aggregate" in names


def test_capture_and_scrape_follow_the_configured_interval(config):
    intervals = {job.name: job.interval for job in jobs_for(config)}
    assert intervals["deckspace"] == timedelta(minutes=config.capture.interval_minutes)


def test_a_recent_run_is_not_due_again(conn, config):
    _record_run(conn, "deckspace", minutes_ago=2)
    assert "deckspace" not in {job.name for job in due_jobs(conn, config)}


def test_a_stale_run_becomes_due(conn, config):
    _record_run(conn, "deckspace", minutes_ago=60)
    assert "deckspace" in {job.name for job in due_jobs(conn, config)}


def test_due_ness_survives_a_restart(conn, config):
    """The schedule lives in the volume, so redeploying does not re-run everything."""
    _record_run(conn, "prune", minutes_ago=60)
    reopened = load_config(config.source_path)
    assert "prune" not in {job.name for job in due_jobs(conn, reopened)}


def test_capture_is_skipped_when_no_camera_is_configured(conn, config):
    from dataclasses import replace

    blind = replace(
        config,
        routes=tuple(
            replace(r, terminals=tuple(replace(t, webcam_url="") for t in r.terminals))
            for r in config.routes
        ),
    )
    assert "capture" not in {job.name for job in jobs_for(blind)}


def test_capture_is_skipped_when_archiving_is_turned_off(config):
    from dataclasses import replace

    off = replace(config, capture=replace(config.capture, scheduled=False))
    assert "capture" not in {job.name for job in jobs_for(off)}


def _with_replay_camera(config):
    from dataclasses import replace

    from ferrycast.config import Camera

    camera = Camera(
        id="drivebc244",
        kind="queue_extent",
        image_url="https://example.invalid/244.jpg",
        replay_index_url="https://example.invalid/244/replay/",
        replay_frame_url="https://example.invalid/244/{timestamp}.jpg",
    )
    return replace(
        config,
        routes=tuple(
            replace(
                r,
                terminals=tuple(
                    replace(t, cameras=(camera,)) if t.code == "ERL" else t
                    for t in r.terminals
                ),
            )
            for r in config.routes
        ),
    )


def test_a_camera_with_a_replay_window_is_swept_by_the_scheduler(config):
    """The container's start command is `ferrycast run`, so a collector this scheduler does
    not know about does not run in production at all — however carefully it is written, and
    whatever the crontab example says."""
    assert "replay" not in {job.name for job in jobs_for(config)}
    assert "replay" in {job.name for job in jobs_for(_with_replay_camera(config))}


def test_the_replay_sweep_keeps_its_own_cadence(config):
    """Not the capture interval. The window is retroactive and a day wide, so sweeping it
    every five minutes would refetch nothing and ask for the index 288 times a day."""
    jobs = {job.name: job.interval for job in jobs_for(_with_replay_camera(config))}
    assert jobs["replay"] == timedelta(hours=12)
    assert jobs["replay"] != jobs["capture"]


def test_scraping_still_runs_without_any_camera(conn, config):
    """Deck space is the historical record; it must not depend on the cameras."""
    from dataclasses import replace

    blind = replace(
        config,
        routes=tuple(
            replace(r, terminals=tuple(replace(t, webcam_url="") for t in r.terminals))
            for r in config.routes
        ),
    )
    assert "deckspace" in {job.name for job in jobs_for(blind)}


def test_a_failing_job_does_not_stop_the_others(conn, config):
    from ferrycast import scheduler

    calls = []

    def boom(conn, config):
        raise RuntimeError("scrape exploded")

    def fine(conn, config):
        calls.append("fine")
        return "ok"

    messages = []
    jobs = (
        Job("deckspace", timedelta(minutes=1), boom),
        Job("aggregate", timedelta(minutes=1), fine),
    )
    original = scheduler.JOBS
    scheduler.JOBS = jobs
    try:
        ran = run_due(conn, config, log=messages.append)
    finally:
        scheduler.JOBS = original

    assert calls == ["fine"]                       # the healthy job still ran
    assert set(ran) == {"deckspace", "aggregate"}  # both were attempted
    assert any("FAILED" in m for m in messages)


def test_running_a_job_records_it_so_it_is_not_immediately_due_again(
    conn, config, monkeypatch
):
    # No real network: the URLs point at example.invalid, and the retry backoff would make
    # this test take tens of seconds for no added confidence.
    from ferrycast import capture as capture_module
    from ferrycast import deckspace as deckspace_module
    from ferrycast.fetching import FetchResult

    for module in (capture_module, deckspace_module):
        monkeypatch.setattr(
            module, "fetch", lambda url, **kw: FetchResult(ok=False, error="offline in tests")
        )

    run_due(conn, config)

    # A job_run is recorded even when the fetch fails, which is what marks it done.
    assert last_run(conn, "deckspace") is not None
    assert not is_due(conn, next(j for j in jobs_for(config) if j.name == "deckspace"))


def test_every_job_records_itself_so_nothing_loops(conn, config, monkeypatch):
    """A job that runs without recording a job_run looks perpetually overdue.

    That is not hypothetical: `prune` did exactly this, and would have re-run on every
    30-second tick forever. This checks the property for all jobs at once.
    """
    from ferrycast import capture as capture_module
    from ferrycast import deckspace as deckspace_module
    from ferrycast.fetching import FetchResult

    for module in (capture_module, deckspace_module):
        monkeypatch.setattr(
            module, "fetch", lambda url, **kw: FetchResult(ok=False, error="offline in tests")
        )

    first = run_due(conn, config)
    assert first, "expected everything to be due on a fresh database"

    still_due = [job.name for job in due_jobs(conn, config)]
    assert still_due == [], f"these jobs did not record a run: {still_due}"


def test_describe_reports_the_plan(conn, config):
    _record_run(conn, "aggregate", minutes_ago=5)
    rows = {row["job"]: row for row in describe(conn, config)}
    assert rows["aggregate"]["last_run"]
    assert rows["aggregate"]["due"] is False
    assert rows["deckspace"]["next_due"] == "now"


def test_capture_reads_the_frames_it_just_took(conn, config, monkeypatch, tmp_path):
    """Geometry costs ~19ms a frame, so there is no reason to defer it the way a paid vision
    call has to be. A frame that is read at capture time is also a frame that can be pruned
    later without losing the measurement."""
    from ferrycast import scheduler

    read_calls = {}

    def fake_capture_once(conn, config):
        return []

    class FakeStats:
        considered = read = 2
        unusable = skipped_no_calibration = failed = 0
        errors: list[str] = []

    def fake_extract_pending(conn, config, *, max_reads):
        read_calls["max_reads"] = max_reads
        return FakeStats()

    monkeypatch.setattr("ferrycast.capture.capture_once", fake_capture_once)
    monkeypatch.setattr("ferrycast.lanes.extract_pending", fake_extract_pending)

    detail = scheduler._capture(conn, config)

    assert read_calls["max_reads"] == scheduler.LANE_READS_PER_RUN
    assert "read 2" in detail


def test_capture_aggregates_what_it_just_learned(conn, config, monkeypatch):
    """Evidence and inference land together. The hourly pass alone left finished sailings
    sitting classified-but-invisible for most of an hour — the 12:55 on 2026-08-16 missed
    the run by seconds and read `unknown` until 14:38 — and aggregation costs a second of
    SQLite, so there is no reason for the record to lag the last frame that decides it."""
    from datetime import timedelta

    from ferrycast import scheduler
    from ferrycast.lanes import LaneStats

    monkeypatch.setattr("ferrycast.capture.capture_once", lambda conn, config: [])
    monkeypatch.setattr(
        "ferrycast.lanes.extract_pending", lambda conn, config, *, max_reads: LaneStats()
    )
    window = {}

    def fake_range(conn, config, start, end):
        window["span"] = (start, end)
        return {"boarded": 1, "unknown": 2}

    monkeypatch.setattr("ferrycast.aggregate.aggregate_range", fake_range)

    detail = scheduler._capture(conn, config)

    assert "1 sailing(s) recorded, 2 unknown" in detail
    assert window["span"][1] - window["span"][0] == timedelta(days=2)


def test_a_broken_aggregation_does_not_fail_the_capture(conn, config, monkeypatch):
    """Capture is the one collector whose failure loses data forever — a frame not taken is
    gone. An aggregation bug must stay visible in the job detail without sharing that fate;
    the hourly aggregate job still runs it unguarded and fails loudly on its own."""
    from ferrycast import scheduler
    from ferrycast.lanes import LaneStats

    monkeypatch.setattr("ferrycast.capture.capture_once", lambda conn, config: [])
    monkeypatch.setattr(
        "ferrycast.lanes.extract_pending", lambda conn, config, *, max_reads: LaneStats()
    )

    def broken_range(conn, config, start, end):
        raise RuntimeError("schema drift")

    monkeypatch.setattr("ferrycast.aggregate.aggregate_range", broken_range)

    detail = scheduler._capture(conn, config)

    assert "aggregation failed: schema drift" in detail
