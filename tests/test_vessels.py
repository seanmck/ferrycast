"""Vessel tracking — the only departure source for a terminal with no board.

What is pinned here is mostly refusal. The feed reports `In Port` without ever naming the
port, so the single thing standing between "the ship left Earls Cove" and "the ship left
Saltery Bay" is its heading once it is moving. Getting that wrong would file the homeward
sailing's departure against the outbound one, so an ambiguous heading has to mean no answer.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from ferrycast.timeutil import combine_local, iso
from ferrycast.vessels import (
    departure_from_tracking,
    parse_tracking,
    refresh,
    store_readings,
)

# The real page, trimmed to what the parser reads. Four plain cells is a vessel; the
# explanatory rows underneath use colspan and must not be mistaken for one.
TRACKER = """
<html><head>
<script language="JavaScript">
function onMapHover(e) {
  if (x >= 166 && y >= 251) {
    html += '<td><b>Malaspina Sky</b></td>';
    html += '<td>Heading: <b>W</b></td>';
    html += '<td>Speed: <b>14.9 knots</b></td>';
  }
}
</script>
</head><body>
<div id="map_image"><img src="route29.jpg"/></div>
<table>
  <tr><td><b>Vessel</b></td><td><b>Status</b></td><td><b>Heading</b></td><td><b>Last Update</b></td></tr>
  <tr><td>Malaspina Sky</td><td>Under Way</td><td>W</td><td>7:04 AM</td></tr>
  <tr><td colspan='4'>&nbsp;</td></tr>
  <tr><td colspan='4'><i>Each arrow icon represents one of our vessels. When the icon is
      displayed, the ship is in port.</i></td></tr>
</table>
</body></html>
"""

def test_a_vessel_line_is_read_off_the_tracker():
    readings = parse_tracking(TRACKER)
    assert len(readings) == 1
    reading = readings[0]
    assert reading.vessel == "Malaspina Sky"
    assert reading.under_way and not reading.in_port
    assert reading.heading == "W"
    assert reading.updated_hhmm == "07:04"
    assert reading.speed_knots == 14.9


def test_the_pages_own_prose_is_not_read_as_a_vessel():
    """The explanatory rows sit in the same table and mention ships being in port."""
    assert [r.vessel for r in parse_tracking(TRACKER)] == ["Malaspina Sky"]


# Busier routes carry more fields per vessel, and several vessels at once. Found against the
# live Tsawwassen tracker, where a `Destination:` line sits between the name and the speed.
BUSY_TRACKER = """
<html><head><script>
  html += '<td><b>Queen of Alberni</b></td>';
  html += '<td>Destination: <b>Tsawwassen</b></td>';
  html += '<td>Heading: <b>S</b></td>';
  html += '<td>Speed: <b>0 knots</b></td>';
  html += '<td><b>Coastal Renaissance</b></td>';
  html += '<td>Destination: <b>Tsawwassen</b></td>';
  html += '<td>Heading: <b>S</b></td>';
  html += '<td>Speed: <b>20 knots</b></td>';
</script></head><body>
<table>
  <tr><td><b>Vessel</b></td><td><b>Status</b></td><td><b>Heading</b></td><td><b>Last Update</b></td></tr>
  <tr><td>Queen of Alberni</td><td>In Port</td><td>Tsawwassen</td><td>7:04 AM</td></tr>
  <tr><td>Coastal Renaissance</td><td>Under Way</td><td>Tsawwassen</td><td>7:04 AM</td></tr>
