from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest

from ferrycast.config import load_config
from ferrycast.db import init_db
from ferrycast.timeutil import iso

CONFIG_TEMPLATE = """
[app]
timezone = "America/Vancouver"
data_dir = "data"
schedule_path = "schedule.toml"

[route]
id = "route7"
name = "Saltery Bay - Earls Cove"

  [[route.terminals]]
  code = "SLT"
  name = "Saltery Bay"
  destination = "ERL"
  webcam_url = "https://example.invalid/slt.jpg"
  deck_space_url = "https://example.invalid/slt"

  [[route.terminals]]
  code = "ERL"
  name = "Earls Cove"
  destination = "SLT"
  webcam_url = "https://example.invalid/erl.jpg"
  deck_space_url = "https://example.invalid/erl"

[capture]
interval_minutes = 15

[vision]
prompt_version = "v1"
monthly_budget_usd = 5.0

[aggregate]
vessel_capacity = 125
residual_threshold = 5

[query]
min_sample = 3
"""

SCHEDULE_TEMPLATE = """
[[block]]
terminal = "SLT"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["08:30", "12:30", "16:30"]

[[block]]
terminal = "ERL"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["09:30", "13:30", "15:25"]
"""


@pytest.fixture
def config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(CONFIG_TEMPLATE)
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    return load_config(config_dir / "ferrycast.toml")


@pytest.fixture
def conn(config):
    connection = init_db(config.db_path)
    yield connection
    connection.close()


@pytest.fixture
def png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 130, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def add_observation(
    conn,
    config,
    terminal: str,
    captured_at: datetime,
    vehicle_count: int | None,
    *,
    ferry_at_dock: bool = False,
    confidence: float = 0.9,
    usable: bool = True,
    beyond_frame: bool = False,
):
    """Insert a frame plus its extracted observation, as capture+extract would have."""
    from ferrycast.timeutil import local

    cur = conn.execute(
        """INSERT INTO frames (route, terminal, captured_at, service_date, path, status)
           VALUES (?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(captured_at),
            local(captured_at, config.tz).date().isoformat(),
            f"{terminal}/{iso(captured_at)}.jpg",
        ),
    )
    frame_id = cur.lastrowid
    conn.execute(
        """INSERT INTO observations
               (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                queue_beyond_frame, ferry_at_dock, visibility, confidence, usable, created_at)
           VALUES (?, ?, 'test-model', ?, 2, ?, ?, 'clear', ?, ?, ?)""",
        (
            frame_id,
            config.vision.prompt_version,
            vehicle_count,
            int(beyond_frame),
            int(ferry_at_dock),
            confidence,
            int(usable),
            iso(captured_at),
        ),
    )
    conn.commit()
    return frame_id


def build_sailing_frames(
    conn,
    config,
    departure_local: datetime,
    *,
    before: list[int],
    after: list[int],
    dock_before: bool = True,
    dock_after: bool = False,
    terminal: str = "SLT",
):
    """Lay down a queue trace around a departure: counts before, then counts after."""
    for index, count in enumerate(reversed(before)):
        moment = departure_local - timedelta(minutes=15 * (index + 1))
        add_observation(
            conn,
            config,
            terminal,
            moment,
            count,
            ferry_at_dock=dock_before and index == 0,
        )
    for index, count in enumerate(after):
        moment = departure_local + timedelta(minutes=15 * (index + 1))
        add_observation(conn, config, terminal, moment, count, ferry_at_dock=dock_after)
