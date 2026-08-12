"""The ECCC marine forecast: what it says, and what this route has done in weather like it.

Two things carry the weight here. The forecast is quoted rather than paraphrased, because
it is the one condition on the page FerryCast does not observe for itself. And the claim
the design asked for — "no sailing cancelled in these conditions" — is an *absence*, so it
is gated: an absence over four days is not evidence of anything.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from ferrycast.config import ConfigError, load_config
from ferrycast.marine import (
    MIN_BAND_DAYS,
    Forecast,
    ForecastDay,
    MarineError,
    band_for,
    band_record,
    newest_file,
    parse_forecast,
    store_forecast,
    summary,
    wind_speed,
)
from ferrycast.timeutil import combine_local, iso, parse_hhmm

from .test_query import seed_record

# A real document from the feed, trimmed to the elements this module reads. The wind
# sentences, the two sub-locations and the compound "Tonight and Wednesday" period are all
# verbatim — this route crosses the north half, and taking the first location would answer
# about the wrong end of a 200 km waterway.
REAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marineData>
  <dateTime name="xmlCreation" zone="UTC" UTCOffset="0"><timeStamp>202608120421</timeStamp></dateTime>
  <area countryCode="CA" region="Pacific Coast" subRegion="Georgia Basin">Strait of Georgia</area>
  <warnings/>
  <regularForecast>
    <dateTime name="Issued" zone="UTC" UTCOffset="0"><timeStamp>202608120430</timeStamp></dateTime>
    <dateTime name="Issued" zone="PDT" UTCOffset="-7"><timeStamp>202608112130</timeStamp></dateTime>
    <location name="Strait of Georgia - north of Nanaimo">
      <weatherCondition>
        <periodOfCoverage>Tonight and Wednesday.</periodOfCoverage>
        <wind>Wind light increasing to northwest 15 to 20 knots late this evening.</wind>
        <weatherVisibility/>
      </weatherCondition>
    </location>
    <location name="Strait of Georgia - south of Nanaimo">
      <weatherCondition>
        <periodOfCoverage>Tonight and Wednesday.</periodOfCoverage>
        <wind>Wind light increasing to northwest 10 to 15 knots late this evening.</wind>
      </weatherCondition>
    </location>
  </regularForecast>
  <extendedForecast>
    <dateTime name="Issued" zone="UTC" UTCOffset="0"><timeStamp>202608112300</timeStamp></dateTime>
    <location>
      <weatherCondition>
        <forecastPeriod name="Thursday">Wind southeast 5 to 15 knots.</forecastPeriod>
        <forecastPeriod name="Friday">Wind southeast 5 to 15 knots.</forecastPeriod>
        <forecastPeriod name="Saturday">Wind northwest 5 to 10 knots.</forecastPeriod>
      </weatherCondition>
    </location>
  </extendedForecast>
  <waveForecast/>
</marineData>
"""

NORTH = "Strait of Georgia - north of Nanaimo"


@pytest.fixture
def marine_config(tmp_path):
    """The stock test config with a marine area attached."""
    from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(
            "[capture]",
            f'[route.marine]\nsite = "m0000028"\ndomain = "pacific"\nlocation = "{NORTH}"\n\n[capture]',
        )
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    return load_config(config_dir / "ferrycast.toml")


@pytest.fixture
def marine_conn(marine_config):
    from ferrycast.db import init_db

    conn = init_db(marine_config.db_path)
    yield conn
    conn.close()


# ---- Reading the feed --------------------------------------------------------------------


def test_the_forecast_is_split_into_the_dates_it_covers(marine_config):
    forecast = parse_forecast(REAL_XML, marine_config, "m0000028", NORTH)

    assert forecast.area == "Strait of Georgia"
    assert forecast.issued_at == datetime(2026, 8, 12, 4, 30, tzinfo=UTC)
    # Issued 21:30 PDT on the 11th: tonight and tomorrow, then three named weekdays.
    assert [day.service_date for day in forecast.days] == [
        "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15",
    ]
    assert [day.kind for day in forecast.days] == [
        "regular", "regular", "extended", "extended", "extended",
    ]


