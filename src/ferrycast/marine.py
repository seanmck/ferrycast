"""The marine forecast for the water this route crosses, from ECCC.

Wind is the one condition that cancels a sailing outright, and it is the only thing on the
page that FerryCast does not observe for itself — everything else here is counted from
cameras, deck space or people in the line. So this module is deliberately thin: it fetches
Environment and Climate Change Canada's own marine forecast, records what it said and when
it said it, and stops. The forecast is quoted, never paraphrased.

**Source.** MSC Datamart publishes one XML document per marine area:

    https://dd.weather.gc.ca/today/marine_weather/{domain}/{HH}/
        {YYYYMMDD}T{HHMMSS.sss}Z_MSC_MarineWeather_{site}_{lang}.xml

`{HH}` is the UTC hour the file was *issued*, so there is no "latest" path — finding the
current forecast means walking hours backwards until one turns up. Only `/today/` carries
marine weather; the dated archive directories do not, so shortly after 00 UTC the most
recent issue can be out of reach entirely. That is not a failure worth shouting about: the
last forecast stays in the table and keeps being served, and `/health` reports its age.

**Two horizons, one table.** The regular forecast covers tonight and tomorrow; the extended
one names weekdays three days out. Both are expanded to a row per local service date, which
is the key the day board asks by. Where both cover a date, the newer issue wins and a
regular forecast beats an extended one at the same issue.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .config import Config, MarineArea
from .db import JobRun
from .fetching import fetch
from .timeutil import iso, local, now_utc, parse_iso

BASE_URL = "https://dd.weather.gc.ca/today/marine_weather"

# How far back to look for the newest issue. Marine forecasts go out roughly every six
# hours, so twelve covers an ordinary gap plus a missed cycle without turning one refresh
# into a crawl of the whole day.
LOOKBACK_HOURS = 12

# Enough of an unrecognised document to diagnose it, bounded so a changed feed cannot fill
# the volume one fetch at a time. The real files are about 3.5 KB.
RAW_LIMIT = 8000

HREF_RE = re.compile(r'href="([^"]+_MSC_MarineWeather_[A-Za-z0-9]+_[a-z]{2}\.xml)"')

# Every integer in a <wind> element is a speed in knots — the element carries nothing else
# numeric. Taking the maximum is deliberate: "northwest 15 to 20 except 30 near Nanaimo"
# describes water this route crosses, and 30 is the number that decides whether it sails.
KNOTS_RE = re.compile(r"\b(\d{1,3})\b")

# A speed written as a span, which is how most of them are written: "15 to 20 knots".
RANGE_RE = re.compile(r"\b(\d{1,3})\s+to\s+(\d{1,3})\b")

# Compass points as ECCC spells them, abbreviated as a phone header can carry them.
DIRECTIONS = (
    ("northwest", "NW"), ("northeast", "NE"), ("southwest", "SW"), ("southeast", "SE"),
    ("north", "N"), ("south", "S"), ("east", "E"), ("west", "W"),
)

# ECCC's own vocabulary, most severe first. Only consulted when the sentence carries no
# number at all — "Wind light" is a complete forecast, and reading it as unknown would
# withhold an answer ECCC actually gave. Ordered so "storm" is tested before "light" for a
# sentence that somehow contains both.
NAMED_BANDS = (
    ("hurricane", 64),
    ("storm", 48),
    ("gale", 34),
    ("strong wind", 20),
    ("light", 10),
)

# The thresholds ECCC issues its marine wind warnings at. Bands are named for the warning
# rather than for Beaufort, because the warning is the thing a sailing is cancelled against.
BANDS = ((48, "storm"), (34, "gale"), (20, "strong"), (0, "light"))

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class MarineError(Exception):
    """The feed could not be read. Always caught — a missing forecast is a gap, not a crash."""


@dataclass
class ForecastDay:
    """One local date's worth of forecast, as ECCC worded it."""

    service_date: str
    kind: str  # regular | extended
    period: str | None
    wind: str | None
    visibility: str | None
    wind_max_kt: int | None
    band: str


