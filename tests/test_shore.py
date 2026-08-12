"""The weather ashore: what ECCC says it will be doing at the terminal.

The rule this file is really testing is separation. The shore forecast and the marine one
are different products about different places, and the value of having both depends
entirely on never letting one speak for the other — not in the table, not in the sheet, and
not in the attribution.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from ferrycast.config import ConfigError, load_config
from ferrycast.shore import (
    ShoreError,
    ShoreForecast,
    newest_file,
    parse_forecast,
    store_forecast,
    summary,
)
from ferrycast.timeutil import iso

from .test_marine import body_of

# A real document from the feed, trimmed to the elements this module reads. The conditions,
# the temperatures and the period labels are verbatim, including the 14-digit issue stamp —
# the marine feed writes 12, and reading this one as 12 would put the forecast in the year
# 2026 at hour 00 of a different day.
REAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<siteData>
  <license>https://dd.weather.gc.ca/doc/LICENCE_GENERAL.txt</license>
  <dateTime name="xmlCreation" zone="UTC" UTCOffset="0"><timeStamp>20260812140043</timeStamp></dateTime>
  <location>
    <name code="s0000634" lat="49.83N" lon="124.52W">Powell River</name>
    <region>Sunshine Coast - Saltery Bay to Powell River</region>
  </location>
  <warnings/>
  <currentConditions>
    <condition>Mostly Cloudy</condition>
    <temperature unitType="metric" units="C">16.3</temperature>
  </currentConditions>
  <forecastGroup>
    <dateTime name="forecastIssue" zone="UTC" UTCOffset="0"><timeStamp>20260812120000</timeStamp></dateTime>
    <dateTime name="forecastIssue" zone="PDT" UTCOffset="-7"><timeStamp>20260812050000</timeStamp></dateTime>
    <forecast>
      <period textForecastName="Today">Wednesday</period>
      <textSummary>Mainly cloudy. 30 percent chance of showers this morning. High 25.</textSummary>
      <abbreviatedForecast>
        <iconCode format="gif">06</iconCode>
        <pop units="%">30</pop>
        <textSummary>Chance of showers</textSummary>
      </abbreviatedForecast>
      <temperatures>
        <textSummary>High 25.</textSummary>
        <temperature unitType="metric" units="C" class="high">25</temperature>
      </temperatures>
    </forecast>
    <forecast>
      <period textForecastName="Tonight">Wednesday night</period>
      <textSummary>Partly cloudy. Low 13.</textSummary>
      <abbreviatedForecast>
        <iconCode format="gif">32</iconCode>
        <pop units="%"></pop>
        <textSummary>Partly cloudy</textSummary>
      </abbreviatedForecast>
      <temperatures>
        <temperature unitType="metric" units="C" class="low">13</temperature>
      </temperatures>
    </forecast>
    <forecast>
      <period textForecastName="Thursday">Thursday</period>
      <textSummary>Sunny. High 27.</textSummary>
      <abbreviatedForecast><pop units="%">0</pop><textSummary>Sunny</textSummary></abbreviatedForecast>
      <temperatures><temperature unitType="metric" units="C" class="high">27</temperature></temperatures>
    </forecast>
    <forecast>
      <period textForecastName="Thursday night">Thursday night</period>
      <textSummary>Clear. Low 12.</textSummary>
      <abbreviatedForecast><textSummary>Clear</textSummary></abbreviatedForecast>
      <temperatures><temperature unitType="metric" units="C" class="low">12</temperature></temperatures>
    </forecast>
  </forecastGroup>
</siteData>
"""

STATION = "s0000634"


@pytest.fixture
def shore_config(tmp_path):
    """The stock test config with a weather station on each terminal, and a marine area.

    Both feeds, because both is the ordinary install: the page has to rank two forecasts
    against one line, which it cannot be shown doing with only one of them on file.
    """
    from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE
    from .test_marine import NORTH

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(
            "[capture]",
            f'[route.marine]\nsite = "m0000028"\ndomain = "pacific"\nlocation = "{NORTH}"\n\n[capture]',
        ).replace(
            'deck_space_url = "https://example.invalid/slt"',
            'deck_space_url = "https://example.invalid/slt"\n'
            f'  weather_site = "{STATION}"\n  weather_province = "BC"',
        ).replace(
            'deck_space_url = "https://example.invalid/erl"',
            'deck_space_url = "https://example.invalid/erl"\n'
            '  weather_site = "s0000316"\n  weather_province = "BC"',
        )
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    return load_config(config_dir / "ferrycast.toml")


