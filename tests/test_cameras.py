"""More than one camera at a terminal.

Two things are pinned here. The first is the migration, which rebuilds `frames` and so runs
straight past a cascade that would take the entire extracted archive with it. The second is
scoping: an extra camera is a different scene, and every consumer that was written when a
terminal meant one camera has to keep meaning that unless it says otherwise. The expensive
version of getting this wrong is the vision extractor, which would post highway frames to a
paid model to answer a question about a compound that is not in the picture.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ferrycast.config import MAIN_CAMERA, ConfigError, load_config
from ferrycast.db import SCHEMA_VERSION, init_db, schema_version

from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

REPO = Path(__file__).resolve().parents[1]

# The shape `frames` had at v9, before cameras were nameable.
FRAMES_V9 = """
CREATE TABLE frames (
    id            INTEGER PRIMARY KEY,
    route         TEXT    NOT NULL,
    terminal      TEXT    NOT NULL,
    captured_at   TEXT    NOT NULL,
    service_date  TEXT    NOT NULL,
    path          TEXT,
    sha256        TEXT,
    bytes         INTEGER,
    width         INTEGER,
    height        INTEGER,
    status        TEXT    NOT NULL,
    error         TEXT,
    UNIQUE (terminal, captured_at)
)
"""


def _v9_database(path):
    """A database at v9 holding one frame and one observation extracted from it."""
    conn = init_db(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE frames")
    conn.executescript(FRAMES_V9)
    conn.execute(
        """INSERT INTO frames (id, route, terminal, captured_at, service_date, path, status)
           VALUES (7, 'route7', 'SLT', '2026-08-14T20:00:01Z', '2026-08-14', 'a.jpg', 'ok')"""
    )
    conn.execute(
        """INSERT INTO observations (frame_id, prompt_version, model, fullness, usable,
                                     created_at)
           VALUES (7, 'v2', 'test', 'heavy', 1, '2026-08-14T20:05:00Z')"""
    )
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()


def test_the_migration_does_not_cascade_the_archive_away(tmp_path):
    """`observations.frame_id` references frames ON DELETE CASCADE, so rebuilding the table
    with foreign keys enforced would delete every extracted observation there has ever been —
    in a migration whose diff reads like a rename."""
    path = tmp_path / "v9.db"
    _v9_database(path)

    conn = init_db(path)

    assert schema_version(conn) == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    # And still attached to the frame it was extracted from, by the same id.
    row = conn.execute(
        "SELECT f.terminal, f.camera, o.fullness FROM observations o "
        "JOIN frames f ON f.id = o.frame_id"
    ).fetchone()
    assert (row["terminal"], row["camera"], row["fullness"]) == ("SLT", MAIN_CAMERA, "heavy")
    conn.close()


def test_frames_already_archived_become_the_main_camera(tmp_path):
    path = tmp_path / "v9.db"
    _v9_database(path)
    conn = init_db(path)
    cameras = [row["camera"] for row in conn.execute("SELECT camera FROM frames")]
    assert cameras == [MAIN_CAMERA]
    conn.close()


def test_the_migration_restores_foreign_key_enforcement(tmp_path):
    """Leaving keys off would silently disarm every cascade in the database for the life of
    the process, which is a far worse bug than the one the migration was written to avoid."""
    path = tmp_path / "v9.db"
    _v9_database(path)
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_running_the_migration_twice_changes_nothing(tmp_path):
    path = tmp_path / "v9.db"
    _v9_database(path)
    init_db(path).close()
    conn = init_db(path)
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    conn.close()


def test_two_cameras_can_hold_the_same_terminal_and_moment(conn, config):
    """The reason `camera` is in the key rather than merely in a column. A replay feed stamps
    on the quarter hour and a cron capture runs whenever cron runs; sooner or later they land
    on the same second, and the INSERT OR IGNORE every writer here uses would drop one."""
    for camera in (MAIN_CAMERA, "drivebc244"):
        conn.execute(
            """INSERT OR IGNORE INTO frames
                   (route, terminal, camera, captured_at, service_date, path, status)
               VALUES ('route7', 'ERL', ?, '2026-08-14T20:00:01Z', '2026-08-14', ?, 'ok')""",
            (camera, f"{camera}.jpg"),
        )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2


def test_one_camera_still_cannot_hold_the_same_moment_twice(conn):
    conn.execute(
        """INSERT INTO frames (route, terminal, camera, captured_at, service_date, status)
           VALUES ('route7', 'ERL', 'drivebc244', '2026-08-14T20:00:01Z', '2026-08-14', 'ok')"""
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO frames (route, terminal, camera, captured_at, service_date, status)
               VALUES ('route7', 'ERL', 'drivebc244', '2026-08-14T20:00:01Z', '2026-08-14',
                       'ok')"""
        )