@dataclass
class Forecast:
    site: str
    area: str | None
    location: str | None
    issued_at: datetime
    warning: str | None
    days: list[ForecastDay] = field(default_factory=list)
    raw: str | None = None


# ---- Parsing ---------------------------------------------------------------------------


def _text(node: ET.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    collapsed = " ".join(node.text.split())
    return collapsed or None


def wind_speed(wind: str | None) -> int | None:
    """The strongest wind named in a forecast sentence, in knots."""
    if not wind:
        return None
    speeds = [int(n) for n in KNOTS_RE.findall(wind)]
    # A three-digit "speed" is not one; the element has no other numerals, but a malformed
    # document should not be able to report 250-knot winds.
    speeds = [s for s in speeds if s <= 150]
    if speeds:
        return max(speeds)
    lowered = wind.lower()
    for word, floor in NAMED_BANDS:
        if word in lowered:
            return floor
    return None


def wind_brief(wind: str | None) -> str | None:
    """The wind sentence at a glance: a compass point and a speed, and nothing else.

    A phone header has room for about a dozen characters, and the sentence ECCC writes is
    two lines of it — "Wind northwest 15 to 20 knots diminishing to northwest 10 to 15 near
    midnight and to light Thursday morning." So the compact reading takes the direction and
    the speed it opens with, which is the wind at the top of the period, and leaves the rest
    to the sheet: this is a cue to tap, not the forecast. The full sentence is one tap away
    on the same screen, never replaced by this.

    Numbers only — the words around them are ECCC's and are not reworded here. A sentence
    that names no speed at all falls back to ECCC's own adjective ("light"), which is a
    quotation rather than a summary of one.
    """
    if not wind:
        return None

    lowered = wind.lower()

    # The direction the sentence opens with, not the first one this table happens to list:
    # "southeast 5 to 15 knots becoming northwest" starts southeast, and a table order would
    # answer with the wind that arrives late in the day. Longer names win a tie so that
    # "northwest" is never read as the "north" inside it.
    direction = None
    best: tuple[int, int] | None = None
    for word, short in DIRECTIONS:
        at = lowered.find(word)
        if at == -1:
            continue
        key = (at, -len(word))
        if best is None or key < best:
            best, direction = key, short

    # The first speed in the sentence, whether it was written as a span or on its own —
    # "southeast 10 knots increasing to 15 to 20 this afternoon" opens at 10. A span wins a
    # tie because it starts at the same character as its own first number.
    speed = None
    span = RANGE_RE.search(wind)
    single = next((m for m in KNOTS_RE.finditer(wind) if int(m.group(1)) <= 150), None)
    if span and int(span.group(2)) <= 150 and (single is None or span.start() <= single.start()):
        speed = f"{span.group(1)}–{span.group(2)} kt"
    elif single is not None:
        speed = f"{single.group(1)} kt"
    if speed is None:
        speed = next((word for word, _ in NAMED_BANDS if word in lowered), None)

    return " ".join(part for part in (direction, speed) if part) or None


def band_for(knots: int | None) -> str:
    """Which warning band a wind speed falls in.

    `unknown` rather than a guess when nothing parsed: the band gates a published claim
    about cancellations, and defaulting an unreadable forecast to "light" would attach that
    claim to weather nobody has actually checked.
    """
    if knots is None:
        return "unknown"
    for floor, name in BANDS:
        if knots >= floor:
            return name
    return "light"


def _warning_text(root: ET.Element) -> str | None:
    """Whatever the feed is warning about, or None when it is warning about nothing.

    Deliberately shape-agnostic. There was no marine warning in force anywhere in Canada
    while this was written, so the element's populated structure could not be observed, and
    the schema URL the documents name has been 404 for some time. Rather than guess at a
    layout and risk reading a real gale warning as an empty element, this collects every
    scrap of text and every naming attribute beneath `<warnings>`: any shape at all yields
    something, and only a genuinely empty element yields nothing.

    The one behaviour that matters: an unrecognised warning must never read as "no warning".
    """
    warnings = root.find("warnings")
    if warnings is None:
        return None
    parts: list[str] = []
    for node in warnings.iter():
        if node is warnings:
            # The container's own attributes can name the event even when it has no children.
            pass
        for key in ("name", "type", "event", "priority"):
            value = node.get(key)
            if value:
                parts.append(value.strip())
        if node.text and node.text.strip():
            parts.append(" ".join(node.text.split()))
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return " · ".join(seen) or None


def _issued_at(root: ET.Element) -> datetime:
    """When ECCC issued this forecast, in UTC.

    Read from the regular forecast's own UTC stamp rather than from the filename: the
    filename is when the file was written, and an amended forecast is what it says it is.
    """
    for parent in (root.find("regularForecast"), root):
        if parent is None:
            continue
        for stamp in parent.findall("dateTime"):
            if stamp.get("zone") != "UTC":
                continue
            raw = _text(stamp.find("timeStamp"))
            if raw and len(raw) == 12 and raw.isdigit():
                return datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=UTC)
    raise MarineError("no UTC issue time in the document")