@pytest.fixture
def shore_conn(shore_config):
    from ferrycast.db import init_db

    conn = init_db(shore_config.db_path)
    yield conn
    conn.close()


@pytest.fixture
def shore_client(shore_conn, shore_config):
    from fastapi.testclient import TestClient

    from ferrycast.web.app import create_app

    with TestClient(create_app(str(shore_config.source_path))) as client:
        yield client


# ---- Reading the feed --------------------------------------------------------------------


def test_the_half_days_land_on_the_dates_they_cover():
    forecast = parse_forecast(REAL_XML, STATION)

    assert forecast.station == "Powell River"
    assert forecast.region == "Sunshine Coast - Saltery Bay to Powell River"
    # Issued 05:00 PDT on the 12th: today and tonight, then Thursday and its night.
    assert [(p.service_date, p.kind) for p in forecast.periods] == [
        ("2026-08-12", "day"),
        ("2026-08-12", "night"),
        ("2026-08-13", "day"),
        ("2026-08-13", "night"),
    ]


def test_the_issue_time_is_the_forecasts_own_not_the_files():
    """The file is rewritten every hour with the same forecast inside it. Reading the
    creation stamp as an issue would make a six-hour-old forecast look minutes old."""
    forecast = parse_forecast(REAL_XML, STATION)
    assert forecast.issued_at == datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def test_the_forecast_text_is_quoted_not_paraphrased():
    today = parse_forecast(REAL_XML, STATION).periods[0]

    assert today.condition == "Chance of showers"
    assert today.summary == (
        "Mainly cloudy. 30 percent chance of showers this morning. High 25."
    )
    assert today.period == "Today"
    assert today.pop == 30
    assert today.temp_high == 25 and today.temp_low is None


def test_a_night_period_carries_the_low_and_no_high():
    tonight = parse_forecast(REAL_XML, STATION).periods[1]
    assert tonight.temp_low == 13 and tonight.temp_high is None
    assert tonight.pop is None  # an empty element is not a zero


def test_a_weekday_named_on_its_own_weekday_is_the_one_a_week_out():
    """A document issued on a Thursday that says "Thursday" means next Thursday. Reading it
    as today would overwrite today's forecast with one written for a week away."""
    xml = REAL_XML.replace("<timeStamp>20260812050000</timeStamp>", "<timeStamp>20260813050000</timeStamp>")
    dates = {p.period: p.service_date for p in parse_forecast(xml, STATION).periods}
    assert dates["Thursday"] == "2026-08-20"  # issued Thursday the 13th


def test_an_unrecognised_period_is_skipped_rather_than_guessed_at():
    """Attaching a forecast to the wrong date is worse than the date having none."""
    xml = REAL_XML.replace('textForecastName="Thursday"', 'textForecastName="Next week sometime"')
    periods = parse_forecast(xml, STATION).periods
    assert all(p.period != "Next week sometime" for p in periods)
    assert len(periods) == 3


def test_a_document_with_a_doctype_is_refused():
    hostile = '<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY a "aaaa">]>\n<siteData/>'
    with pytest.raises(ShoreError, match="DOCTYPE"):
        parse_forecast(hostile, STATION)


def test_junk_is_an_error_not_a_traceback():
    with pytest.raises(ShoreError):
        parse_forecast("<siteData>", STATION)
    with pytest.raises(ShoreError, match="no UTC issue time"):
        parse_forecast("<siteData><location/></siteData>", STATION)


# ---- Finding the newest file -------------------------------------------------------------