# --- scoping -----------------------------------------------------------------------------


def _frame(conn, terminal, camera, minute, config=None):
    if config is not None:
        # Only the background builder looks at the file; it skips rows whose frame has been
        # pruned off disk, so a row without one would be counted as no sample at all.
        path = config.data_dir / f"{terminal}_{camera}_{minute}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    conn.execute(
        """INSERT INTO frames (route, terminal, camera, captured_at, service_date, path, status)
           VALUES ('route7', ?, ?, ?, '2026-08-14', ?, 'ok')""",
        (
            terminal,
            camera,
            f"2026-08-14T20:{minute:02d}:01Z",
            f"{terminal}_{camera}_{minute}.jpg",
        ),
    )
    conn.commit()


def test_an_extra_camera_is_never_sent_to_the_paid_extractor_by_default(conn):
    """The expensive mistake. A highway camera archived to measure an overflow would
    otherwise be posted frame by frame to a model, and billed for, to be asked about a
    compound it cannot see."""
    from ferrycast.vision import pending_frames

    _frame(conn, "ERL", MAIN_CAMERA, 0)
    _frame(conn, "ERL", "drivebc244", 15)

    assert [r["camera"] for r in pending_frames(conn, "v1", 10)] == [MAIN_CAMERA]
    # Reachable, but only by asking for it.
    assert len(pending_frames(conn, "v1", 10, camera=None)) == 2
    assert [r["camera"] for r in pending_frames(conn, "v1", 10, camera="drivebc244")] == [
        "drivebc244"
    ]


def test_an_extra_camera_is_not_read_as_a_lane_grid_by_default(conn):
    from ferrycast.lanes import pending_frames

    _frame(conn, "ERL", MAIN_CAMERA, 0)
    _frame(conn, "ERL", "drivebc244", 15)
    assert [r["camera"] for r in pending_frames(conn, 10)] == [MAIN_CAMERA]


def test_an_on_demand_check_reads_the_terminal_camera_not_the_highway_one(conn, config):
    """"How is the queue where I'm standing" is a question about the compound, and only the
    terminal's own camera is looking at it."""
    from datetime import datetime

    from ferrycast.selection import recent_frames
    from ferrycast.timeutil import UTC

    _frame(conn, "ERL", MAIN_CAMERA, 0)
    _frame(conn, "ERL", "drivebc244", 15)
    at = datetime(2026, 8, 14, 20, 30, tzinfo=UTC)
    rows = recent_frames(conn, config, "ERL", max_age_minutes=120, limit=10, at=at)
    assert [r["camera"] for r in rows] == [MAIN_CAMERA]