def _pick_location(parent: ET.Element, preferred: str | None) -> ET.Element | None:
    """The sub-area this route crosses.

    A marine area is often split — the Strait of Georgia is forecast north and south of
    Nanaimo separately — and taking the first would silently answer about the wrong half of
    a 200 km waterway. An unmatched preference falls back to the first rather than to
    nothing, because some fraction of a forecast is better than none, but the row records
    which location it actually used so the page can say so.
    """
    locations = parent.findall("location")
    if not locations:
        return None
    if preferred:
        for node in locations:
            if (node.get("name") or "").strip().lower() == preferred.strip().lower():
                return node
    return locations[0]


def _regular_days(
    root: ET.Element, issued_local: date, preferred: str | None
) -> tuple[list[ForecastDay], str | None]:
    regular = root.find("regularForecast")
    if regular is None:
        return [], None
    node = _pick_location(regular, preferred)
    if node is None:
        return [], None
    condition = node.find("weatherCondition")
    if condition is None:
        return [], node.get("name")

    wind = _text(condition.find("wind"))
    visibility = _text(condition.find("weatherVisibility"))
    period = _text(condition.find("periodOfCoverage"))
    knots = wind_speed(wind)

    # "Tonight and Wednesday" is one sentence covering two local dates, and splitting a
    # compound forecast into per-day halves would be inventing text ECCC did not write. It
    # is stored against both dates instead, whole and attributed to its period.
    days = [
        ForecastDay(
            service_date=(issued_local + timedelta(days=offset)).isoformat(),
            kind="regular",
            period=period,
            wind=wind,
            visibility=visibility,
            wind_max_kt=knots,
            band=band_for(knots),
        )
        for offset in (0, 1)
    ]
    return days, node.get("name")


def _extended_days(
    root: ET.Element, issued_local: date, preferred: str | None
) -> list[ForecastDay]:
    extended = root.find("extendedForecast")
    if extended is None:
        return []
    node = _pick_location(extended, preferred)
    if node is None:
        return []
    condition = node.find("weatherCondition")
    if condition is None:
        return []

    days = []
    for period in condition.findall("forecastPeriod"):
        name = (period.get("name") or "").strip().lower()
        weekday = WEEKDAYS.get(name.split()[0] if name else "")
        if weekday is None:
            continue
        # The next occurrence strictly after the issue date. ECCC's extended periods start
        # the day after tomorrow, so "Thursday" issued on a Thursday means next Thursday.
        ahead = (weekday - issued_local.weekday()) % 7 or 7
        wind = _text(period)
        knots = wind_speed(wind)
        days.append(
            ForecastDay(
                service_date=(issued_local + timedelta(days=ahead)).isoformat(),
                kind="extended",
                period=period.get("name"),
                wind=wind,
                visibility=None,
                wind_max_kt=knots,
                band=band_for(knots),
            )
        )
    return days


