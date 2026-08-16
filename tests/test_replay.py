"""Collecting a camera that publishes its recent past.

The properties worth pinning are the ones that make a daily schedule sufficient where every
other source in this project needs a fifteen-minute one: a sweep is idempotent, it stamps
frames with the feed's time rather than its own, and it retries what an earlier sweep failed
to get. Lose any of those and this quietly becomes an ordinary capture job with a worse
cadence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ferrycast.config import load_config
from ferrycast.db import init_db
from ferrycast.fetching import FetchResult
from ferrycast.replay import ReplayError, parse_index, sweep, sweep_camera

from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

ERL_ANCHOR = '  deck_space_url = "https://example.invalid/erl"\n'
CAMERA_BLOCK = """
  [[route.terminals.cameras]]
  id = "drivebc244"
  kind = "queue_extent"
  image_url = "https://example.invalid/244.jpg"
  replay_index_url = "https://example.invalid/244/replay/"
  replay_frame_url = "https://example.invalid/244/{timestamp}.jpg"
"""

TIMESTAMPS = ["20260814200001", "20260814201501", "20260814203001"]

JPEG = b"\xff\xd8\xff" + b"\x00" * 64


@pytest.fixture
def replay_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(ERL_ANCHOR, ERL_ANCHOR + CAMERA_BLOCK)
    )
    return load_config(config_dir / "ferrycast.toml")


@pytest.fixture
def replay_conn(replay_config):
    conn = init_db(replay_config.db_path)
    yield conn
    conn.close()


@pytest.fixture
def camera(replay_config):
    return replay_config.route.terminal("ERL").camera("drivebc244")


class FakeFeed:
    """Stands in for the camera. Records what was asked for, so a second sweep can be seen
    to ask for nothing."""

    def __init__(self, timestamps=TIMESTAMPS, fail=()):
        self.timestamps = list(timestamps)
        self.fail = set(fail)
        self.requested: list[str] = []

    def __call__(self, url, **kwargs):
        if url.endswith("/replay/"):
            return FetchResult(
                ok=True, text=json.dumps(self.timestamps), content_type="application/json"
            )
        stamp = url.rsplit("/", 1)[-1].removesuffix(".jpg")
        self.requested.append(stamp)
        if stamp in self.fail:
            return FetchResult(ok=False, error="HTTP 503")
        return FetchResult(ok=True, content=JPEG, content_type="image/jpeg")


def _sweep(monkeypatch, conn, config, camera, feed, **kwargs):
    monkeypatch.setattr("ferrycast.replay.fetch", feed)
    return sweep_camera(conn, config, "ERL", camera, **kwargs)


def test_a_sweep_collects_the_whole_published_window(
    monkeypatch, replay_conn, replay_config, camera
):
    feed = FakeFeed()
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, feed)
    assert (stats.offered, stats.fetched, stats.failed) == (3, 3, 0)
    assert feed.requested == TIMESTAMPS


def test_frames_are_stamped_with_the_feeds_time_not_the_sweeps(
    monkeypatch, replay_conn, replay_config, camera
):
    """The property the whole approach rests on. Stamped with the time of the sweep, a day's
    frames would all land at once, every re-run would look like new frames, and the archive
    would say a queue existed at midnight because that is when cron ran."""
    _sweep(monkeypatch, replay_conn, replay_config, camera, FakeFeed())
    rows = [
        row["captured_at"]
        for row in replay_conn.execute("SELECT captured_at FROM frames ORDER BY captured_at")
    ]
    assert rows == [
        "2026-08-14T20:00:01Z",
        "2026-08-14T20:15:01Z",
        "2026-08-14T20:30:01Z",
    ]


def test_sweeping_twice_fetches_nothing_the_second_time(
    monkeypatch, replay_conn, replay_config, camera
):
    """What lets this run on a schedule at all. A sweep that refetched its own window would
    burn the feed's bandwidth daily and rewrite every frame it already had."""
    _sweep(monkeypatch, replay_conn, replay_config, camera, FakeFeed())
    second = FakeFeed()
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, second)
    assert (stats.offered, stats.already_held, stats.fetched) == (3, 3, 0)
    assert second.requested == []
    assert replay_conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3


