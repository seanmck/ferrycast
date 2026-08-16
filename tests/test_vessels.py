"""Vessel tracking — the only departure source for a terminal with no board.

The feed says the vessel is stopped without ever naming where, so on its own a reading
cannot tell "the ship left Earls Cove" from "the ship left Saltery Bay". What settles it is
the shape of the day rather than any single reading: one boat between two ends must arrive
before it can leave again, so departures alternate, and one departure the board publishes
names every other one.

So what is pinned here is mostly the seam between that premise and the evidence for it — an
unanchored sequence names nothing, a missed transition does not invert the rest of the day,
and a second vessel stops the whole argument rather than quietly breaking it.
"""

from __future__ import annotations

from datetime import date, time, timedelta

from ferrycast.timeutil import combine_local, iso, local
from ferrycast.vessels import (
    departure_from_tracking,
    departure_ledger,
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


def _publish_departure(conn, config, day, sailing_hhmm, departed_hhmm, *, terminal="SLT"):
    """One scrape of the departures board, with a departure published against a sailing."""
    from ferrycast.timeutil import combine_local, iso, parse_hhmm

    conn.execute(
        # OR IGNORE so a test may lay the same anchor down more than once — one scrape of
        # the board is one row, and re-scraping it is what the real collector does.
        """INSERT OR IGNORE INTO deck_space (route, terminal, observed_at, service_date,
               sailing_hhmm, departed_hhmm, fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            iso(combine_local(day, parse_hhmm(departed_hhmm), config.tz)),
            day.isoformat(),
            sailing_hhmm,
            departed_hhmm,
        ),
    )
    conn.commit()


def _departure(config, hhmm="09:30", day=date(2026, 8, 14)):
    hour, minute = (int(p) for p in hhmm.split(":"))
    return combine_local(day, time(hour, minute), config.tz)


# ------------------------------------------------------------- the day's departures in order

#: About how long the crossing takes. The number matters to these tests because it is what
#: makes a per-sailing window ambiguous: a window wide enough to catch a late sailing is also
#: wide enough to contain the vessel arriving at the far end and leaving it again.
CROSSING = timedelta(minutes=50)


def _clock(moment, config):
    """Local wall clock, which is how a timetable is read and how these tests are written."""
    return local(moment, config.tz).strftime("%H:%M")


def _shuttle(conn, config, departures, day=date(2026, 8, 14)):
    """Lay down the readings one vessel produces shuttling between the two ends.

    `departures` is the actual departure times in order, whichever end each was from — which
    is the point: the feed does not say, and these tests are about deriving it. Each leg gets
    the vessel berthed beforehand, moving as it goes, and berthed again on arrival.
    """
    from ferrycast.vessels import VesselReading

    def emit(moment, status):
        store_readings(
            conn,
            config,
            moment,
            [
                VesselReading(
                    "Malaspina Sky",
                    status,
                    "",  # the heading is not read any more; the sequence settles the end
                    moment.astimezone(config.tz).strftime("%H:%M"),
                )
            ],
        )

    for hhmm in departures:
        left = _departure(config, hhmm, day)
        emit(left - timedelta(minutes=10), "Stopped")
        emit(left, "Under Way")
        emit(left + CROSSING - timedelta(minutes=10), "Under Way")
        emit(left + CROSSING, "Stopped")


def _anchor(conn, config, hhmm="08:34", sailing="08:30", day=date(2026, 8, 14)):
    """A Saltery Bay departure the board published *and* the tracker saw go.

    Every ledger needs at least one. Alternation says the ends take turns; it takes a
    published departure to say which end one of the turns was. Production gets eight a day,
    so this is the ordinary case rather than a favourable one.
    """
    _shuttle(conn, config, [hhmm], day)
    _publish_departure(conn, config, day, sailing, hhmm, terminal="SLT")


def test_a_departure_is_read_from_stopped_then_moving_outbound(conn, config):
    """Earls Cove sailings have no other source of a departure time at all."""
    # Saltery Bay's 08:30 went at 08:34 and the board published it; the 09:35 that follows is
    # therefore the other end's, because the vessel has to cross before it can leave again.
    _shuttle(conn, config, ["08:34", "09:35"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    left = departure_from_tracking(
        conn, config, origin="ERL", departure=_departure(config, "09:30")
    )

    # The first moving reading: an upper bound, since it had certainly gone by then.
    assert left == _departure(config, "09:35")


def test_one_published_departure_names_the_whole_day(conn, config):
    """The premise, stated on its own: a two-point shuttle with one boat alternates ends, so
    the parity of any single departure is the parity of all of them.

    Only the 08:30 is published here. That is enough to name the 09:35 as Earls Cove's and
    the 12:34 after it as Saltery Bay's — including, crucially, while the board has not got
    round to publishing the 12:34 itself."""
    _shuttle(conn, config, ["08:34", "09:35", "12:34"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    ledger = departure_ledger(conn, config, target_date=date(2026, 8, 14))

    assert [(_clock(d.at, config), d.terminal) for d in ledger.departures] == [
        ("08:34", "SLT"),
        ("09:35", "ERL"),
        ("12:34", "SLT"),
    ]


def test_a_late_sailing_is_not_handed_the_other_ends_departure(conn, config):
    """The failure that motivated the ledger, and the one a window search cannot avoid here.

    Saltery Bay's 12:30 ran badly late and left at 13:15. Its window opens at 12:10, and the
    Earls Cove departure at 12:20 — the leg that was still bringing the vessel over — falls
    inside it and *before* the real one. Taking the earliest transition in the window is
    therefore wrong, and there is nothing at Saltery Bay to subtract it with: the rule that
    saves Earls Cove is "a transition the board does not publish belongs to the other end",
    and the other end from Saltery Bay is the one that publishes nothing at all.

    So a traveller at Saltery Bay would have been told the 12:30 had gone at 12:20, while the
    vessel was still half an hour from the berth and the queue was still sitting there.
    """
    _shuttle(conn, config, ["08:34", "12:20", "13:15"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    left = departure_from_tracking(
        conn, config, origin="SLT", departure=_departure(config, "12:30")
    )

    assert left == _departure(config, "13:15")


def test_a_crossing_already_under_way_is_not_a_departure(conn, config):
    """Without a stopped reading first, this is the middle of a sailing, not the start."""
    departure = _departure(config)
    _track(conn, config, departure, [(5, "Under Way", "W"), (20, "Under Way", "W")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_a_vessel_that_never_left_yields_no_departure(conn, config):
    departure = _departure(config)
    _track(conn, config, departure, [(-10, "In Port", "S"), (10, "In Port", "S")])

    assert departure_from_tracking(conn, config, origin="ERL", departure=departure) is None


def test_a_departure_survives_the_vessel_arriving_at_the_other_end(conn, config):
    """The regression that emptied the Earls Cove column.

    A window reaching 75 minutes past the scheduled time so a late sailing is still caught
    also contains, on a fifty-minute crossing, the vessel arriving at the far end. Anchored
    on the *last* stopped reading, the old search found that arrival rather than the
    departure, and a berthed vessel produces no moving reading after it to return — so every
    punctual Earls Cove sailing reported nothing at all. The ledger has no such anchor to get
    wrong: the arrival is not a departure, and the departures are taken in order.
    """
    _shuttle(conn, config, ["08:34", "09:35"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    assert departure_from_tracking(
        conn, config, origin="ERL", departure=_departure(config, "09:30")
    ) == _departure(config, "09:35")


def test_the_next_sailings_departure_is_not_borrowed(conn, config):
    """A sailing whose own departure was never tracked is left with nothing rather than
    handed the next one's. The pairing is exclusive and bounded, so it refuses instead of
    reaching for whatever is nearest."""
    _shuttle(conn, config, ["08:34", "12:34"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    # Earls Cove's 09:30 never went; the next tracked departure is Saltery Bay's, an hour
    # after this sailing's tolerance runs out.
    assert (
        departure_from_tracking(
            conn, config, origin="ERL", departure=_departure(config, "09:30")
        )
        is None
    )


def test_a_transition_the_board_claims_is_not_the_other_terminals(conn, config):
    """The anchor itself. Saltery Bay publishes its departures, so a transition matching one
    is Saltery Bay's, whatever the vessel happened to be pointing at while it left."""
    _shuttle(conn, config, ["09:35"])
    _publish_departure(conn, config, date(2026, 8, 14), "09:30", "09:35", terminal="SLT")

    assert (
        departure_from_tracking(
            conn, config, origin="ERL", departure=_departure(config, "09:30")
        )
        is None
    )


def test_with_nothing_published_the_ledger_names_no_end(conn, config):
    """Alternation is only ever as good as the fact it is anchored to. With no published
    departure anywhere in the day there is no fact — only a pattern, and a pattern that could
    equally be run off either foot. Naming an end here would be a coin toss dressed as
    evidence, so the ledger declines and every caller falls back to what it had before."""
    _shuttle(conn, config, ["08:34", "09:35", "12:34"])

    ledger = departure_ledger(conn, config, target_date=date(2026, 8, 14))

    assert [d.terminal for d in ledger.departures] == [None, None, None]
    assert not ledger.named_any
    # The departures themselves are not in doubt — only which end each was from.
    assert len(ledger.departures) == 3


def test_a_missed_transition_does_not_invert_the_rest_of_the_day(conn, config):
    """The check a window search cannot make, because it has nothing to be inconsistent with.

    The feed went quiet across the whole Earls Cove turnaround, so its 09:35 departure was
    never seen. Walking alternation naively from the 08:34 would then call the 12:34 Earls
    Cove's and the 13:40 Saltery Bay's — both exactly wrong, and wrong silently, for the rest
    of the day.

    Two consecutive departures the board says are *both* Saltery Bay's cannot be one step
    apart, because the vessel has to go somewhere in between. That contradiction is visible,
    so the chain is cut at it and re-anchored on the far side, and only the stretch that
    cannot be walked goes unnamed. The board publishes often enough that a gap costs the
    departures inside it and nothing beyond.
    """
    _shuttle(conn, config, ["08:34"])
    # 09:35 happened; the tracker was blind across it and stored nothing.
    _shuttle(conn, config, ["12:34", "13:40"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")
    _publish_departure(conn, config, date(2026, 8, 14), "12:30", "12:34", terminal="SLT")

    ledger = departure_ledger(conn, config, target_date=date(2026, 8, 14))

    assert [(_clock(d.at, config), d.terminal) for d in ledger.departures] == [
        ("08:34", "SLT"),
        ("12:34", "SLT"),
        ("13:40", "ERL"),
    ]


def test_two_vessels_stop_the_ledger_entirely(conn, config):
    """One vessel is a premise, not a detail: two boats interleave their departures into one
    sequence and alternation stops meaning anything. The feed would report the second without
    any other sign that the reasoning had stopped holding, so it is asserted rather than
    assumed."""
    from ferrycast.vessels import VesselReading

    _shuttle(conn, config, ["08:34", "09:35"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")
    moment = _departure(config, "10:00")
    store_readings(
        conn,
        config,
        moment,
        [VesselReading("Island Sky", "Under Way", "", moment.astimezone(config.tz).strftime("%H:%M"))],
    )

    ledger = departure_ledger(conn, config, target_date=date(2026, 8, 14))

    assert ledger.departures == ()
    # Still a reading, so freshness is unaffected — the tracker is alive, it just cannot be
    # reasoned about this way any more.
    assert ledger.observed_at is not None


def test_two_departures_too_close_together_cut_the_chain(conn, config):
    """A step shorter than a crossing plus a turnaround is not the next departure — most
    likely the vessel stopped mid-crossing and the stop registered as one. The chain is cut
    there rather than carried across a step that cannot be real."""
    _shuttle(conn, config, ["08:34"])
    # Fifteen minutes after leaving, the vessel stops mid-strait and gets under way again.
    _track(
        conn,
        config,
        _departure(config, "08:49"),
        [(0, "Stopped", ""), (5, "Under Way", "")],
    )
    _shuttle(conn, config, ["09:35"])
    _publish_departure(conn, config, date(2026, 8, 14), "08:30", "08:34", terminal="SLT")

    ledger = departure_ledger(conn, config, target_date=date(2026, 8, 14))

    named = {_clock(d.at, config): d.terminal for d in ledger.departures}
    # The anchored departure keeps its end; nothing past the impossible step is named off it.
    assert named["08:34"] == "SLT"
    assert named["09:35"] is None


# ------------------------------------------------------------------------ into the record


def test_the_record_carries_the_tracked_departure(conn, config):
    """The point of the exercise: an Earls Cove sailing whose departure is known."""
    from ferrycast.aggregate import aggregate_day

    day = date(2026, 8, 14)
    _anchor(conn, config, day=day)
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
    _anchor(conn, config)
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

    _anchor(conn, config)
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


# ------------------------------------------------- what the board asks: has it gone yet?


def _watch(conn, config, day, times, now_hhmm, origin="ERL"):
    from ferrycast.vessels import tracker_watch

    return tracker_watch(
        conn,
        config,
        origin=origin,
        target_date=day,
        times=times,
        now=_departure(config, now_hhmm, day),
    )


def test_a_late_sailing_reads_as_still_here_not_as_gone(conn, config):
    """The failure this exists to fix. At Earls Cove nothing publishes a departure, so the
    board fell through to the clock and dimmed the 15:40 the moment 15:40 passed — while the
    webcam beside it showed the queue still waiting for a boat that had not arrived."""
    day = date(2026, 8, 14)
    departure = _departure(config, "15:40", day)
    # The vessel is still crossing *towards* this terminal: moving, and moving inbound.
    _track(conn, config, departure, [(-20, "Under Way", "E"), (-5, "Under Way", "SE"),
                                     (10, "Under Way", "S")])

    watch = _watch(conn, config, day, ["15:40"], "15:53")

    assert watch.not_away == frozenset({"15:40"})
    assert watch.departed == frozenset()


def test_a_sailing_the_tracker_watched_leave_reads_as_gone(conn, config):
    day = date(2026, 8, 14)
    _anchor(conn, config, "14:34", "14:30", day)
    departure = _departure(config, "15:40", day)
    _track(conn, config, departure, [(-10, "Stopped", "NW"), (6, "Under Way", "W")])

    watch = _watch(conn, config, day, ["15:40"], "16:10")

    assert watch.departed == frozenset({"15:40"})
    assert watch.not_away == frozenset()


def test_a_sailing_not_yet_due_is_left_alone(conn, config):
    """A boat that is not due has obviously not gone; the board says so with a countdown."""
    day = date(2026, 8, 14)
    departure = _departure(config, "15:40", day)
    _track(conn, config, departure, [(-40, "Under Way", "E")])

    watch = _watch(conn, config, day, ["15:40", "18:00"], "15:20")

    assert "18:00" not in watch.not_away and "18:00" not in watch.departed
    assert "15:40" not in watch.not_away  # not due either


def test_a_sailing_far_past_its_time_is_abstained_on(conn, config):
    """Beyond the span a departure could still turn up in, the tracker stops claiming. Left
    unbounded, an undetected departure would hold "not away yet" for the rest of the day and
    tell somebody a boat they had missed was still catchable."""
    day = date(2026, 8, 14)
    departure = _departure(config, "15:40", day)
    _track(conn, config, departure, [(-20, "Under Way", "E")])

    watch = _watch(conn, config, day, ["15:40"], "17:30")  # 110 min after schedule

    assert watch.not_away == frozenset()
    assert watch.departed == frozenset()


def test_the_watch_also_defers_to_a_published_departure(conn, config):
    day = date(2026, 8, 14)
    departure = _departure(config, "15:40", day)
    _track(conn, config, departure, [(-10, "Stopped", "NW"), (6, "Under Way", "NE")])
    _publish_departure(conn, config, day, "14:30", "15:46", terminal="SLT")

    watch = _watch(conn, config, day, ["15:40"], "16:10")

    assert watch.departed == frozenset()
    assert watch.not_away == frozenset({"15:40"})


def test_a_silent_tracker_is_not_fresh(conn, config):
    from datetime import timedelta as td

    day = date(2026, 8, 14)
    departure = _departure(config, "15:40", day)
    _track(conn, config, departure, [(-20, "Under Way", "E")])

    watch = _watch(conn, config, day, ["15:40"], "15:53")
    now = _departure(config, "15:53", day)
    assert watch.is_fresh(now, td(minutes=40))
    assert not watch.is_fresh(now + td(hours=2), td(minutes=40))


def test_the_heading_no_longer_decides_anything(conn, config):
    """What replaced the compass. The vessel swings north-east out of the narrow cove at
    Earls Cove before turning north-west across the strait, and `NE` contains an `E` — so
    reading the heading called an outbound sailing inbound and refused it. The 15:40 that
    left at 16:26 still showed "not away yet" ten minutes later. Headings are now ignored
    entirely; a stopped-then-moving vessel has departed, and the board says from where."""
    departure = _departure(config)
    for heading in ("NE", "N", "S", "E", ""):
        conn.execute("DELETE FROM vessel_positions")
        conn.commit()
        _anchor(conn, config)
        _track(conn, config, departure, [(-10, "Stopped", heading), (5, "Under Way", heading)])
        assert departure_from_tracking(
            conn, config, origin="ERL", departure=departure
        ) == departure + timedelta(minutes=5), heading