def test_backgrounds_are_built_per_camera_not_per_terminal(conn, config):
    """A reference is a picture of one view. Median two cameras together and the result is a
    picture of nowhere — the pixel at (100, 100) is compound in one and treeline in the
    other."""
    from ferrycast.lanes import backgrounds_dir, build_backgrounds

    assert backgrounds_dir(config.data_dir, "ERL") != backgrounds_dir(
        config.data_dir, "ERL", "drivebc244"
    )
    # The main camera keeps the bare directory it has always used, so no library moves.
    assert backgrounds_dir(config.data_dir, "ERL").name == "ERL"

    _frame(conn, "ERL", MAIN_CAMERA, 0, config)
    _frame(conn, "ERL", "drivebc244", 15, config)
    # Neither bucket has enough samples to build; what matters is that they are counted
    # separately rather than pooled into one bucket of two.
    main = build_backgrounds(conn, config, "ERL", at=_when())
    extra = build_backgrounds(conn, config, "ERL", camera="drivebc244", at=_when())
    assert sum(main.per_hour.values()) == 1
    assert sum(extra.per_hour.values()) == 1


def _when():
    from datetime import datetime

    from ferrycast.timeutil import UTC

    return datetime(2026, 8, 14, 21, 0, tzinfo=UTC)


def test_an_extra_cameras_reference_is_built_by_the_job_that_actually_runs(config, tmp_path):
    """The scheduler builds backgrounds in production, not the CLI. Written separately, the
    two disagreed about which cameras exist — and the one that ran was the one that had
    never heard of the second camera, so its reader would have had nothing to difference
    against however well the frames were collected."""
    from dataclasses import replace

    from ferrycast.config import MAIN_CAMERA, Camera
    from ferrycast.lanes import cameras_with_calibration

    config_dir = tmp_path / "calibration"
    config_dir.mkdir()
    (config_dir / "ERL.json").write_text(
        json.dumps({"terminal": "ERL", "kind": "lanes", "image_size": [320, 240],
                    "lane_spans": {}, "vanishing_point": [0, 0], "y_ref": 1.0,
                    "mobius": [1, 0, 0]}),
        encoding="utf-8",
    )
    (config_dir / "ERL_drivebc244.json").write_text(
        (REPO / "config" / "calibration" / "ERL_drivebc244.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    terminal = replace(
        config.route.terminal("ERL"),
        cameras=(Camera(id="drivebc244", kind="queue_extent",
                        image_url="https://example.invalid/244.jpg"),),
    )

    found = cameras_with_calibration(tmp_path, terminal)

    assert found == [MAIN_CAMERA, "drivebc244"]


def test_a_camera_without_a_calibration_gets_no_reference(config, tmp_path):
    """The reference would be built, stored, and never differenced against anything."""
    from dataclasses import replace

    from ferrycast.config import Camera
    from ferrycast.lanes import cameras_with_calibration

    (tmp_path / "calibration").mkdir()
    terminal = replace(
        config.route.terminal("ERL"),
        cameras=(Camera(id="nocal", kind="queue_extent",
                        image_url="https://example.invalid/x.jpg"),),
    )
    assert cameras_with_calibration(tmp_path, terminal) == []


def test_frames_from_different_cameras_do_not_share_a_path(config):
    from datetime import datetime

    from ferrycast.capture import frame_path
    from ferrycast.timeutil import UTC

    at = datetime(2026, 8, 14, 20, 0, 1, tzinfo=UTC)
    main = frame_path(config, "ERL", at, "jpg")
    extra = frame_path(config, "ERL", at, "jpg", "drivebc244")
    assert main != extra
    # The main camera's layout is unchanged, so an existing frame store needs no migration.
    assert main.parent.parts[-4] == "ERL"
    assert extra.parent.parts[-4] == "ERL_drivebc244"


# --- configuration -----------------------------------------------------------------------


# Cameras hang off the terminal above them, so this is spliced into ERL's entry rather
# than appended — appended, it would read as a third terminal on a two-terminal route.
ERL_ANCHOR = '  deck_space_url = "https://example.invalid/erl"\n'

CAMERA_BLOCK = """
  [[route.terminals.cameras]]
  id = "drivebc244"
  kind = "queue_extent"
  image_url = "https://example.invalid/244.jpg"
  replay_index_url = "https://example.invalid/244/replay/"
  replay_frame_url = "https://example.invalid/244/{timestamp}.jpg"
"""


def _config_with_camera(tmp_path, block: str):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    assert CONFIG_TEMPLATE.count(ERL_ANCHOR) == 1
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(ERL_ANCHOR, ERL_ANCHOR + block)
    )
    return load_config(config_dir / "ferrycast.toml")


