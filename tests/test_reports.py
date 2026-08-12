"""Manual sailing reports — the one signal the pipeline cannot collect for itself.

The things most likely to be silently wrong are here: that a report actually reaches the
sailing record without waiting for the nightly job, that one person left behind outweighs
any number who got on, and that a report is never allowed to move the "arrive before" time
in the direction that would flatter the answer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from ferrycast.aggregate import aggregate_day
from ferrycast.reports import (
    ReportError,
    describe_reports,
    fetch_reports,
    outcome_from_reports,
    parse_clock,
    submit_report,
)
from ferrycast.timeutil import combine_local, iso, parse_hhmm
from ferrycast.web.app import create_app

# A Friday that has been and gone, with a 12:30 sailing from Saltery Bay in the timetable.
SAILED = date(2026, 7, 3)
NOW = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)


@pytest.fixture
def client(conn, config, monkeypatch):
    # Reports may only be filed against a sailing that has departed, so the clock has to be
    # pinned everywhere it is read: the page decides whether to offer the form, the
    # submission checks it again, and `upcoming_sailings` reads it too. Leaving the page's
    # clock on real time makes "a sailing that has not gone" depend on the day the suite
    # runs.
    monkeypatch.setattr("ferrycast.reports.now_utc", lambda: NOW)
    monkeypatch.setattr("ferrycast.query.now_utc", lambda: NOW)
    monkeypatch.setattr("ferrycast.web.app.now_utc", lambda: NOW)
    app = create_app(str(config.source_path))
    with TestClient(app) as test_client:
        yield test_client


def file_report(conn, config, **kwargs):
    fields = {
        "origin": "SLT",
        "service_date": SAILED,
        "depart_hhmm": "12:30",
        "boarded": True,
        "now": NOW,
    }
    fields.update(kwargs)
    return submit_report(conn, config, **fields)


def board_says_departed(conn, config, departed: str, *, hhmm="12:30", origin="SLT"):
    """The departures board publishing an actual departure time.

    Reports no longer ask for it — the board gives it to the minute, and one fewer field
    matters when the form is filled in at the side of a highway. The stored value still
    drives validation, so tests that used to pass `departed=` now seed the board instead.
    """
    conn.execute(
        """INSERT INTO deck_space
               (route, terminal, observed_at, service_date, sailing_hhmm,
                departed_hhmm, status_text, fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, 'Departed', 'ok')""",
        (
            config.route.id,
            origin,
            f"{SAILED.isoformat()}T20:00:00Z",
            SAILED.isoformat(),
            hhmm,
            departed,
        ),
    )
    conn.commit()


def outcome_of(conn, service_date: date, hhmm: str, origin: str = "SLT") -> str:
    row = conn.execute(
        """SELECT r.outcome, r.method, r.filled_at
             FROM sailing_records r JOIN sailings s ON s.id = r.sailing_id
            WHERE s.origin = ? AND s.service_date = ? AND s.depart_hhmm = ?""",
        (origin, service_date.isoformat(), hhmm),
    ).fetchone()
    return row


# ---- Storage and validation -----------------------------------------------------------


def test_a_report_is_stored_against_the_scheduled_sailing(conn, config):
    board_says_departed(conn, config, "12:42")
    file_report(conn, config, joined=time(11, 55), deck_fullness="full")

    rows = fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30")

    assert len(rows) == 1
    assert rows[0]["boarded"] == 1
    assert rows[0]["deck_fullness"] == "full"
    # Stored in UTC like everything else, but reasoned about in local time.
    assert rows[0]["joined_queue_at"].endswith("Z")


def test_only_getting_on_is_required(conn, config):
    """A half-remembered trip is still worth more than no record at all."""
    file_report(conn, config, boarded=False)
    rows = fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30")
    assert rows[0]["joined_queue_at"] is None
    assert rows[0]["deck_fullness"] is None


def test_a_sailing_that_has_not_departed_cannot_be_reported(conn, config):
    future = NOW.astimezone(config.tz).date() + timedelta(days=3)
    with pytest.raises(ReportError, match="has not departed"):
        file_report(conn, config, service_date=future)


def test_a_sailing_not_in_the_timetable_is_refused(conn, config):
    with pytest.raises(ReportError, match="no 11:11 sailing"):
        file_report(conn, config, depart_hhmm="11:11")


def test_joining_after_the_ferry_left_is_refused(conn, config):
    board_says_departed(conn, config, "12:35")
    with pytest.raises(ReportError, match="after the ferry left"):
        file_report(conn, config, joined=time(13, 10))


def test_joining_after_the_scheduled_time_is_fine_when_the_sailing_ran_late(conn, config):
    """The reason the departure time is still needed at all. Someone who joined at 12:50 for
    a 12:30 that left at 13:05 is telling the truth, and asking them for the time by hand was
    the previous way of knowing it."""
    board_says_departed(conn, config, "13:05")
    file_report(conn, config, joined=time(12, 50))
    assert len(fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30")) == 1


def test_without_a_board_departure_the_scheduled_time_bounds_the_report(conn, config):
    """No board reading yet — fall back to the timetable rather than accepting anything."""
    with pytest.raises(ReportError, match="after the ferry left"):
        file_report(conn, config, joined=time(13, 10))


def test_an_unknown_deck_fullness_is_refused(conn, config):
    with pytest.raises(ReportError, match="deck fullness"):
        file_report(conn, config, deck_fullness="quite-full-ish")


def test_reports_are_capped_per_sailing(conn, config, monkeypatch):
    monkeypatch.setattr("ferrycast.reports.MAX_REPORTS_PER_SAILING", 2)
    file_report(conn, config)
    file_report(conn, config)
    with pytest.raises(ReportError, match="already has 2 reports"):
        file_report(conn, config)


def test_clock_parsing_tolerates_seconds(conn, config):
    assert parse_clock("11:55:00") == time(11, 55)
    assert parse_clock("") is None
    with pytest.raises(ReportError):
        parse_clock("noon")


# ---- What a report is allowed to conclude ---------------------------------------------


def test_one_person_left_behind_outweighs_any_number_who_got_on():
    """Several people boarding is not evidence that nobody was turned away after them."""
    rows = [{"boarded": 1}, {"boarded": 1}, {"boarded": 0}]
    assert outcome_from_reports(rows) == "filled"
    assert outcome_from_reports([{"boarded": 1}, {"boarded": 1}]) == "boarded"
    assert outcome_from_reports([]) is None


def test_a_report_reaches_the_sailing_record_immediately(conn, config):
    """Aggregation is nightly; a submitter must not have to wait for it to see their report
    counted, or the page contradicts what they just said."""
    file_report(conn, config, boarded=False)

    record = outcome_of(conn, SAILED, "12:30")
    assert record["outcome"] == "filled"
    assert record["method"] == "report"


def test_a_report_outranks_the_deck_space_feed(conn, config):
    """Deck space describes space aboard the vessel; it cannot see the approach road. A
    person standing on that road can."""
    departure = combine_local(SAILED, parse_hhmm("12:30"), config.tz)
    for minutes in (60, 45, 30, 15):
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    percent_available, fetch_status)
               VALUES (?, 'SLT', ?, ?, '12:30', 30, 'ok')""",
            (
                config.route.id,
                iso(departure - timedelta(minutes=minutes)),
                SAILED.isoformat(),
            ),
        )
    conn.commit()
    aggregate_day(conn, config, SAILED)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "boarded"

    file_report(conn, config, boarded=False, joined=time(11, 50))

    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "filled"