def test_the_named_sub_area_is_the_one_this_route_crosses(marine_config):
    """A marine area is often split, and the halves get different wind. Taking the first
    would quietly answer about the wrong end of the strait."""
    north = parse_forecast(REAL_XML, marine_config, "m0000028", NORTH)
    south = parse_forecast(REAL_XML, marine_config, "m0000028", "Strait of Georgia - south of Nanaimo")

    assert north.location == NORTH
    assert "15 to 20 knots" in north.days[0].wind
    assert "10 to 15 knots" in south.days[0].wind


def test_an_unmatched_location_falls_back_rather_than_going_blank(marine_config):
    """Some of a forecast beats none — but the row records which location it settled for,
    so the page can say which water it is talking about."""
    forecast = parse_forecast(REAL_XML, marine_config, "m0000028", "Nowhere In Particular")
    assert forecast.location == NORTH  # the first, and it says so


def test_the_forecast_text_is_quoted_not_paraphrased(marine_config):
    forecast = parse_forecast(REAL_XML, marine_config, "m0000028", NORTH)
    assert forecast.days[0].wind == (
        "Wind light increasing to northwest 15 to 20 knots late this evening."
    )
    assert forecast.days[0].period == "Tonight and Wednesday."


def test_a_compound_period_is_stored_whole_against_both_its_dates(marine_config):
    """"Tonight and Wednesday" is one sentence covering two dates. Splitting it in half
    would be writing forecast text ECCC did not write."""
    forecast = parse_forecast(REAL_XML, marine_config, "m0000028", NORTH)
    tonight, tomorrow = forecast.days[0], forecast.days[1]
    assert tonight.wind == tomorrow.wind
    assert tonight.period == tomorrow.period == "Tonight and Wednesday."


def test_a_document_with_a_doctype_is_refused(marine_config):
    """ElementTree expands internal entities, and this document arrives over the network.
    No genuine MSC file declares a DOCTYPE, so refusing one closes the hole for free."""
    hostile = '<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY a "aaaa">]>\n<marineData/>'
    with pytest.raises(MarineError, match="DOCTYPE"):
        parse_forecast(hostile, marine_config, "m0000028", NORTH)


def test_junk_is_an_error_not_a_traceback(marine_config):
    with pytest.raises(MarineError):
        parse_forecast("<marineData>", marine_config, "m0000028", NORTH)
    with pytest.raises(MarineError, match="no UTC issue time"):
        parse_forecast("<marineData><area>x</area></marineData>", marine_config, "m0000028", NORTH)


# ---- Warnings ----------------------------------------------------------------------------


def test_an_empty_warnings_element_means_no_warning(marine_config):
    assert parse_forecast(REAL_XML, marine_config, "m0000028", NORTH).warning is None


@pytest.mark.parametrize(
    "markup",
    [
        "<warnings><event name='gale warning'/></warnings>",
        "<warnings><location><warning>gale warning</warning></location></warnings>",
        "<warnings type='gale warning'/>",
        "<warnings>gale warning in effect</warnings>",
    ],
)
def test_a_warning_survives_whatever_shape_it_arrives_in(marine_config, markup):
    """There was no marine warning in force anywhere in Canada while this was written, and
    the schema the documents name has been 404 for some time — so the populated shape could
    not be observed. The one behaviour that must hold whatever it turns out to be: a real
    gale warning must never be read as an empty element."""
    forecast = parse_forecast(
        REAL_XML.replace("<warnings/>", markup), marine_config, "m0000028", NORTH
    )
    assert forecast.warning is not None
    assert "gale" in forecast.warning.lower()


# ---- Wind bands --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "knots", "band"),
    [
        ("Wind southeast 5 to 15 knots.", 15, "light"),
        ("Wind northwest 15 to 20 knots.", 20, "strong"),
        ("Wind southeast 35 to 45 knots.", 45, "gale"),
        ("Wind 50 knots.", 50, "storm"),
        # The strongest wind named wins: it is the one that decides whether it sails.
        ("Wind northwest 15 to 20 except 30 near Nanaimo.", 30, "strong"),
        # A complete forecast with no number in it at all.
        ("Wind light.", 10, "light"),
        ("Gale warning in effect.", 34, "gale"),
        (None, None, "unknown"),
        ("Wind variable.", None, "unknown"),
    ],
)
def test_the_band_is_read_off_the_strongest_wind_named(text, knots, band):
    assert wind_speed(text) == knots
    assert band_for(wind_speed(text)) == band


def test_an_unreadable_forecast_is_unknown_rather_than_calm(marine_config):
    """`unknown` and not `light`. The band gates a published claim about cancellations, and
    defaulting an unreadable forecast to calm would attach that claim to weather nobody
    has actually checked."""
    assert band_for(None) == "unknown"