</table>
</body></html>
"""


def test_every_vessel_gets_its_own_speed():
    """A labelled field is never the vessel's name. Matching only 'Heading:' filed each
    speed under the destination port on routes that publish one."""
    readings = {r.vessel: r for r in parse_tracking(BUSY_TRACKER)}
    assert set(readings) == {"Queen of Alberni", "Coastal Renaissance"}
    assert readings["Queen of Alberni"].speed_knots == 0.0
    assert readings["Queen of Alberni"].in_port
    assert readings["Coastal Renaissance"].speed_knots == 20.0


def test_an_unrecognisable_page_yields_nothing_rather_than_raising():
    assert parse_tracking("<html><body>maintenance</body></html>") == []
    assert parse_tracking("") == []


def test_afternoon_times_are_converted():
    assert parse_tracking(TRACKER.replace("7:04 AM", "12:05 AM"))[0].updated_hhmm == "00:05"
    assert parse_tracking(TRACKER.replace("7:04 AM", "12:05 PM"))[0].updated_hhmm == "12:05"
    assert parse_tracking(TRACKER.replace("7:04 AM", "9:47 PM"))[0].updated_hhmm == "21:47"


# ------------------------------------------------------------------------ polling & storage


def _fetching(monkeypatch, text):
    from ferrycast import vessels
    from ferrycast.fetching import FetchResult

    monkeypatch.setattr(vessels, "fetch", lambda *a, **k: FetchResult(ok=True, text=text))


def _with_tracker(config):
    from dataclasses import replace

    return replace(
        config,
        routes=tuple(
            replace(r, vessel_tracking_url="https://example.invalid/route29.html")
            for r in config.routes
        ),
    )


def test_polling_stores_a_reading(conn, config, monkeypatch):
    config = _with_tracker(config)
    _fetching(monkeypatch, TRACKER)

    result = refresh(conn, config)

    assert result["ok"] and result["rows"] == 1
    row = conn.execute(
        "SELECT vessel, status, speed_knots FROM vessel_positions"
    ).fetchone()
    assert row["vessel"] == "Malaspina Sky"
    assert row["status"] == "Under Way"
    assert row["speed_knots"] == 14.9


def test_the_same_instant_is_not_stored_twice(conn, config, monkeypatch):
    """The page refreshes every 30s but the position behind it moves about every 5 min, so
    polling on the capture cadence re-reads one instant repeatedly."""
    config = _with_tracker(config)
    _fetching(monkeypatch, TRACKER)

    refresh(conn, config)
    second = refresh(conn, config)

    assert second["rows"] == 0
    assert conn.execute("SELECT COUNT(*) FROM vessel_positions").fetchone()[0] == 1


def test_a_tracker_that_cannot_be_read_is_recorded_not_raised(conn, config, monkeypatch):
    config = _with_tracker(config)
    _fetching(monkeypatch, "<html><body>site maintenance</body></html>")

    result = refresh(conn, config)

    assert not result["ok"]
    assert conn.execute(
        "SELECT fetch_status FROM vessel_positions"
    ).fetchone()["fetch_status"] == "unparsed"


def test_no_tracker_configured_is_a_skip(conn, config):
    assert refresh(conn, config)["skipped"] is True


# ------------------------------------------------------------------- deriving a departure


def _track(conn, config, departure, entries):
    """`entries` is (minutes relative to departure, status, heading)."""
    from ferrycast.vessels import VesselReading

    for offset, status, heading in entries:
        moment = departure + timedelta(minutes=offset)
        store_readings(
            conn,
            config,
            moment,
            [VesselReading("Malaspina Sky", status, heading, moment.astimezone(config.tz).strftime("%H:%M"))],
        )


def _departure(config, hhmm="09:30", day=date(2026, 8, 14)):
    hour, minute = (int(p) for p in hhmm.split(":"))
    return combine_local(day, time(hour, minute), config.tz)


def test_a_departure_is_read_from_stopped_then_moving_outbound(conn, config):
    """Earls Cove sailings have no other source of a departure time at all."""
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "In Port", "S"), (5, "Under Way", "W"), (20, "Under Way", "NW")])

    left = departure_from_tracking(conn, config, origin="ERL", departure=departure)

    # The first moving reading: an upper bound, since it had certainly gone by then.
    assert left == departure + timedelta(minutes=5)


def test_the_other_terminals_departure_is_not_claimed_as_this_ones(conn, config):
    """The feed never says which port a ship is docked at, so a vessel leaving Saltery Bay
    eastbound looks identical to one leaving Earls Cove until you read the heading."""
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "In Port", "S"), (5, "Under Way", "E")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None
    assert departure_from_tracking(conn, config, origin="SLT", departure=departure) is not None


def test_an_ambiguous_heading_is_refused_rather_than_guessed(conn, config):
    """A vessel manoeuvring off the berth can point anywhere for a reading or two. 'N' says
    nothing about east or west, and guessing would file the sailing against a terminal."""
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "In Port", "S"), (5, "Under Way", "N")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_a_manoeuvring_reading_does_not_hide_the_real_direction(conn, config):
    departure = _departure(config)
    _track(
        conn, config, departure,
        [(-10, "In Port", "S"), (5, "Under Way", "N"), (15, "Under Way", "W")],
    )

    left = departure_from_tracking(conn, config, origin="ERL", departure=departure)
    assert left == departure + timedelta(minutes=5)


def test_a_crossing_already_under_way_is_not_a_departure(conn, config):
    """Without a stopped reading first, this is the middle of a sailing, not the start."""
    departure = _departure(config)
    _track(conn, config, departure, [(5, "Under Way", "W"), (20, "Under Way", "W")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_a_vessel_that_never_left_yields_no_departure(conn, config):
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "In Port", "S"), (10, "In Port", "S")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_the_next_sailings_departure_is_out_of_range(conn, config):
    """The window stops well short of the tightest headway on this route, so one sailing
    cannot borrow the next one's departure and call itself very late."""
    departure = _departure(config)
    _track(conn, config, departure, [(100, "In Port", "S"), (110, "Under Way", "W")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_a_terminal_with_no_configured_bearing_never_infers_one(conn, config):
    """Bearing is route geometry, so it is configuration. Absent it, no claim is made —
    a wrong one would attribute every sailing to the wrong end of the route."""
    from dataclasses import replace

    blind = replace(
        config,
        routes=tuple(
            replace(r, terminals=tuple(replace(t, outbound_bearing="") for t in r.terminals))
            for r in config.routes
        ),
    )
    departure = _departure(blind)
    _track(conn, blind, departure, [(-10, "In Port", "S"), (5, "Under Way", "W")])

    assert departure_from_tracking(conn, blind, origin="ERL", departure=departure) is None


# ------------------------------------------------------------------------ into the record


def test_the_record_carries_the_tracked_departure(conn, config):
    """The point of the exercise: an Earls Cove sailing whose departure is known."""
    from ferrycast.aggregate import aggregate_day

    day = date(2026, 8, 14)
    departure = _departure(config, "09:30", day)
    _track(conn, config, departure, [(-10, "In Port", "S"), (6, "Under Way", "W")])

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.departed_at, r.departed_source, r.outcome FROM sailings s
             JOIN sailing_records r ON r.sailing_id = s.id
            WHERE s.origin = 'ERL' AND s.depart_hhmm = '09:30'"""
    ).fetchone()
    assert row["departed_at"] == iso(departure + timedelta(minutes=6))
    assert row["departed_source"] == "tracking"
    # ...and it stays `unknown`, because seeing the ship go says nothing about the deck or
    # the compound. Tracking establishes when, never whether anyone got on.
    assert row["outcome"] == "unknown"


def test_the_board_outranks_the_tracker(conn, config):
    """Both describe the same departure; the board reports it to the minute and the tracker
    to about five, so where the board speaks it wins."""
    from ferrycast.aggregate import aggregate_day

    day = date(2026, 8, 14)
    departure = _departure(config, "12:30", day)
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                departed_hhmm, status_text, fetch_status)
           VALUES (?, 'SLT', ?, ?, '12:30', '12:33', 'Departed 12:33 pm', 'ok')""",
        (config.route.id, iso(departure + timedelta(minutes=20)), day.isoformat()),
    )
    conn.commit()
    _track(conn, config, departure, [(-10, "In Port", "S"), (8, "Under Way", "E")])

    aggregate_day(conn, config, day)

    row = conn.execute(
        """SELECT r.departed_at, r.departed_source FROM sailings s
             JOIN sailing_records r ON r.sailing_id = s.id
            WHERE s.origin = 'SLT' AND s.depart_hhmm = '12:30'"""
    ).fetchone()
    assert row["departed_source"] == "board"
    assert row["departed_at"] == iso(departure + timedelta(minutes=3))


# ------------------------------------------------------- the status word is per-route


def test_stopped_is_recognised_as_well_as_in_port(conn, config):
    """Route 1 says `In Port`; this route says `Stopped`. Matching only the first meant the
    live tracker never once produced a departure — Earls Cove, whose every departure this
    is the only source of, saw nothing at all for as long as it ran."""
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "Stopped", "NW"), (5, "Under Way", "W")])

    assert departure_from_tracking(
        conn, config, origin="ERL", departure=departure
    ) == departure + timedelta(minutes=5)