LISTING = """
<a href="20260812T140043.071Z_MSC_CitypageWeather_s0000634_en.xml">x</a>
<a href="20260812T180043.071Z_MSC_CitypageWeather_s0000634_en.xml">x</a>
<a href="20260812T180043.071Z_MSC_CitypageWeather_s0000634_fr.xml">x</a>
<a href="20260812T230000.000Z_MSC_CitypageWeather_s0000316_en.xml">x</a>
"""


def test_the_newest_english_file_for_the_station_wins():
    assert newest_file(LISTING, STATION) == (
        "20260812T180043.071Z_MSC_CitypageWeather_s0000634_en.xml"
    )
    assert newest_file(LISTING, "s0000999") is None


# ---- Storage and the summary the page reads ----------------------------------------------


def seed(conn, config, terminal, day: date, *, condition="Sunny", high=22, low=None,
         kind="day", issued=None, station="Powell River"):
    conn.execute(
        """INSERT OR REPLACE INTO shore_forecast
               (route, terminal, site, service_date, issued_at, fetched_at, station, region,
                kind, period, condition, pop, temp_high, temp_low, summary, fetch_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Sunshine Coast - Saltery Bay to Powell River',
                   ?, 'Today', ?, 20, ?, ?, ?, 'ok')""",
        (
            config.route.id,
            terminal,
            STATION,
            day.isoformat(),
            iso(issued or datetime(2026, 8, 12, 12, 0, tzinfo=UTC)),
            iso(issued or datetime(2026, 8, 12, 12, 5, tzinfo=UTC)),
            station,
            kind,
            condition,
            high,
            low,
            f"{condition}. High {high}." if high is not None else f"{condition}.",
        ),
    )
    conn.commit()


def test_storing_the_same_issue_twice_adds_nothing(shore_conn, shore_config):
    forecast = parse_forecast(REAL_XML, STATION)
    terminal = shore_config.route.terminal("SLT")
    at = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)

    first = store_forecast(shore_conn, shore_config, terminal, forecast, fetched_at=at)
    second = store_forecast(shore_conn, shore_config, terminal, forecast, fetched_at=at)

    assert first == 4
    assert second == 0


def test_an_amended_forecast_is_kept_alongside_the_one_it_replaces(shore_conn, shore_config):
    terminal = shore_config.route.terminal("SLT")
    at = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
    store_forecast(shore_conn, shore_config, terminal, parse_forecast(REAL_XML, STATION), fetched_at=at)
    amended = ShoreForecast(
        site=STATION,
        station="Powell River",
        region="Sunshine Coast - Gibsons to Earls Cove",
        issued_at=datetime(2026, 8, 12, 18, 0, tzinfo=UTC),
        periods=parse_forecast(REAL_XML.replace("Chance of showers", "Rain"), STATION).periods,
    )
    store_forecast(shore_conn, shore_config, terminal, amended, fetched_at=at)

    rows = shore_conn.execute(
        "SELECT condition FROM shore_forecast WHERE service_date = '2026-08-12' AND kind = 'day'"
        " ORDER BY issued_at"
    ).fetchall()
    assert [r["condition"] for r in rows] == ["Chance of showers", "Rain"]


def test_the_day_half_answers_for_the_date(shore_conn, shore_config):
    """Both halves are on file; a ferry queue is a daytime problem, and the day period is
    the one carrying the high."""
    day = date(2026, 8, 20)
    seed(shore_conn, shore_config, "SLT", day, condition="Sunny", high=24, kind="day")
    seed(shore_conn, shore_config, "SLT", day, condition="Clear", high=None, low=11, kind="night")

    answer = summary(shore_conn, shore_config, terminal="SLT", service_date=day)

    assert answer.condition == "Sunny"
    assert answer.temp_label == "High" and answer.temp_c == 24


def test_a_night_only_date_says_low_rather_than_inventing_a_high(shore_conn, shore_config):
    day = date(2026, 8, 21)
    seed(shore_conn, shore_config, "SLT", day, condition="Clear", high=None, low=11, kind="night")

    answer = summary(shore_conn, shore_config, terminal="SLT", service_date=day)

    assert answer.temp_label == "Low" and answer.temp_c == 11