def test_a_report_never_sets_the_arrive_before_time(conn, config):
    """Someone who joined at 11:50 and did not get on proves the cutoff was *earlier* than
    11:50. Recording 11:50 as the moment it filled would tell the next traveller they can
    turn up later than they really can — the one direction this app must not be wrong in."""
    file_report(conn, config, boarded=False, joined=time(11, 50))

    assert outcome_of(conn, SAILED, "12:30")["filled_at"] is None


def test_deck_space_still_supplies_the_fill_time_under_a_report(conn, config):
    """The bound a report gives up is not lost — the feed keeps that job when it saw it."""
    departure = combine_local(SAILED, parse_hhmm("12:30"), config.tz)
    for minutes, available in [(60, 20), (40, 0), (20, 0)]:
        conn.execute(
            """INSERT INTO deck_space
                   (route, terminal, observed_at, service_date, sailing_hhmm,
                    percent_available, fetch_status)
               VALUES (?, 'SLT', ?, ?, '12:30', ?, 'ok')""",
            (
                config.route.id,
                iso(departure - timedelta(minutes=minutes)),
                SAILED.isoformat(),
                available,
            ),
        )
    conn.commit()

    file_report(conn, config, boarded=False)

    record = outcome_of(conn, SAILED, "12:30")
    assert record["method"] == "report"
    assert record["filled_at"] == iso(departure - timedelta(minutes=40))