def parse_forecast(xml: str, config: Config, site: str, preferred: str | None) -> Forecast:
    """Turn one MSC MarineWeather document into per-date rows.

    Raises `MarineError` on anything it cannot read, which the caller records as a gap.
    """
    # ElementTree resolves no external entities, but it will happily expand internal ones,
    # and this document arrives over the network. No legitimate MSC file carries a DOCTYPE,
    # so refusing one costs nothing and closes the expansion route entirely.
    if "<!DOCTYPE" in xml[:2048].upper():
        raise MarineError("document declares a DOCTYPE; refusing to parse")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MarineError(f"not well-formed XML: {exc}") from exc

    issued = _issued_at(root)
    issued_local = local(issued, config.tz).date()

    days, location = _regular_days(root, issued_local, preferred)
    extended = _extended_days(root, issued_local, preferred)
    # A regular forecast is the more specific one, so it holds its dates against an extended
    # period that happens to name the same day.
    covered = {day.service_date for day in days}
    days.extend(day for day in extended if day.service_date not in covered)

    if not days:
        raise MarineError("no forecast periods found")

    return Forecast(
        site=site,
        area=_text(root.find("area")),
        location=location,
        issued_at=issued,
        warning=_warning_text(root),
        days=days,
        raw=xml[:RAW_LIMIT],
    )


# ---- Fetching --------------------------------------------------------------------------


def _listing_url(domain: str, hour: datetime) -> str:
    return f"{BASE_URL}/{domain}/{hour:%H}/"


def newest_file(listing: str, site: str, lang: str = "en") -> str | None:
    """The most recent document for this site in one hour's directory listing.

    Filenames begin with a sortable UTC timestamp, so the newest is the last one by name.
    """
    suffix = f"_MSC_MarineWeather_{site}_{lang}.xml"
    names = sorted({name for name in HREF_RE.findall(listing) if name.endswith(suffix)})
    return names[-1] if names else None


def fetch_forecast(
    config: Config, area: MarineArea, *, at: datetime | None = None, lookback: int = LOOKBACK_HOURS
) -> Forecast:
    """The current forecast for a marine area, or `MarineError` if none is reachable.

    Walks back an hour at a time from now. Stops at the first hour holding a document for
    this site, which is almost always within a cycle or two.
    """
    now = at or now_utc()
    last_error = "no forecast published in the last "
    for back in range(lookback + 1):
        hour = now - timedelta(hours=back)
        listing = fetch(
            _listing_url(area.domain, hour),
            user_agent=config.capture.user_agent,
            timeout=config.capture.timeout_seconds,
            # A missing hour directory is a 404 and an ordinary answer, not something to
            # retry: the hours before the current one simply may not exist yet today.
            max_retries=0,
        )
        if not listing.ok or not listing.text:
            continue
        name = newest_file(listing.text, area.site)
        if not name:
            continue
        document = fetch(
            _listing_url(area.domain, hour) + name,
            user_agent=config.capture.user_agent,
            timeout=config.capture.timeout_seconds,
            max_retries=config.capture.max_retries,
        )
        if not document.ok or not document.text:
            last_error = document.error or "empty document"
            continue
        return parse_forecast(document.text, config, area.site, area.location)
    raise MarineError(f"{last_error}{lookback} h")


# ---- Storage ---------------------------------------------------------------------------


def store_forecast(
    conn: sqlite3.Connection, config: Config, forecast: Forecast, *, fetched_at: datetime
) -> int:
    stored = 0
    for day in forecast.days:
        cur = conn.execute(
            """INSERT OR IGNORE INTO marine_forecast
                   (route, site, service_date, issued_at, fetched_at, area, location,
                    kind, period, wind, visibility, warning, wind_max_kt, band,
                    fetch_status, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)""",
            (
                config.route.id,
                forecast.site,
                day.service_date,
                iso(forecast.issued_at),
                iso(fetched_at),
                forecast.area,
                forecast.location,
                day.kind,
                day.period,
                day.wind,
                day.visibility,
                forecast.warning,
                day.wind_max_kt,
                day.band,
                forecast.raw,
            ),
        )
        stored += cur.rowcount or 0
    conn.commit()
    return stored


