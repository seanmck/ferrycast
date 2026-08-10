"""v1 tracks one route, but the seams that would be expensive to change later are here now.

The expensive things are the database keys (a UNIQUE constraint is a migration on live
data) and the query filters (mixing two routes produces a confidently wrong answer rather
than an error). These tests pin both down using a second, inactive route.
"""

from datetime import date

import pytest

from ferrycast.config import ConfigError, load_config
from ferrycast.db import SCHEMA_VERSION, init_db, schema_version
from ferrycast.query import query_distribution
from ferrycast.schedule import load_schedule, sailings_for_day
from ferrycast.timeutil import combine_local, parse_hhmm

from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

# A second route sharing a terminal with the first — the case that breaks naive keys.
# Horseshoe Bay really does serve several routes at once.
SECOND_ROUTE = """
  [[route]]
  id = "route3"
  name = "Horseshoe Bay - Langdale"

    [[route.terminals]]
    code = "HSB"
    name = "Horseshoe Bay"
    destination = "LNG"
    webcam_url = "https://example.invalid/hsb.jpg"

    [[route.terminals]]
    code = "LNG"
    name = "Langdale"
    destination = "HSB"
    webcam_url = "https://example.invalid/lng.jpg"
"""


def _multi_route_config(tmp_path, *, active: str | None = "route7"):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    body = CONFIG_TEMPLATE.replace("[route]", "[[route]]").replace(
        "  [[route.terminals]]", "    [[route.terminals]]"
    )
    if active:
        body = body.replace('data_dir = "data"', f'data_dir = "data"\nactive_route = "{active}"')
    (config_dir / "ferrycast.toml").write_text(body + SECOND_ROUTE)
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    return load_config(config_dir / "ferrycast.toml")


def test_v1_config_needs_no_route_boilerplate(config):
    """The single-route form stays the simple default."""
    assert len(config.routes) == 1
    assert config.route.id == "route7"
    assert config.active_route_id == "route7"


def test_several_routes_can_be_defined_with_one_active(tmp_path):
    multi = _multi_route_config(tmp_path)
    assert {r.id for r in multi.routes} == {"route7", "route3"}
    # v1 behaviour is unchanged: everything operates on the active route.
    assert multi.route.id == "route7"
    assert multi.route_by_id("route3").name == "Horseshoe Bay - Langdale"


def test_ambiguous_multi_route_config_is_rejected_with_guidance(tmp_path):
    with pytest.raises(ConfigError, match="active_route"):
        _multi_route_config(tmp_path, active=None)


def test_unknown_active_route_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not defined"):
        _multi_route_config(tmp_path, active="route99")