def test_reports_survive_a_later_aggregation_run(conn, config):
    """The nightly job re-derives every record, so it has to keep consulting reports."""
    file_report(conn, config, boarded=False)
    aggregate_day(conn, config, SAILED)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "filled"


def test_the_evidence_note_names_the_reports(conn, config):
    from ferrycast.query import query_distribution

    file_report(conn, config, boarded=False)

    result = query_distribution(
        conn, config, origin="SLT", target_date=date(2026, 7, 31), depart_hhmm="12:30"
    )
    assert result.evidence == "report"
    assert "in the line" in result.evidence_note


# ---- The bounds shown on the page ------------------------------------------------------


def test_the_two_bounds_are_reported_the_honest_way_round(conn, config):
    file_report(conn, config, boarded=True, joined=time(11, 5))
    file_report(conn, config, boarded=False, joined=time(11, 50))
    file_report(conn, config, boarded=False, joined=time(12, 10))

    departure = combine_local(SAILED, parse_hhmm("12:30"), config.tz)
    summary = describe_reports(
        config,
        fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30"),
        departure,
    )

    assert summary["boarded"] == 1
    assert summary["left_behind"] == 2
    # The earliest person turned away is the tightest bound on the cutoff...
    assert summary["cutoff_before"] == "11:50"
    # ...and the latest person who made it is the tightest bound the other way.
    assert summary["still_room_at"] == "11:05"


def test_lateness_is_measured_against_the_scheduled_departure(conn, config):
    board_says_departed(conn, config, "12:47")
    file_report(conn, config)
    departure = combine_local(SAILED, parse_hhmm("12:30"), config.tz)

    summary = describe_reports(
        config,
        fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30"),
        departure,
    )
    assert summary["typical_minutes_late"] == 17


# ---- The page and the API --------------------------------------------------------------


def _form(**overrides) -> dict:
    body = {
        "origin": "SLT",
        "service_date": SAILED.isoformat(),
        "time": "12:30",
        "boarded": "yes",
        "joined": "11:55",
        "deck_fullness": "nearly_full",
    }
    body.update(overrides)
    return body


def test_the_form_is_offered_for_a_sailing_that_has_gone(client):
    body = client.get(f"/?origin=SLT&service_date={SAILED}&time=12:30").text
    assert "Add what happened" in body
    assert "Did you get on?" in body


def test_the_form_is_not_offered_for_a_sailing_that_has_not(client):
    """You cannot report on a crossing you have not made."""
    ahead = (NOW.astimezone(client.app.state.config.tz).date() + timedelta(days=2)).isoformat()
    body = client.get(f"/?origin=SLT&service_date={ahead}&time=12:30").text
    assert "Add what happened" not in body
    assert "hasn't gone yet" in body