def test_the_newest_issue_wins(shore_conn, shore_config):
    day = date(2026, 8, 20)
    seed(shore_conn, shore_config, "SLT", day, condition="Sunny")
    seed(
        shore_conn, shore_config, "SLT", day, condition="Rain",
        issued=datetime(2026, 8, 12, 23, 0, tzinfo=UTC),
    )

    assert summary(shore_conn, shore_config, terminal="SLT", service_date=day).condition == "Rain"


def test_each_terminal_answers_for_its_own_shore(shore_conn, shore_config):
    """The two ends are a strait apart. Answering for the wrong one is the whole reason this
    is keyed by terminal."""
    day = date(2026, 8, 20)
    seed(shore_conn, shore_config, "SLT", day, condition="Sunny", station="Powell River")
    seed(shore_conn, shore_config, "ERL", day, condition="Rain", station="Gibsons")

    assert summary(shore_conn, shore_config, terminal="SLT", service_date=day).condition == "Sunny"
    assert summary(shore_conn, shore_config, terminal="ERL", service_date=day).condition == "Rain"


def test_a_failed_fetch_is_never_served_as_a_forecast(shore_conn, shore_config):
    day = date(2026, 8, 21)
    shore_conn.execute(
        """INSERT INTO shore_forecast
               (route, terminal, site, service_date, issued_at, fetched_at, kind,
                fetch_status, error)
           VALUES (?, 'SLT', ?, ?, '2026-08-21T04:00:00Z', '2026-08-21T04:00:00Z',
                   'day', 'error', 'nothing published')""",
        (shore_config.route.id, STATION, day.isoformat()),
    )
    shore_conn.commit()

    assert summary(shore_conn, shore_config, terminal="SLT", service_date=day) is None


def test_the_age_of_the_forecast_travels_with_it(shore_conn, shore_config):
    day = date(2026, 8, 22)
    issued = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    seed(shore_conn, shore_config, "SLT", day, issued=issued)

    fresh = summary(
        shore_conn, shore_config, terminal="SLT", service_date=day, now=issued + timedelta(minutes=20)
    )
    old = summary(
        shore_conn, shore_config, terminal="SLT", service_date=day, now=issued + timedelta(hours=30)
    )

    assert fresh.issued_label == "issued in the last hour" and not fresh.stale
    assert old.stale and "30 h ago" in old.issued_label


def test_no_station_configured_means_no_forecast(conn, config):
    """The feature is optional, and a terminal without a station simply has none."""
    assert not any(t.configured_for_weather for t in config.route.terminals)
    assert summary(conn, config, terminal="SLT", service_date=date(2026, 8, 11)) is None


def test_a_database_without_the_table_answers_rather_than_raises(shore_config, tmp_path):
    from ferrycast.db import connect

    conn = connect(tmp_path / "bare.db")
    conn.execute("CREATE TABLE sailings (id INTEGER PRIMARY KEY)")
    assert summary(conn, shore_config, terminal="SLT", service_date=date(2026, 8, 11)) is None


def test_a_site_without_a_province_is_rejected(tmp_path):
    """The province is part of the feed's path, so half the setting cannot be fetched."""
    from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(
            'deck_space_url = "https://example.invalid/slt"',
            'deck_space_url = "https://example.invalid/slt"\n  weather_site = "s0000634"',
        )
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    with pytest.raises(ConfigError, match="weather_province"):
        load_config(config_dir / "ferrycast.toml")


# ---- On the page --------------------------------------------------------------------------


def test_the_shore_forecast_reaches_the_page_attributed_to_its_own_station(
    shore_client, shore_conn, shore_config
):
    """The two forecasts never merge. The shore one names its own town and its own issue
    time, so nothing measured ashore is read as a forecast for the strait."""
    seed(shore_conn, shore_config, "SLT", date(2026, 8, 20), condition="Chance of showers", high=25)

    body = body_of(shore_client.get("/?origin=SLT&service_date=2026-08-20").text)

    assert "At Saltery Bay" in body
    assert "Chance of showers" in body
    assert "25 °C" in body
    assert "Powell River" in body