def test_a_camera_with_a_replay_feed_is_declared_as_having_one(tmp_path):
    config = _config_with_camera(tmp_path, CAMERA_BLOCK)
    erl = config.route.terminal("ERL")
    camera = erl.camera("drivebc244")
    assert camera.has_replay
    assert erl.replay_cameras == (camera,)
    assert config.route.terminal("SLT").replay_cameras == ()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ('id = "drivebc244"', "declares camera"),          # the same id twice
        ('id = "main"', "reserved"),
        ('id = "../escape"', "names a directory"),
        ('kind = "guesswork"', "must be one of"),
    ],
)
def test_a_camera_that_could_not_work_is_refused_at_load(tmp_path, mutation, message):
    key = mutation.split(" =")[0]
    block = CAMERA_BLOCK
    if key == "id" and "drivebc244" in mutation:
        block = CAMERA_BLOCK + """
  [[route.terminals.cameras]]
  id = "drivebc244"
  image_url = "https://example.invalid/again.jpg"
"""
    else:
        block = block.replace(f'  {key} = ', "  # ", 1) + f"  {mutation}\n"
    with pytest.raises(ConfigError, match=message):
        _config_with_camera(tmp_path, block)


def test_half_a_replay_feed_is_refused(tmp_path):
    """An index of timestamps is no use without the address to fetch one from."""
    block = CAMERA_BLOCK.replace(
        '  replay_frame_url = "https://example.invalid/244/{timestamp}.jpg"\n', ""
    )
    with pytest.raises(ConfigError, match="or neither"):
        _config_with_camera(tmp_path, block)


def test_a_replay_url_with_no_timestamp_in_it_is_refused(tmp_path):
    """Otherwise every archived frame is fetched from the same address, and the archive fills
    with ninety-six copies of one moment."""
    block = CAMERA_BLOCK.replace("{timestamp}", "latest")
    with pytest.raises(ConfigError, match="timestamp"):
        _config_with_camera(tmp_path, block)


def test_a_camera_with_nothing_to_fetch_is_refused(tmp_path):
    block = CAMERA_BLOCK
    for line in (
        '  image_url = "https://example.invalid/244.jpg"\n',
        '  replay_index_url = "https://example.invalid/244/replay/"\n',
        '  replay_frame_url = "https://example.invalid/244/{timestamp}.jpg"\n',
    ):
        block = block.replace(line, "")
    with pytest.raises(ConfigError, match="nothing could ever be captured"):
        _config_with_camera(tmp_path, block)


# --- extraction --------------------------------------------------------------------------

EXTENT_CAL = REPO / "config" / "calibration" / "ERL_drivebc244.json"
EXTENT_BG = REPO / "config" / "calibration" / "ERL_drivebc244_background.jpg"

needs_extent_calibration = pytest.mark.skipif(
    not (EXTENT_CAL.exists() and EXTENT_BG.exists()),
    reason="Earls Cove highway calibration not present",
)

#: The UTC hour the frames below are captured in, as a local wall-clock hour — what the
#: background library keys references by. 20:00Z in America/Vancouver is 13:00.
LOCAL_HOUR = 13