def test_a_frame_an_earlier_sweep_failed_on_is_retried(
    monkeypatch, replay_conn, replay_config, camera
):
    """The compensation for a daily cadence. A live camera's failure is a permanent hole;
    here the frame is still published, so the next sweep can simply pick it up."""
    first = FakeFeed(fail={TIMESTAMPS[1]})
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, first)
    assert (stats.fetched, stats.failed) == (2, 1)

    second = FakeFeed()
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, second)
    assert second.requested == [TIMESTAMPS[1]]
    assert stats.fetched == 1
    # One row for that moment, now good — not a failed row shadowing a successful refetch.
    rows = replay_conn.execute(
        "SELECT status, path FROM frames WHERE captured_at = '2026-08-14T20:15:01Z'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["path"]


def test_a_dry_run_reports_the_gap_without_filling_it(
    monkeypatch, replay_conn, replay_config, camera
):
    feed = FakeFeed()
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, feed, dry_run=True)
    assert (stats.offered, stats.missing, stats.fetched) == (3, 3, 0)
    assert feed.requested == []
    assert replay_conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0


def test_a_capped_sweep_keeps_the_newest_and_says_what_it_dropped(
    monkeypatch, replay_conn, replay_config, camera
):
    """A silent cap would read as complete coverage of a window it had only partly
    collected."""
    feed = FakeFeed()
    stats = _sweep(monkeypatch, replay_conn, replay_config, camera, feed, limit=2)
    assert stats.capped == 1
    assert feed.requested == TIMESTAMPS[1:]


def test_frames_land_against_the_camera_that_took_them(
    monkeypatch, replay_conn, replay_config, camera
):
    _sweep(monkeypatch, replay_conn, replay_config, camera, FakeFeed())
    rows = {
        (row["terminal"], row["camera"])
        for row in replay_conn.execute("SELECT terminal, camera FROM frames")
    }
    assert rows == {("ERL", "drivebc244")}


def test_an_unreachable_index_is_reported_rather_than_raised(
    monkeypatch, replay_conn, replay_config, camera
):
    monkeypatch.setattr(
        "ferrycast.replay.fetch", lambda url, **kw: FetchResult(ok=False, error="timeout")
    )
    stats = sweep_camera(replay_conn, replay_config, "ERL", camera)
    assert stats.fetched == 0
    assert "timeout" in stats.errors[0]


def test_sweeping_a_route_skips_cameras_with_no_replay_feed(
    monkeypatch, replay_conn, replay_config
):
    monkeypatch.setattr("ferrycast.replay.fetch", FakeFeed())
    runs = sweep(replay_conn, replay_config)
    assert [(s.terminal, s.camera) for s in runs] == [("ERL", "drivebc244")]


def test_a_sweep_with_nothing_missing_counts_as_a_successful_run(
    monkeypatch, replay_conn, replay_config
):
    """Otherwise every run after the first is logged as a failure and `ferrycast health`
    reports an outage that is really a fully collected window."""
    monkeypatch.setattr("ferrycast.replay.fetch", FakeFeed())
    sweep(replay_conn, replay_config)
    monkeypatch.setattr("ferrycast.replay.fetch", FakeFeed())
    sweep(replay_conn, replay_config)
    ok = [
        row["ok"]
        for row in replay_conn.execute(
            "SELECT ok FROM job_runs WHERE job = 'replay' ORDER BY id"
        )
    ]
    assert ok == [1, 1]


# --- index parsing -----------------------------------------------------------------------


def test_the_index_is_sorted_oldest_first_whatever_order_it_arrives_in(camera):
    shuffled = json.dumps([TIMESTAMPS[2], TIMESTAMPS[0], TIMESTAMPS[1]])
    assert parse_index(shuffled, camera) == [
        datetime(2026, 8, 14, 20, 0, 1, tzinfo=UTC),
        datetime(2026, 8, 14, 20, 15, 1, tzinfo=UTC),
        datetime(2026, 8, 14, 20, 30, 1, tzinfo=UTC),
    ]


def test_a_page_where_an_index_was_expected_is_refused(camera):
    """The classic misconfiguration, and it must not read as an empty window — which would
    look exactly like a camera with nothing new to offer."""
    with pytest.raises(ReplayError, match="not JSON"):
        parse_index("<html>not an index</html>", camera)
    with pytest.raises(ReplayError, match="not a list"):
        parse_index('{"cameras": []}', camera)


def test_an_index_in_an_unexpected_time_format_is_refused(camera):
    with pytest.raises(ReplayError, match="parsed as"):
        parse_index(json.dumps(["2026-08-14T20:00:01Z"]), camera)


def test_an_empty_window_is_an_answer_not_an_error(camera):
    assert parse_index("[]", camera) == []