def test_the_page_follows_the_terminal_you_are_leaving_from(shore_client, shore_conn, shore_config):
    day = date(2026, 8, 20)
    seed(shore_conn, shore_config, "SLT", day, condition="Sunny", station="Powell River")
    seed(shore_conn, shore_config, "ERL", day, condition="Rain", station="Gibsons")

    from_slt = body_of(shore_client.get(f"/?origin=SLT&service_date={day}").text)
    from_erl = body_of(shore_client.get(f"/?origin=ERL&service_date={day}").text)

    assert "Powell River" in from_slt and "Gibsons" not in from_slt
    assert "Gibsons" in from_erl and "Powell River" not in from_erl


def test_the_cue_carries_the_weather_when_there_is_no_record_to_carry(
    shore_client, shore_conn, shore_config
):
    """One line under the wind, and the ranking decides what goes on it. With no cancellation
    record earned yet, the weather takes it."""
    from .test_marine import seed_forecast as seed_marine

    day = date(2026, 8, 20)
    seed_marine(shore_conn, shore_config, day, "light", kt=15)
    seed(shore_conn, shore_config, "SLT", day, condition="Chance of showers")

    body = body_of(shore_client.get(f"/?origin=SLT&service_date={day}").text)

    assert '<span class="marine-sub">Chance of showers</span>' in body


def test_a_route_with_no_marine_area_still_gets_the_weather_it_waits_in(tmp_path):
    """Both feeds are optional and they fail apart. A route whose operator never cancels for
    weather has no marine area configured, and the phone must still answer "what will it be
    like at the terminal" rather than going blank because the other feed is off."""
    from fastapi.testclient import TestClient

    from ferrycast.db import init_db
    from ferrycast.web.app import create_app

    from .conftest import CONFIG_TEMPLATE, SCHEDULE_TEMPLATE

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ferrycast.toml").write_text(
        CONFIG_TEMPLATE.replace(
            'deck_space_url = "https://example.invalid/slt"',
            'deck_space_url = "https://example.invalid/slt"\n'
            f'  weather_site = "{STATION}"\n  weather_province = "BC"',
        )
    )
    (config_dir / "schedule.toml").write_text(SCHEDULE_TEMPLATE)
    config = load_config(config_dir / "ferrycast.toml")
    conn = init_db(config.db_path)
    assert config.route.marine is None
    seed(conn, config, "SLT", date(2026, 8, 20), condition="Chance of showers", high=25)

    with TestClient(create_app(str(config.source_path))) as client:
        body = body_of(client.get("/?origin=SLT&service_date=2026-08-20").text)

    # The condition leads, since there is no wind to lead with, and the temperature takes
    # the line below rather than repeating it.
    assert '<span class="marine-brief">Chance of showers</span>' in body
    assert '<span class="marine-sub">high 25 °C</span>' in body
    # And the sheet carries one section, not an empty heading for the feed that is off.
    assert "At Saltery Bay" in body
    assert "On the water" not in body
    conn.close()


def test_the_record_outranks_the_weather_on_a_day_being_planned(
    shore_client, shore_conn, shore_config
):
    """Both would fit on neither. The record is the only one of the two that says anything
    about whether the boat runs, so it takes the line and the weather waits in the sheet."""
    from .test_marine import MIN_BAND_DAYS
    from .test_marine import seed_forecast as seed_marine
    from .test_query import seed_record

    target = date(2026, 8, 20)
    for back in range(1, MIN_BAND_DAYS + 5):
        past = target - timedelta(days=back)
        seed_marine(shore_conn, shore_config, past, "light", kt=15)
        seed_record(shore_conn, shore_config, past, "12:30", "boarded")
    seed_marine(shore_conn, shore_config, target, "light", kt=15)
    seed(shore_conn, shore_config, "SLT", target, condition="Chance of showers")

    body = body_of(shore_client.get(f"/?origin=SLT&service_date={target}").text)

    assert '<span class="marine-sub">No cancellations in' in body
    # Not lost — the sheet has it, under its own heading.
    assert "At Saltery Bay" in body
    assert "Chance of showers" in body