# ---- Finding the newest file -------------------------------------------------------------

LISTING = """
<a href="20260812T042128.170Z_MSC_MarineWeather_m0000028_en.xml">x</a>
<a href="20260812T101500.000Z_MSC_MarineWeather_m0000028_en.xml">x</a>
<a href="20260812T101500.000Z_MSC_MarineWeather_m0000028_fr.xml">x</a>
<a href="20260812T230000.000Z_MSC_MarineWeather_m0000064_en.xml">x</a>
"""


def test_the_newest_english_file_for_the_site_wins():
    """Filenames begin with a sortable UTC stamp, so newest is last by name."""
    assert newest_file(LISTING, "m0000028") == (
        "20260812T101500.000Z_MSC_MarineWeather_m0000028_en.xml"
    )
    assert newest_file(LISTING, "m0000009") is None


# ---- The cancellation record -------------------------------------------------------------


def seed_forecast(conn, config, day: date, band: str, *, kt: int = 20, warning=None):
    conn.execute(
        """INSERT OR REPLACE INTO marine_forecast
               (route, site, service_date, issued_at, fetched_at, area, location, kind,
                period, wind, warning, wind_max_kt, band, fetch_status)
           VALUES (?, 'm0000028', ?, ?, ?, 'Strait of Georgia', ?, 'regular',
                   'Today.', ?, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            day.isoformat(),
            iso(combine_local(day, parse_hhmm("04:30"), config.tz)),
            iso(combine_local(day, parse_hhmm("05:00"), config.tz)),
            NORTH,
            f"Wind {kt} knots.",
            warning,
            kt,
            band,
        ),
    )
    conn.commit()


def test_no_claim_until_enough_weather_has_been_watched(marine_conn, marine_config):
    """The claim is an absence. Over four days an absence is not evidence of anything."""
    for back in range(1, 5):
        day = date(2026, 8, 11) - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "light")
        seed_record(marine_conn, marine_config, day, "12:30", "boarded")

    assert band_record(marine_conn, marine_config, band="light", before=date(2026, 8, 11)) is None


def test_the_absence_is_reported_once_it_is_well_founded(marine_conn, marine_config):
    for back in range(1, MIN_BAND_DAYS + 5):
        day = date(2026, 8, 11) - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "light")
        seed_record(marine_conn, marine_config, day, "12:30", "boarded")

    record = band_record(marine_conn, marine_config, band="light", before=date(2026, 8, 11))

    assert record["cancelled"] == 0
    assert record["days"] >= MIN_BAND_DAYS
    assert record["band_words"] == "light winds"
    assert record["since"] is not None


def test_a_cancellation_is_reported_from_the_first_one(marine_conn, marine_config):
    """The threshold guards the absence, not the finding. Holding back a cancellation that
    did happen, to satisfy a sample size, would suppress the more useful evidence."""
    for back in range(1, 4):
        day = date(2026, 8, 11) - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "gale", kt=40)
        seed_record(marine_conn, marine_config, day, "12:30", "cancelled")

    record = band_record(marine_conn, marine_config, band="gale", before=date(2026, 8, 11))

    assert record is not None
    assert record["cancelled"] == 3
    assert record["days"] == 3  # well under the threshold, and reported anyway


def test_forecasts_with_no_sailings_behind_them_claim_nothing(marine_conn, marine_config):
    """The most confident-sounding thing this page could say off an empty table is that
    nothing has ever been cancelled."""
    for back in range(1, MIN_BAND_DAYS + 5):
        seed_forecast(marine_conn, marine_config, date(2026, 8, 11) - timedelta(days=back), "light")

    assert band_record(marine_conn, marine_config, band="light", before=date(2026, 8, 11)) is None


def test_an_unreadable_band_pools_with_nothing(marine_conn, marine_config):
    for back in range(1, MIN_BAND_DAYS + 5):
        day = date(2026, 8, 11) - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "unknown")
        seed_record(marine_conn, marine_config, day, "12:30", "boarded")

    assert band_record(marine_conn, marine_config, band="unknown", before=date(2026, 8, 11)) is None


def test_the_record_never_counts_the_day_being_asked_about(marine_conn, marine_config):
    """A board for a future date must not count that date's own sailings, and today's board
    must not quote a cancellation that has not happened yet."""
    target = date(2026, 8, 11)
    for back in range(1, MIN_BAND_DAYS + 5):
        day = target - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "light")
        seed_record(marine_conn, marine_config, day, "12:30", "boarded")
    seed_forecast(marine_conn, marine_config, target, "light")
    seed_record(marine_conn, marine_config, target, "12:30", "cancelled")

    record = band_record(marine_conn, marine_config, band="light", before=target)
    assert record["cancelled"] == 0


# ---- The summary the page reads ----------------------------------------------------------


def test_the_newest_issue_wins(marine_conn, marine_config):
    day = date(2026, 8, 20)
    seed_forecast(marine_conn, marine_config, day, "light", kt=10)
    marine_conn.execute(
        """INSERT OR REPLACE INTO marine_forecast
               (route, site, service_date, issued_at, fetched_at, kind, wind,
                wind_max_kt, band, fetch_status)
           VALUES (?, 'm0000028', ?, '2026-08-20T22:00:00Z', '2026-08-20T22:00:00Z',
                   'regular', 'Wind 40 knots.', 40, 'gale', 'ok')""",
        (marine_config.route.id, day.isoformat()),
    )
    marine_conn.commit()

    assert summary(marine_conn, marine_config, service_date=day).band == "gale"


def test_a_failed_fetch_is_never_served_as_a_forecast(marine_conn, marine_config):
    day = date(2026, 8, 21)
    marine_conn.execute(
        """INSERT INTO marine_forecast
               (route, site, service_date, issued_at, fetched_at, kind, fetch_status, error)
           VALUES (?, 'm0000028', ?, '2026-08-21T04:00:00Z', '2026-08-21T04:00:00Z',
                   'regular', 'error', 'nothing published')""",
        (marine_config.route.id, day.isoformat()),
    )
    marine_conn.commit()

    assert summary(marine_conn, marine_config, service_date=day) is None


def test_the_age_of_the_forecast_travels_with_it(marine_conn, marine_config):
    day = date(2026, 8, 22)
    seed_forecast(marine_conn, marine_config, day, "light")
    issued = combine_local(day, parse_hhmm("04:30"), marine_config.tz)

    fresh = summary(marine_conn, marine_config, service_date=day, now=issued + timedelta(minutes=20))
    old = summary(marine_conn, marine_config, service_date=day, now=issued + timedelta(hours=30))

    assert fresh.issued_label == "issued in the last hour" and not fresh.stale
    assert old.stale and "30 h ago" in old.issued_label


def test_no_marine_area_configured_means_no_forecast(conn, config):
    """The feature is optional. A route whose operator never cancels for weather should not
    grow a marine panel, and an install that predates the table must not 500."""
    assert config.route.marine is None
    assert summary(conn, config, service_date=date(2026, 8, 11)) is None


def test_a_database_without_the_table_answers_rather_than_raises(marine_config, tmp_path):
    """`ferrycast serve` on its own does not create tables. An install upgraded without
    running `init` should lose the forecast, not the page."""
    from ferrycast.db import connect

    conn = connect(tmp_path / "bare.db")
    conn.execute("CREATE TABLE sailings (id INTEGER PRIMARY KEY)")
    assert summary(conn, marine_config, service_date=date(2026, 8, 11)) is None


# ---- Storage ------------------------------------------------------------------------------


def test_storing_the_same_issue_twice_adds_nothing(marine_conn, marine_config):
    forecast = parse_forecast(REAL_XML, marine_config, "m0000028", NORTH)
    at = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)

    first = store_forecast(marine_conn, marine_config, forecast, fetched_at=at)
    second = store_forecast(marine_conn, marine_config, forecast, fetched_at=at)

    assert first == 5
    assert second == 0


def test_an_amended_forecast_is_kept_alongside_the_one_it_replaces(marine_conn, marine_config):
    """A forecast is amended through the day, and the cancellation claim has to be
    checkable against what was being said at the time rather than against the last word."""
    at = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)
    store_forecast(
        marine_conn, marine_config, parse_forecast(REAL_XML, marine_config, "m0000028", NORTH),
        fetched_at=at,
    )
    amended = Forecast(
        site="m0000028", area="Strait of Georgia", location=NORTH,
        issued_at=datetime(2026, 8, 12, 10, 30, tzinfo=UTC), warning="Gale warning in effect",
        days=[ForecastDay("2026-08-12", "regular", "Today.", "Wind 40 knots.", None, 40, "gale")],
    )
    store_forecast(marine_conn, marine_config, amended, fetched_at=at)

    rows = marine_conn.execute(
        "SELECT band FROM marine_forecast WHERE service_date = '2026-08-12' ORDER BY issued_at"
    ).fetchall()
    assert [r["band"] for r in rows] == ["strong", "gale"]


# ---- The strip on the board ---------------------------------------------------------------


def body_of(text: str) -> str:
    """Collapse template whitespace so a sentence split across source lines still matches."""
    return " ".join(text.split())


@pytest.fixture
def marine_client(marine_conn, marine_config):
    from fastapi.testclient import TestClient

    from ferrycast.web.app import create_app

    with TestClient(create_app(str(marine_config.source_path))) as client:
        yield client


def test_the_forecast_reaches_the_board(marine_client, marine_conn, marine_config):
    seed_forecast(marine_conn, marine_config, date(2026, 8, 20), "light", kt=15)

    body = marine_client.get("/?origin=SLT&service_date=2026-08-20").text

    assert "Marine forecast · ECCC" in body
    assert "Wind 15 knots." in body                       # quoted, not paraphrased
    assert "Strait of Georgia - north of Nanaimo" in body  # which water it is about


def test_a_warning_is_on_the_board_on_any_date_including_today(
    marine_client, marine_conn, marine_config
):
    """Whatever the design does with the planning strip, a gale warning is not something to
    hold back until somebody happens to be looking at a future date."""
    from ferrycast.timeutil import local, now_utc

    today = local(now_utc(), marine_config.tz).date()
    seed_forecast(marine_conn, marine_config, today, "gale", kt=40, warning="Gale warning in effect")

    body = marine_client.get(f"/?origin=SLT&service_date={today}").text
    assert "Gale warning in effect" in body


def test_no_forecast_means_no_panel_rather_than_an_empty_one(marine_client):
    body = marine_client.get("/?origin=SLT&service_date=2026-08-20").text
    assert "Marine forecast" not in body


def test_the_cancellation_claim_only_appears_once_it_is_earned(
    marine_client, marine_conn, marine_config
):
    target = date(2026, 8, 20)
    seed_forecast(marine_conn, marine_config, target, "light", kt=15)

    thin = marine_client.get(f"/?origin=SLT&service_date={target}").text
    assert "was cancelled on the" not in thin

    for back in range(1, MIN_BAND_DAYS + 5):
        day = target - timedelta(days=back)
        seed_forecast(marine_conn, marine_config, day, "light", kt=15)
        seed_record(marine_conn, marine_config, day, "12:30", "boarded")

    earned = body_of(marine_client.get(f"/?origin=SLT&service_date={target}").text)
    assert "No sailing on this route was cancelled" in earned
    assert "cancellation counts are FerryCast's own" in earned


def test_the_strip_names_the_shape_of_the_day(marine_client, marine_conn, marine_config):
    """Ten percentages are not a plan. "before 12:30" is."""
    from .test_query import fridays

    for day in fridays(6):
        seed_record(marine_conn, marine_config, day, "08:30", "boarded")
        seed_record(marine_conn, marine_config, day, "12:30", "waited_2plus")
        seed_record(marine_conn, marine_config, day, "16:30", "waited_2plus")

    body = body_of(marine_client.get("/?origin=SLT&service_date=2026-08-21").text)

    assert "Easiest sailings" in body
    assert "before 12:30" in body
    assert "Hardest stretch" in body
    assert "12:30–16:30" in body


def test_no_shape_is_claimed_from_a_day_nobody_has_data_for(marine_client, marine_conn, marine_config):
    """A "hardest stretch" drawn from one known sailing out of three is a claim about the
    two nobody has data for."""
    from .test_query import fridays

    for day in fridays(6):
        seed_record(marine_conn, marine_config, day, "12:30", "waited_2plus")

    body = marine_client.get("/?origin=SLT&service_date=2026-08-21").text
    assert "Hardest stretch" not in body
    assert "Easiest sailings" not in body


def test_a_marine_section_without_a_site_is_rejected(tmp_path):
    from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace("[capture]", '[route.marine]\ndomain = "pacific"\n\n[capture]')
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    with pytest.raises(ConfigError, match="site"):
        load_config(config_dir / "ferrycast.toml")
