
from ferrycast.capture import capture_once, sniff_extension, store_frame
from ferrycast.fetching import FetchResult, fetch
from ferrycast.timeutil import now_utc


def test_sniffs_common_image_formats(png_bytes):
    assert sniff_extension(png_bytes) == "png"
    assert sniff_extension(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert sniff_extension(b"<html>") is None


def test_stores_a_captured_frame_on_disk_and_in_the_database(conn, config, png_bytes):
    moment = now_utc()
    outcome = store_frame(
        conn, config, "SLT", moment, FetchResult(ok=True, content=png_bytes)
    )

    assert outcome.ok
    assert outcome.path.exists()
    assert outcome.path.read_bytes() == png_bytes

    row = conn.execute("SELECT * FROM frames").fetchone()
    assert row["status"] == "ok"
    assert row["terminal"] == "SLT"
    assert row["bytes"] == len(png_bytes)
    assert row["width"] == 64 and row["height"] == 48
    assert row["sha256"]


def test_a_dead_camera_records_a_gap_instead_of_crashing(conn, config):
    outcome = store_frame(
        conn, config, "SLT", now_utc(), FetchResult(ok=False, error="ConnectTimeout")
    )

    assert not outcome.ok
    row = conn.execute("SELECT status, error FROM frames").fetchone()
    assert row["status"] == "error"
    assert "ConnectTimeout" in row["error"]


def test_an_html_error_page_is_not_archived_as_a_frame(conn, config):
    """A webcam URL that starts returning a login page must be loud, not silently stored."""
    outcome = store_frame(
        conn,
        config,
        "SLT",
        now_utc(),
        FetchResult(ok=True, content=b"<html>404</html>", content_type="text/html"),
    )

    assert not outcome.ok
    assert "not an image" in outcome.error
    assert conn.execute("SELECT status FROM frames").fetchone()["status"] == "error"


def test_capture_is_idempotent_for_the_same_timestamp(conn, config, png_bytes):
    moment = now_utc()
    store_frame(conn, config, "SLT", moment, FetchResult(ok=True, content=png_bytes))
    store_frame(conn, config, "SLT", moment, FetchResult(ok=True, content=png_bytes))
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1


def test_one_dead_camera_does_not_stop_the_other(conn, config, monkeypatch):
    from ferrycast import capture

    def fake_fetch(url, **kwargs):
        if "slt" in url:
            return FetchResult(ok=False, error="HTTP 500")
        return FetchResult(ok=True, content=png)

    png = _png()
    monkeypatch.setattr(capture, "fetch", fake_fetch)

    outcomes = capture_once(conn, config)

    assert {o.terminal: o.ok for o in outcomes} == {"SLT": False, "ERL": True}
    statuses = dict(
        conn.execute("SELECT terminal, status FROM frames").fetchall()  # type: ignore[arg-type]
    )
    assert statuses == {"SLT": "error", "ERL": "ok"}

    # The run is still recorded as usable because at least one camera delivered.
    run = conn.execute("SELECT * FROM job_runs WHERE job = 'capture'").fetchone()
    assert run["ok"] == 1
    assert run["attempted"] == 2 and run["succeeded"] == 1


def _png():
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_fetch_retries_transient_failures_then_gives_up():
    calls = []

    class FlakyClient:
        def get(self, url, headers=None):
            calls.append(url)
            raise __import__("httpx").ConnectError("boom")

    result = fetch(
        "https://example.invalid/x.jpg",
        user_agent="test",
        max_retries=2,
        client=FlakyClient(),
        sleep=lambda _: None,
    )
    assert not result.ok
    assert len(calls) == 3  # initial attempt plus two retries


def test_fetch_does_not_retry_a_404():
    calls = []

    class NotFoundClient:
        def get(self, url, headers=None):
            calls.append(url)
            import httpx

            return httpx.Response(404, request=httpx.Request("GET", url))

    result = fetch(
        "https://example.invalid/x.jpg",
        user_agent="test",
        max_retries=3,
        client=NotFoundClient(),
        sleep=lambda _: None,
    )
    assert not result.ok
    assert result.status_code == 404
    assert len(calls) == 1