def _record_problem(
    conn: sqlite3.Connection, config: Config, site: str, at: datetime, error: str
) -> None:
    """A gap, recorded against the date it was noticed.

    `issued_at` carries the fetch time because there is no issue to speak of, and the unique
    key then keeps one failure per attempt rather than collapsing them all into one row.
    """
    conn.execute(
        """INSERT OR IGNORE INTO marine_forecast
               (route, site, service_date, issued_at, fetched_at, kind,
                fetch_status, error)
           VALUES (?, ?, ?, ?, ?, 'regular', 'error', ?)""",
        (
            config.route.id,
            site,
            local(at, config.tz).date().isoformat(),
            iso(at),
            iso(at),
            error[:500],
        ),
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection) -> bool:
    """Whether this database has been through the schema that introduced the forecast.

    Checked rather than assumed. `ferrycast run` creates the table before it serves, but
    `ferrycast serve` on its own does not, and an install upgraded without either would
    otherwise turn a page that has never needed the forecast into a 500. "No forecast" is
    already a first-class state here, so an absent table is answered, not raised.
    """
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'marine_forecast'"
        ).fetchone()[0]
        > 0
    )


# How many past days in the same band must be on record before the page will say anything
# about cancellations. The claim is an absence — "nothing was cancelled in weather like
# this" — and an absence over four days is not evidence of anything.
MIN_BAND_DAYS = 20


@dataclass
class MarineSummary:
    """What ECCC says about one date, and what this route has done in weather like it."""

    service_date: str
    area: str | None
    location: str | None
    issued_at: str
    stale_hours: int
    kind: str
    period: str | None
    wind: str | None
    visibility: str | None
    warning: str | None
    wind_max_kt: int | None
    band: str
    # The cancellation record for this band, or None when too little has been recorded to
    # say anything at all. Never a zero-dressed-as-a-finding.
    record: dict | None = None

    @property
    def brief(self) -> str | None:
        """The forecast in a phone header's worth of characters. See `wind_brief`."""
        return wind_brief(self.wind)

    @property
    def issued_label(self) -> str:
        if self.stale_hours < 1:
            return "issued in the last hour"
        if self.stale_hours < 48:
            return f"issued {self.stale_hours} h ago"
        return f"issued {self.stale_hours // 24} days ago"

    @property
    def stale(self) -> bool:
        """Old enough that presenting it as current would overstate it.

        A forecast is still the last thing ECCC said about this date, so it is shown either
        way — but past a day it stops looking like fresh information, because the honest
        reading of a day-old marine forecast is "this is what they were saying", not "this
        is the weather".
        """
        return self.stale_hours >= 24


BAND_WORDS = {
    "light": "light winds",
    "strong": "strong winds",
    "gale": "gale-force winds",
    "storm": "storm-force winds",
    "unknown": "these conditions",
}


