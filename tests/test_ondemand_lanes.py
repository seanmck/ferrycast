"""The on-demand vision check must not be offered where lane geometry already answers.

A calibrated terminal gets a free per-lane reading on every capture — see `lanes.py`.
Paying for a vision call there buys nothing a moment's wait wouldn't give for free, so
`/api/check` refuses for any terminal with a lane calibration on disk, and only for those.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ferrycast.web.app import create_app

MINIMAL_LANE_CAL = {
    "terminal": "SLT",
    "kind": "lanes",
    "image_size": [320, 240],
    "vanishing_point": [160.0, 0.0],
    "y_ref": 190.0,
    "mobius": [1.0, 0.0, 0.0],
    "lane_spans": {"9": [[100, 0, 10]]},
}


def _write_calibration(config, terminal="SLT"):
    cal_dir = config.source_path.parent / "calibration"
    cal_dir.mkdir(exist_ok=True)
    (cal_dir / f"{terminal}.json").write_text(
        json.dumps({**MINIMAL_LANE_CAL, "terminal": terminal})
    )


@pytest.fixture
def enabled_client(conn, config):
    app = create_app(str(config.source_path))
    app.state.config = replace(config, web=replace(config.web, allow_on_demand_checks=True))
    with TestClient(app) as client:
        yield client


def test_on_demand_check_refused_for_a_calibrated_terminal(enabled_client, config):
    _write_calibration(config, "SLT")

    response = enabled_client.post("/api/check?origin=SLT")

    assert response.status_code == 403
    assert "calibrated lane camera" in response.json()["detail"]


def test_on_demand_check_still_allowed_for_an_uncalibrated_terminal(enabled_client, config):
    _write_calibration(config, "SLT")

    response = enabled_client.post("/api/check?origin=ERL")

    # No lane calibration for ERL, so it still reaches the feature-flag/cap checks rather
    # than being refused for being calibrated — it may fail later for unrelated reasons
    # (no webcam reachable in tests), but not with the "calibrated lane camera" message.
    assert response.status_code != 403 or "calibrated lane camera" not in response.text