def test_posting_a_report_redirects_back_to_the_sailing(client):
    response = client.post("/report", data=_form(), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/?origin=SLT")
    assert "reported=1" in response.headers["location"]


def test_a_submitted_report_shows_on_the_page(client):
    client.post("/report", data=_form(boarded="no", joined="11:40"))

    body = client.get(f"/?origin=SLT&service_date={SAILED}&time=12:30").text

    assert "1 report" in body
    assert "1 left behind" in body
    assert "0 got on" not in body
    assert "joined the line at <strong>11:40</strong>" in body


def test_a_rejected_report_says_why_and_keeps_what_was_typed(client):
    response = client.post("/report", data=_form(joined="12:50"))

    assert response.status_code == 400
    assert "Not recorded: you cannot have joined the line after the ferry left" in response.text
    # The times come back with the form rather than being silently dropped.
    assert 'name="joined" value="12:50"' in response.text


def test_a_report_with_no_answer_is_refused(client):
    response = client.post("/report", data=_form(boarded=""))
    assert response.status_code == 400
    assert "say whether you got on" in response.text


def test_the_api_lists_a_sailings_reports(client, conn, config):
    board_says_departed(conn, config, "12:35")
    client.post("/report", data=_form())

    payload = client.get(
        "/api/reports", params={"origin": "SLT", "service_date": SAILED.isoformat(), "time": "12:30"}
    ).json()

    assert payload["n"] == 1
    assert payload["entries"][0] == {
        "boarded": True,
        "joined": "11:55",
        "departed": "12:35",
        "minutes_late": 5,
        "fullness": "Nearly full",
        "submitted": payload["entries"][0]["submitted"],
    }


def test_the_api_accepts_a_report(client):
    response = client.post(
        "/api/report",
        params={
            "origin": "SLT",
            "service_date": SAILED.isoformat(),
            "time": "12:30",
            "boarded": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "filled"


def test_the_api_rejects_a_bad_report_with_a_reason(client):
    response = client.post(
        "/api/report",
        params={
            "origin": "SLT",
            "service_date": SAILED.isoformat(),
            "time": "03:00",
            "boarded": True,
        },
    )
    assert response.status_code == 400
    assert "timetable" in response.json()["detail"]


def test_reports_export(client):
    client.post("/report", data=_form())
    response = client.get("/export/reports.csv")
    assert response.status_code == 200
    assert "nearly_full" in response.text


# ---- How long they waited ---------------------------------------------------------------
#
# Without this question a report could only ever say "did not get on", stored as `filled`.
# The distribution could never contain waited_1 or waited_2plus from the one source that
# actually knows — so a page showed "1 filled up" while the headline said nobody waited.


def test_waiting_one_sailing_is_recorded_as_such(conn, config):
    file_report(conn, config, boarded=False, sailings_waited=1)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "waited_1"


def test_waiting_two_or_more_is_recorded_as_such(conn, config):
    file_report(conn, config, boarded=False, sailings_waited=2)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "waited_2plus"


def test_not_answering_stays_filled(conn, config):
    """"Did not get on" is certain; "waited exactly one sailing" is not. An unanswered
    follow-up must not be turned into a number nobody supplied."""
    file_report(conn, config, boarded=False)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "filled"


def test_the_longest_wait_reported_wins(conn, config):
    """Somebody who waited two sailings was not on the one the other person caught."""
    file_report(conn, config, boarded=False, sailings_waited=1)
    file_report(conn, config, boarded=False, sailings_waited=2)
    assert outcome_of(conn, SAILED, "12:30")["outcome"] == "waited_2plus"


def test_you_cannot_have_waited_for_a_sailing_you_were_on(conn, config):
    with pytest.raises(ReportError, match="you were on"):
        file_report(conn, config, boarded=True, sailings_waited=1)


def test_a_nonsense_wait_is_refused(conn, config):
    with pytest.raises(ReportError, match="one sailing or two"):
        file_report(conn, config, boarded=False, sailings_waited=7)


# ---- The departure time, back but pre-filled --------------------------------------------


def test_a_typed_departure_still_wins_for_a_past_date(conn, config):
    """The board carries today's sailings only. Report on last Friday and there is nothing
    to read — only the person who was there can say."""
    file_report(conn, config, joined=time(12, 50), departed=time(13, 5))
    rows = fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30")
    assert rows[0]["departed_at"] is not None


def test_the_board_is_used_when_nothing_is_typed(conn, config):
    board_says_departed(conn, config, "12:42")
    file_report(conn, config)
    rows = fetch_reports(conn, config.route.id, "SLT", SAILED.isoformat(), "12:30")
    assert rows[0]["departed_at"].endswith("Z")


def test_an_am_pm_flip_in_a_typed_departure_is_still_caught(conn, config):
    """The guard that came back with the field: a 12:30 sailing reported as leaving 00:30."""
    with pytest.raises(ReportError, match="too far from the scheduled"):
        file_report(conn, config, departed=time(0, 30))
