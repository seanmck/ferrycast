from dataclasses import replace
from datetime import date, timedelta

from ferrycast.export import export, rows_for
from ferrycast.maintenance import add_tag, health_report, list_tags, prune_frames
from ferrycast.timeutil import iso, now_utc

from .conftest import add_observation
from .test_query import seed_record


def _add_frame_file(conn, config, terminal, moment, content=b"x" * 100, *, extracted=True):
    """A captured frame, extracted by default.

    Retention only reclaims frames that have already been read — an unread frame is the
    only record of its moment — so these tests describe the normal, extracted case.
    Un-extracted pruning behaviour is covered in test_ondemand.py.
    """
    path = config.frames_dir / terminal / f"{iso(moment).replace(':', '-')}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    cur = conn.execute(
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
    if extracted:
        conn.execute(
            """INSERT INTO observations
                   (frame_id, prompt_version, model, vehicle_count, usable, created_at)
               VALUES (?, ?, 'test', 20, 1, ?)""",
            (cur.lastrowid, config.vision.prompt_version, iso(moment)),
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


def _with_scheduled_capture(config):
    """Capture health only applies to installs that actually schedule capture."""
    return replace(config, capture=replace(config.capture, scheduled=True))


def test_health_flags_a_stalled_pipeline(conn, config):
    add_observation(conn, config, "SLT", now_utc() - timedelta(days=3), 10)
    report = health_report(conn, _with_scheduled_capture(config))
    assert not report.healthy
    assert any("no successful capture since" in p for p in report.problems)


def test_health_ignores_capture_when_it_is_not_scheduled(conn, config):
    """If an install has deliberately turned archiving off, a quiet camera is not a fault."""
    no_capture = replace(config, capture=replace(config.capture, scheduled=False))
    report = health_report(conn, no_capture)
    assert not any("capture" in p for p in report.problems)


def test_health_flags_a_silent_deck_space_feed(conn, config):
    """Deck space is the historical record now, so its silence is the real alarm."""
    report = health_report(conn, config)
    assert any("deck-space readings" in p for p in report.problems)


def test_health_flags_missing_camera_configuration(conn, config):
    blind = _with_scheduled_capture(
        replace(
            config,
            routes=tuple(
                replace(r, terminals=tuple(replace(t, webcam_url="") for t in r.terminals))
                for r in config.routes
            ),
        )
    )
    report = health_report(conn, blind)
    assert any("no webcam URLs" in p for p in report.problems)


def test_health_reports_the_extraction_backlog(conn, config):
    for i in range(5):
        _add_frame_file(
            conn, config, "SLT", now_utc() - timedelta(minutes=15 * i), extracted=False
        )
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


def _add_deck_rows(conn, config, statuses):
    """One deck-space row per status, each at its own minute (they are keyed by time)."""
    for i, status in enumerate(statuses):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm, fetch_status)
               VALUES (?, 'SLT', ?, ?, '12:30', ?)""",
            (
                config.route.id,
                iso(now_utc() - timedelta(minutes=i)),
                date.today().isoformat(),
                status,
            ),
        )
    conn.commit()

def test_an_unpublished_board_does_not_count_against_the_scrape_rate(conn, config):
    """One end of this route publishes a departures board and the other does not, so half
    of every scrape cycle has nothing to read. Counted as failures those rows pinned the
    rate near 50% permanently — which is both wrong and useless, because a real parser
    break could no longer move it."""
    statuses = ["ok", "ok", "ok", "not_published", "not_published", "not_published"]
    _add_deck_rows(conn, config, statuses)

    assert health_report(conn, config).deckspace_success_rate == 1.0


def test_a_genuine_parse_failure_still_shows(conn, config):
    _add_deck_rows(conn, config, ["ok", "unparsed", "not_published"])

    assert health_report(conn, config).deckspace_success_rate == 0.5
