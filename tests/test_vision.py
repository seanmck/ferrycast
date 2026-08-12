import io
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from ferrycast.timeutil import iso, now_utc
from ferrycast.vision import (
    FULLNESS_LEVELS,
    OBSERVATION_SCHEMA,
    OBSERVATION_SCHEMA_V2,
    estimate_cost,
    extract_pending,
    month_to_date_cost,
    pending_frames,
    prepare_image,
)


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    stop_reason = "end_turn"

    def __init__(self, payload, input_tokens=900, output_tokens=60):
        self.content = [FakeBlock(json.dumps(payload))]
        self.usage = FakeUsage(input_tokens, output_tokens)


class FakeMessages:
    def __init__(self, payload, input_tokens=900, output_tokens=60):
        self.payload = payload
        self.calls = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.payload, self.input_tokens, self.output_tokens)


class FakeClient:
    def __init__(self, payload, **kw):
        self.messages = FakeMessages(payload, **kw)


GOOD_PAYLOAD = {
    "vehicle_count": 42,
    "lanes_occupied": 3,
    "queue_extends_beyond_frame": False,
    "ferry_at_dock": True,
    "visibility": "clear",
    "confidence": 0.86,
    "notes": "",
}


def _bright_png(width=1600, height=900, colour=(200, 200, 200)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _add_frame(conn, config, content: bytes, *, terminal="SLT", minutes_ago=0):
    moment = now_utc() - timedelta(minutes=minutes_ago)
    path = config.frames_dir / terminal / f"{terminal}-{minutes_ago}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    cur = conn.execute(
        """INSERT INTO frames (route, terminal, captured_at, service_date, path, status)
           VALUES (?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(moment),
            moment.date().isoformat(),
            str(path.relative_to(config.data_dir)),
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_schema_is_strict_so_the_model_cannot_invent_fields():
    assert OBSERVATION_SCHEMA["additionalProperties"] is False
    assert set(OBSERVATION_SCHEMA["required"]) == set(OBSERVATION_SCHEMA["properties"])


def test_images_are_downscaled_before_upload():
    original = _bright_png(1600, 900)
    prepared, media_type, luma = prepare_image(original, max_width=896)

    from PIL import Image

    with Image.open(io.BytesIO(prepared)) as img:
        assert img.width == 896
        assert img.height == 504  # aspect ratio preserved
    assert media_type == "image/jpeg"
    assert len(prepared) < len(original)
    assert luma > 100


def test_dark_frames_are_measured_as_dark():
    _, _, luma = prepare_image(_bright_png(64, 64, (3, 3, 3)), max_width=896)
    assert luma < 24


def test_extraction_writes_an_observation(conn, config):
    frame_id = _add_frame(conn, config, _bright_png())
    client = FakeClient(GOOD_PAYLOAD)

    stats = extract_pending(conn, config, client=client)

    assert stats.extracted == 1
    row = conn.execute("SELECT * FROM observations WHERE frame_id = ?", (frame_id,)).fetchone()
    assert row["vehicle_count"] == 42
    assert row["ferry_at_dock"] == 1
    assert row["usable"] == 1
    assert row["confidence"] == pytest.approx(0.86)
    assert row["cost_usd"] > 0


def test_request_uses_the_configured_model_and_a_json_schema(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient(GOOD_PAYLOAD)

    extract_pending(conn, config, client=client)

    call = client.messages.calls[0]
    assert call["model"] == config.vision.model
    assert call["output_config"]["format"]["type"] == "json_schema"
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    # The terminal's human name is given so the model knows which compound it is reading.
    assert "Saltery Bay" in content[1]["text"]


def test_low_confidence_frames_are_flagged_not_trusted(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient({**GOOD_PAYLOAD, "confidence": 0.1})

    stats = extract_pending(conn, config, client=client)

    assert stats.flagged_unusable == 1
    assert conn.execute("SELECT usable FROM observations").fetchone()["usable"] == 0


def test_obscured_frames_are_flagged(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient({**GOOD_PAYLOAD, "visibility": "obscured"})

    extract_pending(conn, config, client=client)

    assert conn.execute("SELECT usable FROM observations").fetchone()["usable"] == 0


def test_dark_frames_are_flagged_without_spending_a_call(conn, config):
    _add_frame(conn, config, _bright_png(400, 300, (2, 2, 2)))
    client = FakeClient(GOOD_PAYLOAD)

    stats = extract_pending(conn, config, client=client)

    assert stats.skipped_dark == 1
    assert stats.extracted == 0
    assert client.messages.calls == []
    row = conn.execute("SELECT visibility, usable, cost_usd FROM observations").fetchone()
    assert row["visibility"] == "dark"
    assert row["usable"] == 0
    assert row["cost_usd"] == 0


def test_extraction_is_idempotent(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient(GOOD_PAYLOAD)

    extract_pending(conn, config, client=client)
    second = extract_pending(conn, config, client=client)

    assert second.considered == 0
    assert len(client.messages.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1


def test_bumping_the_prompt_version_re_extracts_the_backlog(conn, config):
    from dataclasses import replace

    _add_frame(conn, config, _bright_png())
    client = FakeClient(GOOD_PAYLOAD)
    extract_pending(conn, config, client=client)

    v2 = replace(config, vision=replace(config.vision, prompt_version="v2"))
    stats = extract_pending(conn, v2, client=client)

    assert stats.extracted == 1
    versions = {
        row["prompt_version"]
        for row in conn.execute("SELECT prompt_version FROM observations").fetchall()
    }
    assert versions == {"v1", "v2"}


def test_a_failing_frame_does_not_abort_the_run(conn, config):
    _add_frame(conn, config, _bright_png(), minutes_ago=30)
    _add_frame(conn, config, _bright_png(), minutes_ago=15)

    calls = {"n": 0}

    class HalfBrokenClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("overloaded")
                return FakeResponse(GOOD_PAYLOAD)

    stats = extract_pending(conn, config, client=HalfBrokenClient())

    assert stats.failed == 1
    assert stats.extracted == 1
    assert stats.errors


def test_monthly_budget_stops_the_run(conn, config):
    from dataclasses import replace

    for i in range(5):
        _add_frame(conn, config, _bright_png(), minutes_ago=i)
    # A budget so small that a single call exhausts it.
    tiny = replace(config, vision=replace(config.vision, monthly_budget_usd=0.0015))
    client = FakeClient(GOOD_PAYLOAD, input_tokens=1_000_000, output_tokens=0)

    stats = extract_pending(conn, tiny, client=client)

    assert stats.budget_stopped
    assert stats.extracted < 5
    assert any("budget" in e for e in stats.errors)


def test_cost_estimate_uses_configured_rates(config):
    # Pinned here rather than taken from the default config, so switching models does not
    # look like a broken cost calculation.
    priced = replace(
        config,
        vision=replace(config.vision, input_usd_per_mtok=1.0, output_usd_per_mtok=5.0),
    )
    assert estimate_cost(priced, 1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost(priced, 0, 1_000_000) == pytest.approx(5.0)


def test_month_to_date_cost_accumulates(conn, config):
    _add_frame(conn, config, _bright_png())
    extract_pending(conn, config, client=FakeClient(GOOD_PAYLOAD))
    assert month_to_date_cost(conn) > 0


def test_pending_frames_can_be_scoped_to_a_terminal(conn, config):
    _add_frame(conn, config, _bright_png(), terminal="SLT", minutes_ago=1)
    _add_frame(conn, config, _bright_png(), terminal="ERL", minutes_ago=2)

    scoped = pending_frames(conn, "v1", 10, terminal="ERL")
    assert [row["terminal"] for row in scoped] == ["ERL"]


def test_dry_run_sends_nothing(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient(GOOD_PAYLOAD)

    stats = extract_pending(conn, config, client=client, dry_run=True)

    assert stats.considered == 1
    assert stats.extracted == 0
    assert client.messages.calls == []


# --- v2: the band replaces the count -------------------------------------------------

V2_PAYLOAD = {
    "compound_visible": True,
    "fullness": "heavy",
    "vehicle_count": None,
    "confidence": 0.8,
    "notes": "",
}


def _v2(config):
    return replace(config, vision=replace(config.vision, prompt_version="v2"))


def test_v2_sends_its_own_prompt_and_schema(conn, config):
    _add_frame(conn, config, _bright_png())
    client = FakeClient(V2_PAYLOAD)

    extract_pending(conn, _v2(config), client=client)

    sent = client.messages.calls[0]
    assert "how full" in sent["system"].lower()
    schema = sent["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == set(OBSERVATION_SCHEMA_V2["properties"])
    # The band is what we asked for, so it must be a required part of the answer.
    assert "fullness" in schema["required"]


def test_v2_stores_the_band_and_stays_usable_without_a_count(conn, config):
    frame_id = _add_frame(conn, config, _bright_png())

    extract_pending(conn, _v2(config), client=FakeClient(V2_PAYLOAD))

    row = conn.execute("SELECT * FROM observations WHERE frame_id = ?", (frame_id,)).fetchone()
    assert row["fullness"] == "heavy"
    assert row["vehicle_count"] is None
    # v1 required a count before it would trust a frame. Under v2 the count is optional,
    # so insisting on one here would throw away every good reading.
    assert row["usable"] == 1
    assert row["prompt_version"] == "v2"


def test_v2_discards_a_frame_whose_compound_cannot_be_seen(conn, config):
    frame_id = _add_frame(conn, config, _bright_png())
    payload = {**V2_PAYLOAD, "compound_visible": False, "fullness": None, "confidence": 0.1}

    stats = extract_pending(conn, _v2(config), client=FakeClient(payload))

    row = conn.execute("SELECT * FROM observations WHERE frame_id = ?", (frame_id,)).fetchone()
    assert row["usable"] == 0
    assert row["visibility"] == "obscured"
    assert stats.flagged_unusable == 1


def test_v2_keeps_a_dim_but_legible_frame(conn, config):
    """These cameras are floodlit all night, so "dim" is not a reason to drop a frame.

    v1 gated on a `visibility` of clear/dim/obscured/dark and would discard readings that
    were perfectly legible. v2 asks only whether the compound can be judged.
    """
    frame_id = _add_frame(conn, config, _bright_png())
    payload = {**V2_PAYLOAD, "fullness": "empty", "confidence": 0.7}

    extract_pending(conn, _v2(config), client=FakeClient(payload))

    row = conn.execute("SELECT * FROM observations WHERE frame_id = ?", (frame_id,)).fetchone()
    assert row["fullness"] == "empty"
    assert row["usable"] == 1


def test_v2_still_drops_a_low_confidence_reading(conn, config):
    frame_id = _add_frame(conn, config, _bright_png())
    payload = {**V2_PAYLOAD, "confidence": 0.05}

    extract_pending(conn, _v2(config), client=FakeClient(payload))

    row = conn.execute("SELECT * FROM observations WHERE frame_id = ?", (frame_id,)).fetchone()
    assert row["usable"] == 0


def test_v1_observations_survive_the_new_version(conn, config):
    """Bumping the prompt must add a generation, not rewrite the archive."""
    frame_id = _add_frame(conn, config, _bright_png())
    extract_pending(conn, config, client=FakeClient(GOOD_PAYLOAD))
    extract_pending(conn, _v2(config), client=FakeClient(V2_PAYLOAD))

    rows = conn.execute(
        "SELECT prompt_version, vehicle_count, fullness FROM observations "
        "WHERE frame_id = ? ORDER BY prompt_version",
        (frame_id,),
    ).fetchall()
    assert [r["prompt_version"] for r in rows] == ["v1", "v2"]
    assert rows[0]["vehicle_count"] == 42 and rows[0]["fullness"] is None
    assert rows[1]["vehicle_count"] is None and rows[1]["fullness"] == "heavy"


def test_an_unknown_prompt_version_is_refused(conn, config):
    _add_frame(conn, config, _bright_png())
    bogus = replace(config, vision=replace(config.vision, prompt_version="v99"))

    stats = extract_pending(conn, bogus, client=FakeClient(V2_PAYLOAD))

    assert stats.extracted == 0
    assert any("v99" in e for e in stats.errors)


def test_fullness_levels_are_ordered_weakest_first():
    assert FULLNESS_LEVELS[0] == "empty"
    assert FULLNESS_LEVELS[-1] == "overflowing"
    assert set(OBSERVATION_SCHEMA_V2["properties"]["fullness"]["enum"]) == {
        *FULLNESS_LEVELS,
        None,
    }