def summary(
    conn: sqlite3.Connection,
    config: Config,
    *,
    service_date: date,
    now: datetime | None = None,
) -> MarineSummary | None:
    """The forecast in force for one date, with this route's record in that band.

    The newest issue wins, and a regular forecast beats an extended one issued at the same
    moment — the regular forecast is the more specific of the two.
    """
    if config.route.marine is None or not _table_exists(conn):
        return None

    row = conn.execute(
        """SELECT * FROM marine_forecast
            WHERE route = ? AND service_date = ? AND fetch_status = 'ok'
            ORDER BY issued_at DESC, kind = 'regular' DESC
            LIMIT 1""",
        (config.route.id, service_date.isoformat()),
    ).fetchone()
    if row is None:
        return None

    issued = parse_iso(row["issued_at"])
    return MarineSummary(
        service_date=row["service_date"],
        area=row["area"],
        location=row["location"],
        issued_at=row["issued_at"],
        stale_hours=max(0, int(((now or now_utc()) - issued).total_seconds() // 3600)),
        kind=row["kind"],
        period=row["period"],
        wind=row["wind"],
        visibility=row["visibility"],
        warning=row["warning"],
        wind_max_kt=row["wind_max_kt"],
        band=row["band"],
        record=band_record(conn, config, band=row["band"], before=service_date),
    )


def band_record(
    conn: sqlite3.Connection, config: Config, *, band: str, before: date
) -> dict | None:
    """What this route has done on past days forecast to be in the same wind band.

    The design this came from states it outright — "no sailing on this route cancelled in
    these conditions since 2021" — and that is a claim FerryCast could not make on the day
    it shipped, because nothing joined weather to outcomes. It can make it once it has
    watched enough weather, and not before: below `MIN_BAND_DAYS` this returns None and the
    page says nothing rather than reporting an absence as a finding.

    Deliberately scoped to days strictly before the one being asked about. A board for a
    future date must not count that date's own sailings, and a board for today must not
    quote a cancellation that has not happened yet.

    `unknown` never yields a record: a band that could not be read is not a band, and
    pooling unreadable forecasts would attach a cancellation claim to weather nobody
    checked.
    """
    if band == "unknown":
        return None

    row = conn.execute(
        """SELECT COUNT(DISTINCT f.service_date) AS days,
                  MIN(f.service_date)            AS since,
                  COUNT(r.sailing_id)            AS sailings,
                  SUM(CASE WHEN r.outcome = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
             FROM (SELECT DISTINCT route, service_date, band
                     FROM marine_forecast
                    WHERE route = ? AND band = ? AND service_date < ?
                      AND fetch_status = 'ok') f
             LEFT JOIN sailings s
                    ON s.route = f.route AND s.service_date = f.service_date
             LEFT JOIN sailing_records r
                    ON r.sailing_id = s.id AND r.outcome != 'unknown'""",
        (config.route.id, band, before.isoformat()),
    ).fetchone()

    days = int(row["days"] or 0)
    sailings = int(row["sailings"] or 0)
    cancelled = int(row["cancelled"] or 0)

    # No sailings on record is not "nothing was cancelled". A install with forecasts but no
    # outcomes yet would otherwise announce a spotless record it has no evidence for — the
    # single most confident-sounding thing this page could say off an empty table.
    if not sailings:
        return None

    # The threshold guards the *absence*. "Nothing was cancelled in weather like this" is
    # only worth printing once enough weather like this has been watched; a cancellation
    # that did happen is a fact from the first one, and holding it back to satisfy a sample
    # size would be suppressing the more useful half of the evidence.
    if not cancelled and days < MIN_BAND_DAYS:
        return None

    return {
        "band": band,
        "band_words": BAND_WORDS.get(band, "these conditions"),
        "days": days,
        "since": row["since"],
        "sailings": sailings,
        "cancelled": cancelled,
    }


def refresh(conn: sqlite3.Connection, config: Config, *, at: datetime | None = None) -> dict:
    """Fetch the forecast and record it. Never raises.

    A refresh that fails changes nothing a reader sees: the last forecast is still in the
    table and is still served, with its age attached. Only `/health` treats staleness as a
    problem, which is the right place for it — a stale marine forecast is an operational
    fault, not a reason to withhold what ECCC last said.
    """
    area = config.route.marine
    fetched_at = at or now_utc()
    if area is None:
        return {"ok": False, "skipped": True, "rows": 0}

    with JobRun(conn, "marine") as run:
        run.attempted += 1
        try:
            forecast = fetch_forecast(config, area, at=fetched_at)
        except MarineError as exc:
            _record_problem(conn, config, area.site, fetched_at, str(exc))
            return {"ok": False, "rows": 0, "error": str(exc)}
        rows = store_forecast(conn, config, forecast, fetched_at=fetched_at)
        run.succeeded += 1
        return {
            "ok": True,
            "rows": rows,
            "issued_at": iso(forecast.issued_at),
            "area": forecast.area,
            "location": forecast.location,
            "warning": forecast.warning,
            "days": len(forecast.days),
        }