def _wired_config(tmp_path):
    """A config that declares the highway camera, with its calibration installed and one
    hour's background reference built — everything extraction needs short of frames."""
    import shutil

    from PIL import Image

    from ferrycast.lanes import MIN_BACKGROUND_SAMPLES, backgrounds_dir

    config = _config_with_camera(tmp_path, CAMERA_BLOCK)
    cal_dir = Path(config.source_path).parent / "calibration"
    cal_dir.mkdir(exist_ok=True)
    shutil.copyfile(EXTENT_CAL, cal_dir / "ERL_drivebc244.json")

    root = backgrounds_dir(config.data_dir, "ERL", "drivebc244")
    root.mkdir(parents=True, exist_ok=True)
    Image.open(EXTENT_BG).convert("L").save(root / f"{LOCAL_HOUR:02d}.png")
    (root / f"{LOCAL_HOUR:02d}.json").write_text(
        json.dumps({"terminal": "ERL", "hour": LOCAL_HOUR,
                    "samples": MIN_BACKGROUND_SAMPLES,
                    "from": "x", "to": "y", "built_at": "z"}),
        encoding="utf-8",
    )
    return config


def _hw_frame(conn, config, minute, content):
    rel = f"hw/{minute:02d}.jpg"
    path = config.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    conn.execute(
        """INSERT INTO frames (route, terminal, camera, captured_at, service_date, path,
                               status)
           VALUES ('route7', 'ERL', 'drivebc244', ?, '2026-08-14', ?, 'ok')""",
        (f"2026-08-14T20:{minute:02d}:01Z", rel),
    )
    conn.commit()


def _queue_bytes():
    """A frame with a real standing queue painted onto the reference road."""
    from ferrycast.extent import QueueExtentCalibration

    from .test_extent import _road

    cal = QueueExtentCalibration.load(EXTENT_CAL)
    tail = next(
        row for row in range(cal.near_row, cal.far_row - 1, -1)
        if cal.vehicles_at(row) >= 6.0
    )
    return _road(cal, tail=tail)


@needs_extent_calibration
def test_the_shared_sweep_reads_the_highway_camera(tmp_path):
    """`extract_pending` is the one place that decides which cameras get read; a camera it
    has not heard of is collected, background-built, and never once extracted. Also pins
    what gets stored: a clear road is a trustworthy reading that claims *nothing* — no
    band, and above all no vehicle count of zero for aggregation to mistake for an emptied
    compound — and even a real queue keeps its highway count out of `vehicle_count`,
    because that column means vehicles in the compound."""
    from ferrycast.extent import PROMPT_VERSION
    from ferrycast.lanes import extract_pending

    config = _wired_config(tmp_path)
    conn = init_db(config.db_path)
    _hw_frame(conn, config, 0, EXTENT_BG.read_bytes())   # clear road
    _hw_frame(conn, config, 15, _queue_bytes())          # standing queue

    stats = extract_pending(conn, config, max_reads=10)

    rows = conn.execute(
        """SELECT o.fullness, o.vehicle_count, o.usable FROM observations o
             JOIN frames f ON f.id = o.frame_id
            WHERE o.prompt_version = ? ORDER BY f.captured_at""",
        (PROMPT_VERSION,),
    ).fetchall()
    assert stats.read == 2
    clear, queue = rows
    assert clear["usable"] == 1
    assert clear["fullness"] is None
    assert clear["vehicle_count"] is None
    assert queue["usable"] == 1
    assert queue["fullness"] in ("heavy", "overflowing")
    assert queue["vehicle_count"] is None


@needs_extent_calibration
def test_a_dark_highway_frame_is_refused_not_read(tmp_path):
    """Differencing across a big illumination change is not validated, and the failure mode
    is a confident phantom queue. The refusal is stored, unusable, so the frame does not
    stay pending forever."""
    import io

    from PIL import Image

    from ferrycast.extent import PROMPT_VERSION
    from ferrycast.lanes import extract_pending

    config = _wired_config(tmp_path)
    conn = init_db(config.db_path)
    buf = io.BytesIO()
    Image.new("RGB", Image.open(EXTENT_BG).size, (5, 5, 5)).save(buf, format="JPEG")
    _hw_frame(conn, config, 0, buf.getvalue())

    stats = extract_pending(conn, config, max_reads=10)

    row = conn.execute(
        "SELECT fullness, usable FROM observations WHERE prompt_version = ?",
        (PROMPT_VERSION,),
    ).fetchone()
    assert stats.read == 1 and stats.unusable == 1
    assert row["usable"] == 0
    assert row["fullness"] is None


