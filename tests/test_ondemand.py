"""On-demand extraction: pay for frames when someone asks, not on a timer.

Capture stays continuous because a frame not captured is gone forever and costs nothing but
disk. Extraction is the paid step, so these tests pin down that it happens only where asked,
and that deferring it can't quietly destroy the history it was deferred from.
"""

from datetime import date, timedelta

from ferrycast.maintenance import prune_frames
from ferrycast.selection import essential_frames_for_day, frames_for_sailing, recent_frames
from ferrycast.status import check_and_compare, check_now
from ferrycast.timeutil import combine_local, iso, now_utc, parse_hhmm
from ferrycast.vision import extract_pending

from .test_query import fridays, seed_record
from .test_vision import GOOD_PAYLOAD, FakeClient, _add_frame, _bright_png


def _frames_around(conn, config, departure, offsets, terminal="SLT"):
    """Lay down captured-but-unextracted frames at given minute offsets from a departure."""
    for offset in offsets:
        _add_frame_at(conn, config, terminal, departure + timedelta(minutes=offset))


def _add_frame_at(conn, config, terminal, moment, content=None):
    path = config.frames_dir / terminal / f"{iso(moment).replace(':', '-')}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content or _bright_png(200, 150))
    cur = conn.execute(
        """INSERT OR IGNORE INTO frames
               (route, terminal, captured_at, service_date, path, status)
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


# --------------------------------------------------------------------- sparse selection


def test_essential_selection_reads_a_handful_not_the_whole_window(conn, config):
    day = date(2026, 8, 14)
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)
    # A full window of 15-minute frames, as continuous capture would produce.
    _frames_around(conn, config, departure, range(-120, 46, 15))

    everything = frames_for_sailing(conn, config, origin="SLT", departure=departure)
    essential = essential_frames_for_day(conn, config, day, origin="SLT")

    assert len(everything) == 12
    # Four offsets are configured, so at most four frames per sailing get read.
    assert len(essential) <= len(config.vision.essential_offsets_minutes)
    assert len(essential) < len(everything)


def test_essential_selection_picks_frames_around_departure(conn, config):
    day = date(2026, 8, 14)
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)
    _frames_around(conn, config, departure, range(-120, 46, 15))

    chosen = essential_frames_for_day(conn, config, day, origin="SLT")
    offsets = sorted(
        round((parse_iso_local(row["captured_at"]) - departure).total_seconds() / 60)
        for row in chosen
    )

    # Something shortly before departure (the peak and the at-departure reading) and
    # something after it settles (the residual, which is what proves an overload).
    assert any(-35 <= o <= 0 for o in offsets), offsets
    assert any(o >= 12 for o in offsets), offsets


def parse_iso_local(value):
    from ferrycast.timeutil import parse_iso

    return parse_iso(value)


def test_essential_selection_skips_already_extracted_frames(conn, config):
    day = date(2026, 8, 14)
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)
    _frames_around(conn, config, departure, [-30, -10, 15, 30])

    first = essential_frames_for_day(conn, config, day, origin="SLT")
    assert first
    from ferrycast.vision import extract_frames

    extract_frames(conn, config, first, client=FakeClient(GOOD_PAYLOAD))

    # Nothing left to pay for on a second pass.
    assert essential_frames_for_day(conn, config, day, origin="SLT") == []


def test_a_sailing_with_no_frames_selects_nothing(conn, config):
    assert essential_frames_for_day(conn, config, date(2026, 8, 14)) == []


# ------------------------------------------------------------------------ on-demand check


def test_check_reads_only_the_newest_frames(conn, config):
    now = now_utc()
    for minutes in range(0, 120, 15):
        _add_frame_at(conn, config, "SLT", now - timedelta(minutes=minutes))
    client = FakeClient(GOOD_PAYLOAD)

    status = check_now(conn, config, origin="SLT", capture_fresh=False, client=client)

    assert status.frames_read <= config.vision.on_demand_max_frames
    assert len(client.messages.calls) == status.frames_read
    assert status.latest is not None
    assert status.latest.vehicle_count == 42
    assert status.cost_usd > 0


def test_check_does_not_re_read_frames_it_already_paid_for(conn, config):
    now = now_utc()
    _add_frame_at(conn, config, "SLT", now - timedelta(minutes=5))
    client = FakeClient(GOOD_PAYLOAD)

    first = check_now(conn, config, origin="SLT", capture_fresh=False, client=client)
    second = check_now(conn, config, origin="SLT", capture_fresh=False, client=client)

    assert first.frames_read == 1
    assert second.frames_read == 0
    assert second.cost_usd == 0
    # The answer still comes back from the stored observation.
    assert second.latest.vehicle_count == 42


def test_check_ignores_stale_frames(conn, config):
    """A frame from yesterday is not a current reading."""
    _add_frame_at(conn, config, "SLT", now_utc() - timedelta(days=1))
    client = FakeClient(GOOD_PAYLOAD)

    status = check_now(conn, config, origin="SLT", capture_fresh=False, client=client)

    assert status.frames_read == 0
    assert status.latest is None
    assert any("no recent frames" in note for note in status.notes)


def test_check_captures_a_fresh_frame_so_it_works_without_cron(conn, config, monkeypatch):
    from ferrycast import capture as capture_module
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        capture_module,
        "fetch",
        lambda url, **kw: FetchResult(ok=True, content=_bright_png(200, 150)),
    )
    client = FakeClient(GOOD_PAYLOAD)

    status = check_now(conn, config, origin="SLT", capture_fresh=True, client=client)

    assert status.captured_now
    assert status.frames_read == 1
    # Only the terminal being asked about is woken, not both cameras.
    terminals = {row["terminal"] for row in conn.execute("SELECT terminal FROM frames")}
    assert terminals == {"SLT"}


def test_check_survives_a_dead_camera_and_says_so(conn, config, monkeypatch):
    from ferrycast import capture as capture_module
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(
        capture_module, "fetch", lambda url, **kw: FetchResult(ok=False, error="HTTP 503")
    )

    status = check_now(conn, config, origin="SLT", capture_fresh=True, client=FakeClient(GOOD_PAYLOAD))

    assert not status.captured_now
    assert any("503" in note for note in status.notes)


def test_check_flags_an_unusable_night_reading(conn, config):
    _add_frame_at(conn, config, "SLT", now_utc() - timedelta(minutes=5))
    client = FakeClient({**GOOD_PAYLOAD, "visibility": "dark", "confidence": 0.1})

    status = check_now(conn, config, origin="SLT", capture_fresh=False, client=client)

    assert not status.has_reading
    assert any("not usable" in note for note in status.notes)


def test_check_and_compare_pairs_live_with_history(conn, config):
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "waited_1")
    _add_frame_at(conn, config, "SLT", now_utc() - timedelta(minutes=5))

    result = check_and_compare(
        conn,
        config,
        origin="SLT",
        target_date=date(2026, 7, 31),
        depart_hhmm="12:30",
        capture_fresh=False,
        client=FakeClient(GOOD_PAYLOAD),
    )

    assert result["status"]["latest"]["vehicle_count"] == 42
    assert result["history"]["n"] == 4
    assert result["history"]["counts"]["waited_1"] == 4
    assert result["cost_usd"] > 0


def test_history_side_of_a_check_costs_nothing(conn, config):
    """The comparison is stored records, so only the live reading is billable."""
    for day in fridays(4):
        seed_record(conn, config, day, "12:30", "waited_1")
    client = FakeClient(GOOD_PAYLOAD)

    result = check_and_compare(
        conn,
        config,
        origin="SLT",
        target_date=date(2026, 7, 31),
        depart_hhmm="12:30",
        capture_fresh=False,
        client=client,
    )

    assert client.messages.calls == []  # no frames on hand to read
    assert result["cost_usd"] == 0
    assert result["history"]["n"] == 4


# --------------------------------------------------------- deferred extraction vs pruning


def test_pruning_will_not_delete_a_frame_nobody_has_read(conn, config):
    """The trap: with extraction deferred, an unread frame is the only record left.

    Past its retention date and still unread, an analysable frame is held rather than
    dropped — otherwise the retention policy would quietly destroy the archive that a
    deferred-extraction setup exists to preserve.
    """
    from ferrycast.timeutil import local

    old = now_utc() - timedelta(days=500)
    day = local(old, config.tz).date()
    # Sitting right where an extraction would look, so thinning keeps it too.
    departure = combine_local(day, parse_hhmm("12:30"), config.tz)
    _add_frame_at(conn, config, "SLT", departure - timedelta(minutes=10))

    result = prune_frames(conn, config)

    assert result.deleted == 0
    assert conn.execute("SELECT path FROM frames").fetchone()["path"] is not None
    assert result.kept_unextracted == 1


def test_pruning_reclaims_a_frame_once_it_has_been_read(conn, config):
    old = now_utc() - timedelta(days=500)
    frame_id = _add_frame_at(conn, config, "SLT", old)
    conn.execute(
        """INSERT INTO observations
               (frame_id, prompt_version, model, vehicle_count, usable, created_at)
           VALUES (?, ?, 'test', 12, 1, ?)""",
        (frame_id, config.vision.prompt_version, iso(old)),
    )
    conn.commit()

    result = prune_frames(conn, config)

    assert result.deleted == 1
    assert result.kept_unextracted == 0


def _window_of_frames(conn, config, *, days_ago: int, hhmm: str = "12:30"):
    """A full two-hour window of unread frames around a departure `days_ago` days back."""
    from ferrycast.timeutil import local

    day = local(now_utc() - timedelta(days=days_ago), config.tz).date()
    departure = combine_local(day, parse_hhmm(hhmm), config.tz)
    for offset in range(-120, 46, 15):
        _add_frame_at(conn, config, "SLT", departure + timedelta(minutes=offset))
    return day


def test_aged_unread_frames_are_thinned_to_the_analysable_ones(conn, config):
    """Archive-now-analyse-later needs the archive to stay bounded without losing value."""
    day = _window_of_frames(conn, config, days_ago=90)

    before = len(essential_frames_for_day(conn, config, day, origin="SLT"))
    result = prune_frames(conn, config)
    after = len(essential_frames_for_day(conn, config, day, origin="SLT"))

    assert before > 0
    assert result.thinned_unextracted > 0
    # Every frame an extraction would actually read survives; only the surplus goes.
    assert after == before
    remaining = conn.execute(
        "SELECT COUNT(*) FROM frames WHERE path IS NOT NULL"
    ).fetchone()[0]
    assert remaining == before


def test_thinning_leaves_recent_unread_frames_alone(conn, config):
    _window_of_frames(conn, config, days_ago=5)

    result = prune_frames(conn, config)  # default: thin only after 45 days

    assert result.thinned_unextracted == 0


def test_thinning_can_be_disabled(conn, config):
    from dataclasses import replace

    _window_of_frames(conn, config, days_ago=90)
    never = replace(config, retention=replace(config.retention, thin_unextracted_after_days=0))

    assert prune_frames(conn, never).thinned_unextracted == 0


def test_retention_can_be_told_to_reclaim_disk_regardless(conn, config):
    from dataclasses import replace

    old = now_utc() - timedelta(days=500)
    _add_frame_at(conn, config, "SLT", old)
    aggressive = replace(config, retention=replace(config.retention, keep_unextracted=False))

    result = prune_frames(conn, aggressive)

    assert result.deleted == 1


# ---------------------------------------------------------------------- scheduled path


def test_scheduled_extraction_still_works_unchanged(conn, config):
    _add_frame(conn, config, _bright_png())
    stats = extract_pending(conn, config, client=FakeClient(GOOD_PAYLOAD))
    assert stats.extracted == 1


def test_recent_frames_are_newest_first(conn, config):
    now = now_utc()
    for minutes in (30, 5, 60):
        _add_frame_at(conn, config, "SLT", now - timedelta(minutes=minutes))

    rows = recent_frames(conn, config, "SLT", limit=3, at=now)
    ages = [row["captured_at"] for row in rows]
    assert ages == sorted(ages, reverse=True)