def test_duplicate_route_ids_are_rejected(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    body = CONFIG_TEMPLATE.replace("[route]", "[[route]]").replace(
        "  [[route.terminals]]", "    [[route.terminals]]"
    )
    (config_dir / "ferrycast.toml").write_text(
        body + SECOND_ROUTE.replace('id = "route3"', 'id = "route7"')
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    with pytest.raises(ConfigError, match="duplicate route id"):
        load_config(config_dir / "ferrycast.toml")


def test_two_routes_can_share_a_terminal_and_departure_minute(conn, config):
    """The key fix: Horseshoe Bay can run a 15:25 to Langdale and a 15:25 to Nanaimo.

    Keyed only on (origin, scheduled_departure), the second sailing would collide with the
    first and be silently dropped.
    """
    departure = combine_local(date(2026, 8, 14), parse_hhmm("15:25"), config.tz)
    for route, destination in [("route3", "LNG"), ("route4", "NAN")]:
        conn.execute(
            """INSERT INTO sailings
                   (route, origin, destination, service_date, scheduled_departure,
                    depart_hhmm, day_type, season)
               VALUES (?, 'HSB', ?, '2026-08-14', ?, '15:25', 'friday', 'peak_summer')""",
            (route, destination, departure.isoformat()),
        )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM sailings").fetchone()[0] == 2


def test_deck_space_can_hold_one_sailing_time_per_route(conn, config):
    """The conditions page is per route-direction, so the same terminal reports both."""
    for route in ("route3", "route4"):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    percent_available, fetch_status)
               VALUES (?, 'HSB', '2026-08-14T22:00:00Z', '2026-08-14', '15:25', 40, 'ok')""",
            (route,),
        )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM deck_space").fetchone()[0] == 2


def test_a_frame_is_shared_across_routes_rather_than_duplicated(conn, config):
    """A camera belongs to a terminal, not a route, so frames stay keyed on the terminal."""
    for route in ("route3", "route4"):
        conn.execute(
            """INSERT OR IGNORE INTO frames
                   (route, terminal, captured_at, service_date, path, status)
               VALUES (?, 'HSB', '2026-08-14T22:00:00Z', '2026-08-14', 'x.jpg', 'ok')""",
            (route,),
        )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1


def test_a_query_never_mixes_another_routes_history(conn, config):
    """Cross-route contamination would be a confidently wrong answer, not a visible error."""
    departure_day = date(2026, 7, 3)
    for offset in range(4):
        day = date.fromordinal(departure_day.toordinal() + 7 * offset)
        departure = combine_local(day, parse_hhmm("12:30"), config.tz)
        # Same origin code and time on a different route, with the opposite outcome.
        for route, outcome in [("route7", "waited_1"), ("other", "boarded")]:
            cur = conn.execute(
                """INSERT INTO sailings
                       (route, origin, destination, service_date, scheduled_departure,
                        depart_hhmm, day_type, season)
                   VALUES (?, 'SLT', 'ERL', ?, ?, '12:30', 'friday', 'peak_summer')""",
                (route, day.isoformat(), departure.isoformat()),
            )
            conn.execute(
                """INSERT INTO sailing_records (sailing_id, outcome, n_frames, computed_at)
                   VALUES (?, ?, 8, '2026-01-01T00:00:00Z')""",
                (cur.lastrowid, outcome),
            )
    conn.commit()

    result = query_distribution(
        conn, config, origin="SLT", target_date=date(2026, 7, 31), depart_hhmm="12:30"
    )

    assert result.n == 4
    assert result.counts["waited_1"] == 4
    assert result.counts["boarded"] == 0


def test_schedule_blocks_may_name_a_route(config, tmp_path):
    path = tmp_path / "multi.toml"
    path.write_text(
        """
[[block]]
route = "route7"
terminal = "SLT"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["08:30"]

[[block]]
route = "route3"
terminal = "HSB"
effective_from = 2020-01-01
effective_to = 2030-12-31
days = "all"
departures = ["09:00", "11:00"]
"""
    )
    blocks = load_schedule(path)

    route7 = sailings_for_day(
        blocks, date(2026, 8, 14), "route7", {"SLT": "ERL"}, config.tz
    )
    assert [s.depart_hhmm for s in route7] == ["08:30"]

    route3 = sailings_for_day(
        blocks, date(2026, 8, 14), "route3", {"HSB": "LNG"}, config.tz
    )
    assert [s.depart_hhmm for s in route3] == ["09:00", "11:00"]


def test_unrouted_schedule_blocks_apply_to_whatever_route_asks(config, tmp_path):
    """Single-route schedules stay boilerplate-free."""
    path = tmp_path / "plain.toml"
    path.write_text(SCHEDULE_TEMPLATE)
    blocks = load_schedule(path)
    sailings = sailings_for_day(
        blocks, date(2026, 8, 14), "anything", {"SLT": "ERL"}, config.tz
    )
    assert [s.depart_hhmm for s in sailings] == ["08:30", "12:30", "16:30"]


def test_database_records_its_schema_version(tmp_path):
    conn = init_db(tmp_path / "v.db")
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_a_newer_database_is_refused_rather_than_corrupted(tmp_path):
    from ferrycast.db import SchemaTooNewError

    path = tmp_path / "future.db"
    conn = init_db(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaTooNewError, match="Upgrade FerryCast"):
        init_db(path)