def test_an_unfamiliar_status_falls_back_to_speed(conn, config):
    """A word we have not seen must not read as "not stopped". Knots are far less likely to
    be reworded than a label, so they are the safety net under the vocabulary."""
    from ferrycast.vessels import VesselReading, is_moving, store_readings

    assert is_moving("Alongside", 0.0) is False
    assert is_moving("Alongside", 12.0) is True
    assert is_moving("Alongside", None) is None

    departure = _departure(config)
    for offset, status, heading, speed in [
        (-10, "Alongside", "NW", 0.0),
        (5, "Making Way", "W", 11.4),
    ]:
        moment = departure + timedelta(minutes=offset)
        store_readings(
            conn,
            config,
            moment,
            [
                VesselReading(
                    "Malaspina Sky",
                    status,
                    heading,
                    moment.astimezone(config.tz).strftime("%H:%M"),
                    speed,
                )
            ],
        )

    assert departure_from_tracking(
        conn, config, origin="ERL", departure=departure
    ) == departure + timedelta(minutes=5)


def test_the_live_wording_parses_as_stopped():
    """Pinned against what route 29 actually publishes."""
    from ferrycast.vessels import parse_tracking

    stopped = TRACKER.replace("<td>Under Way</td><td>W</td>", "<td>Stopped</td><td>NW</td>")
    stopped = stopped.replace("14.9 knots", "0 knots")
    reading = parse_tracking(stopped)[0]
    assert reading.in_port and not reading.under_way
    assert reading.speed_knots == 0.0
