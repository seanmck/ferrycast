from dataclasses import replace
from datetime import date, timedelta

from ferrycast.export import export, rows_for
from ferrycast.maintenance import add_tag, health_report, list_tags, prune_frames
from ferrycast.timeutil import iso, now_utc

from .conftest import add_observation
from .test_query import seed_record


def _add_frame_file(conn, config, terminal, moment, content=b"x" * 100):
    path = config.frames_dir / terminal / f"{iso(moment).replace(':', '-')}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    conn.execute(
        """INSERT INTO frames (route, terminal, captured_at, service_date, path, bytes, status)
           VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(moment),
            moment.date().isoformat(),
            str(path.relative_to(config.data_dir)),
            len(content),
        ),
    )
    conn.commit()
    return path


def test_prune_removes_expired_frame_files_but_keeps_the_record(conn, config):
    old = now_utc() - timedelta(days=500)
    path = _add_frame_file(conn, config, "SLT", old)

    result = prune_frames(conn, config)

    assert result.deleted == 1
    assert not path.exists()
    row = conn.execute("SELECT path, status FROM frames").fetchone()
    assert row["path"] is None       # the image is gone
    assert row["status"] == "ok"     # but the observation history stays


def test_prune_downsamples_middle_aged_frames_to_one_per_hour(conn, config):
    # Pin to the top of an hour so all four frames land in the same hourly bucket
    # regardless of when the test happens to run.
    base = (now_utc() - timedelta(days=200)).replace(minute=0, second=0, microsecond=0)
    paths = [_add_frame_file(conn, config, "SLT", base + timedelta(minutes=15 * i)) for i in range(4)]

    result = prune_frames(conn, config)

    assert result.downsampled == 3
    assert paths[0].exists()
    assert not any(p.exists() for p in paths[1:])


def test_prune_leaves_recent_frames_alone(conn, config):
    path = _add_frame_file(conn, config, "SLT", now_utc() - timedelta(days=2))
    result = prune_frames(conn, config)
    assert result.deleted == 0 and result.downsampled == 0
    assert path.exists()


def test_prune_dry_run_touches_nothing(conn, config):
    path = _add_frame_file(conn, config, "SLT", now_utc() - timedelta(days=500))
    result = prune_frames(conn, config, dry_run=True)
    assert result.deleted == 1
    assert path.exists()
    assert conn.execute("SELECT path FROM frames").fetchone()["path"] is not None


def test_health_flags_a_stalled_pipeline(conn, config):
    add_observation(conn, config, "SLT", now_utc() - timedelta(days=3), 10)
    report = health_report(conn, config)
    assert not report.healthy
    assert any("no successful capture since" in p for p in report.problems)


def test_health_flags_missing_camera_configuration(conn, config):
    blind = replace(
        config,
        route=replace(
            config.route,
            terminals=tuple(replace(t, webcam_url="") for t in config.route.terminals),
        ),
    )
    report = health_report(conn, blind)
    assert any("no webcam URLs" in p for p in report.problems)


def test_health_reports_the_extraction_backlog(conn, config):
    for i in range(5):
        _add_frame_file(conn, config, "SLT", now_utc() - timedelta(minutes=15 * i))
    report = health_report(conn, config)
    assert report.frames_awaiting_extraction == 5


def test_health_tracks_spend_against_the_budget(conn, config):
    report = health_report(conn, config)
    assert report.budget == 5.0
    assert report.month_to_date_cost == 0.0


def test_tags_are_deduplicated(conn, config):
    assert add_tag(conn, date(2026, 8, 1), "sunshine-music-fest", "annual")
    assert not add_tag(conn, date(2026, 8, 1), "sunshine-music-fest")
    tags = list_tags(conn, date(2026, 8, 1))
    assert len(tags) == 1
    assert tags[0]["note"] == "annual"


def test_export_sailings_as_csv_and_json(conn, config):
    seed_record(conn, config, date(2026, 7, 3), "12:30", "waited_1")

    csv_body = export(conn, "sailings", "csv")
    assert "outcome" in csv_body.splitlines()[0]
    assert "waited_1" in csv_body

    import json

    parsed = json.loads(export(conn, "sailings", "json"))
    assert parsed[0]["outcome"] == "waited_1"
    assert parsed[0]["origin"] == "SLT"


def test_export_frames_includes_per_frame_observations(conn, config):
    add_observation(conn, config, "SLT", now_utc(), 37)
    rows = rows_for(conn, "frames")
    assert rows[0]["vehicle_count"] == 37
    assert rows[0]["terminal"] == "SLT"


def test_export_respects_a_date_window(conn, config):
    add_observation(conn, config, "SLT", now_utc() - timedelta(days=10), 5)
    add_observation(conn, config, "SLT", now_utc(), 9)
    recent = rows_for(conn, "frames", since=iso(now_utc() - timedelta(days=1)))
    assert [row["vehicle_count"] for row in recent] == [9]