@needs_extent_calibration
def test_the_read_budget_carries_over_between_sweeps(tmp_path):
    """The cap bounds one run's work, not which frames ever get read: what this sweep
    cannot afford, the next one picks up."""
    from ferrycast.extent import PROMPT_VERSION
    from ferrycast.lanes import extract_pending

    config = _wired_config(tmp_path)
    conn = init_db(config.db_path)
    _hw_frame(conn, config, 0, EXTENT_BG.read_bytes())
    _hw_frame(conn, config, 15, EXTENT_BG.read_bytes())

    first = extract_pending(conn, config, max_reads=1)
    second = extract_pending(conn, config, max_reads=1)

    assert (first.read, second.read) == (1, 1)
    n = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE prompt_version = ?", (PROMPT_VERSION,)
    ).fetchone()[0]
    assert n == 2


# --- retention ---------------------------------------------------------------------------


def test_thinning_does_not_delete_the_replay_archive(conn, config):
    """`_thin_unextracted` keeps the frames the essential-offsets selection picks, and that
    selection has never heard of a second camera — so applied to the highway one it kept
    nothing and would have deleted every replay frame ever collected, unread."""
    from datetime import timedelta

    from ferrycast.maintenance import prune_frames
    from ferrycast.timeutil import iso, now_utc

    old = now_utc() - timedelta(days=60)   # past thinning (45d), inside retention (400d)
    rel = "hw/old.jpg"
    path = config.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 100)
    conn.execute(
        """INSERT INTO frames (route, terminal, camera, captured_at, service_date, path,
                               status)
           VALUES ('route7', 'ERL', 'drivebc244', ?, ?, ?, 'ok')""",
        (iso(old), old.date().isoformat(), rel),
    )
    conn.commit()

    result = prune_frames(conn, config)

    assert result.thinned_unextracted == 0
    assert path.exists()


def test_an_extent_read_highway_frame_is_reclaimed_at_the_horizon(conn, config):
    """The other half of the retention rule. "Read" means read by the extractor that will
    ever look at a frame — vision never sees the highway camera, so testing for a vision
    observation made every replay frame permanently unprunable, extracted or not."""
    from datetime import timedelta

    from ferrycast.extent import PROMPT_VERSION
    from ferrycast.maintenance import prune_frames
    from ferrycast.timeutil import iso, now_utc

    ancient = now_utc() - timedelta(days=500)   # past the 400-day hard horizon
    paths = {}
    for name, extracted in (("read", True), ("unread", False)):
        rel = f"hw/{name}.jpg"
        path = config.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        paths[name] = path
        cur = conn.execute(
            """INSERT INTO frames (route, terminal, camera, captured_at, service_date,
                                   path, status)
               VALUES ('route7', 'ERL', 'drivebc244', ?, ?, ?, 'ok')""",
            (iso(ancient + timedelta(minutes=1 if extracted else 2)),
             ancient.date().isoformat(), rel),
        )
        if extracted:
            conn.execute(
                """INSERT INTO observations (frame_id, prompt_version, model, usable,
                                             created_at)
                   VALUES (?, ?, 'queue-extent', 1, ?)""",
                (cur.lastrowid, PROMPT_VERSION, iso(ancient)),
            )
    conn.commit()

    result = prune_frames(conn, config)

    assert not paths["read"].exists()      # numbers banked, file reclaimed
    assert paths["unread"].exists()        # the only record of its moment
    assert result.deleted == 1
