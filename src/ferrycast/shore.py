"""The weather ashore, at the terminal you are leaving from, from ECCC.

`marine.py` reads the water: wind, and whether it has ever cancelled a sailing here. This
module reads the other half of the same question — what it is doing at the compound while
you sit in it. They are deliberately separate, because they are separate ECCC products
about separate places. The marine forecast covers the Strait of Georgia; this one covers a
town on the shore, and merging the two into one line of stats would attribute a temperature
measured miles inland to a forecast for the middle of a strait.

**Source.** MSC Datamart publishes one XML document per city page, in the same
hour-directory shape as the marine feed:

    https://dd.weather.gc.ca/today/citypage_weather/{PROVINCE}/{HH}/
        {YYYYMMDD}T{HHMMSS.sss}Z_MSC_CitypageWeather_{site}_{lang}.xml

so finding the current one means walking hours backwards, exactly as `marine.fetch_forecast`
does. One province directory holds every station in it — this route's two terminals are both
in BC — so a refresh reads each hour's listing once and takes both files from it.

**Periods to dates.** ECCC forecasts in half-days: "Today", "Tonight", then weekday and
weekday-night out to about a week. Both halves are stored against the local date they fall
on, and the day half wins when the page asks: a ferry queue is a daytime problem, and the
day period is the one carrying the high.

**What is deliberately not read here.** Current conditions — the observation at the top of
the document — are not stored: the page always shows a *date*, and an observation is only
ever about now, so it would be the one number on screen that meant something different from
everything beside it. Land warnings are not read either; the warning this page raises is
the marine one, because that is the one that cancels sailings.

The forecast is quoted, never paraphrased — the same rule as the water.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .config import Config, ConfigError, Terminal
from .db import JobRun
from .fetching import fetch
from .timeutil import iso, local, now_utc, parse_iso

BASE_URL = "https://dd.weather.gc.ca/today/citypage_weather"

# As the marine feed: forecasts are issued a few times a day and amended between, so twelve
# hours covers an ordinary gap plus a missed cycle without crawling the whole day.
LOOKBACK_HOURS = 12

# Enough of an unrecognised document to diagnose it, bounded so a changed feed cannot fill
# the volume one fetch at a time. The real files are about 12 KB.
RAW_LIMIT = 8000

HREF_RE = re.compile(r'href="([^"]+_MSC_CitypageWeather_[A-Za-z0-9]+_[a-z]{2}\.xml)"')

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class ShoreError(Exception):
    """The feed could not be read. Always caught — a missing forecast is a gap, not a crash."""


@dataclass
class ForecastPeriod:
    """One half-day, as ECCC worded it."""

    service_date: str
    kind: str  # day | night
    period: str | None       # ECCC's own label: "Today", "Thursday night"
    condition: str | None    # the abbreviated forecast: "Chance of showers"
    pop: int | None          # probability of precipitation, %
    temp_high: int | None
    temp_low: int | None
    summary: str | None      # the full sentence


@dataclass
class ShoreForecast:
    site: str
    station: str | None
    region: str | None
    issued_at: datetime
    periods: list[ForecastPeriod] = field(default_factory=list)
    raw: str | None = None


# ---- Parsing ---------------------------------------------------------------------------


def _text(node: ET.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    collapsed = " ".join(node.text.split())
    return collapsed or None


def _int(node: ET.Element | None) -> int | None:
    raw = _text(node)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _issued_at(root: ET.Element) -> datetime:
    """When ECCC issued this forecast, in UTC.

    Read from the forecast group's own issue stamp rather than from the document's creation
    time or the filename: the file is rewritten every hour with the same forecast inside it,
    and calling that a new issue would make every forecast look minutes old.
    """
    group = root.find("forecastGroup")
    for parent in (group, root):
        if parent is None:
            continue
        for stamp in parent.findall("dateTime"):
            if stamp.get("zone") != "UTC":
                continue
            raw = _text(stamp.find("timeStamp"))
            # 12 digits in the marine feed, 14 here. Both are YYYYMMDDHHMM at the front.
            if raw and raw.isdigit() and len(raw) in (12, 14):
                return datetime.strptime(raw[:12], "%Y%m%d%H%M").replace(tzinfo=UTC)
    raise ShoreError("no UTC issue time in the document")


def _period_date(name: str, issued_local: date) -> tuple[date, str] | None:
    """Which local date a period label falls on, and whether it is the day or the night.

    Returns None for anything unrecognised. A label this does not know is skipped rather
    than guessed at: attaching "Thursday" to the wrong Thursday would put a forecast on a
    date ECCC never wrote it for, which is worse than the date having no forecast at all.
    """
    label = name.strip().lower()
    if not label:
        return None
    night = label.endswith("night")
    # "Today", "Tonight", and the mid-day issues that say "This afternoon" instead.
    if label in ("today", "tonight") or label.startswith("this "):
        return issued_local, "night" if label == "tonight" else "day"

    weekday = WEEKDAYS.get(label.split()[0])
    if weekday is None:
        return None
    # The next occurrence strictly after the issue date: a document issued on a Thursday
    # that names "Thursday" means the one a week out, not the day it was written.
    ahead = (weekday - issued_local.weekday()) % 7 or 7
    return issued_local + timedelta(days=ahead), "night" if night else "day"


def _temperature(node: ET.Element | None, which: str) -> int | None:
    if node is None:
        return None
    for temp in node.findall("temperature"):
        if temp.get("class") == which:
            return _int(temp)
    return None


def parse_forecast(xml: str, site: str) -> ShoreForecast:
    """Turn one MSC CitypageWeather document into per-date half-days.

    Raises `ShoreError` on anything it cannot read, which the caller records as a gap.
    """
    # As in `marine.parse_forecast`: ElementTree resolves no external entities but will
    # expand internal ones, and this document arrives over the network. No legitimate MSC
    # file carries a DOCTYPE, so refusing one closes the expansion route for free.
    if "<!DOCTYPE" in xml[:2048].upper():
        raise ShoreError("document declares a DOCTYPE; refusing to parse")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ShoreError(f"not well-formed XML: {exc}") from exc

    issued = _issued_at(root)

    location = root.find("location")
    station = _text(location.find("name")) if location is not None else None
    region = _text(location.find("region")) if location is not None else None

    group = root.find("forecastGroup")
    if group is None:
        raise ShoreError("no forecast group in the document")

    # ECCC stamps the issue in local time too, and the local date is the one "Today" means.
    issued_local = None
    for stamp in group.findall("dateTime"):
        if stamp.get("zone") != "UTC":
            raw = _text(stamp.find("timeStamp"))
            if raw and raw.isdigit() and len(raw) in (12, 14):
                issued_local = datetime.strptime(raw[:8], "%Y%m%d").date()
                break
    if issued_local is None:
        raise ShoreError("no local issue date in the document")

    periods = []
    for node in group.findall("forecast"):
        label = node.find("period")
        name = (label.get("textForecastName") if label is not None else None) or _text(label)
        when = _period_date(name or "", issued_local)
        if when is None:
            continue
        service_date, kind = when
        abbreviated = node.find("abbreviatedForecast")
        temperatures = node.find("temperatures")
        periods.append(
            ForecastPeriod(
                service_date=service_date.isoformat(),
                kind=kind,
                period=name,
                condition=_text(abbreviated.find("textSummary")) if abbreviated is not None else None,
                pop=_int(abbreviated.find("pop")) if abbreviated is not None else None,
                temp_high=_temperature(temperatures, "high"),
                temp_low=_temperature(temperatures, "low"),
                summary=_text(node.find("textSummary")),
            )
        )

    if not periods:
        raise ShoreError("no forecast periods found")

    return ShoreForecast(
        site=site,
        station=station,
        region=region,
        issued_at=issued,
        periods=periods,
        raw=xml[:RAW_LIMIT],
    )


# ---- Fetching --------------------------------------------------------------------------


def _listing_url(province: str, hour: datetime) -> str:
    return f"{BASE_URL}/{province.upper()}/{hour:%H}/"


def newest_file(listing: str, site: str, lang: str = "en") -> str | None:
    """The most recent document for this station in one hour's directory listing.

    Filenames begin with a sortable UTC timestamp, so the newest is the last one by name.
    """
    suffix = f"_MSC_CitypageWeather_{site}_{lang}.xml"
    names = sorted({name for name in HREF_RE.findall(listing) if name.endswith(suffix)})
    return names[-1] if names else None


def fetch_forecast(
    config: Config,
    terminal: Terminal,
    *,
    at: datetime | None = None,
    lookback: int = LOOKBACK_HOURS,
    listings: dict[str, str | None] | None = None,
) -> ShoreForecast:
    """The current forecast for a terminal's station, or `ShoreError` if none is reachable.

    `listings` is an optional cache of directory listings shared across a single refresh.
    One province directory holds every station in it, so both ends of a route are usually
    found in the same listing and there is no reason to download it twice.
    """
    now = at or now_utc()
    last_error = f"no forecast published in the last {lookback} h"
    for back in range(lookback + 1):
        hour = now - timedelta(hours=back)
        url = _listing_url(terminal.weather_province, hour)
        if listings is not None and url in listings:
            body = listings[url]
        else:
            response = fetch(
                url,
                user_agent=config.capture.user_agent,
                timeout=config.capture.timeout_seconds,
                # A missing hour directory is a 404 and an ordinary answer, not something to
                # retry: the hours before the current one may simply not exist yet today.
                max_retries=0,
            )
            body = response.text if response.ok else None
            if listings is not None:
                listings[url] = body
        if not body:
            continue
        name = newest_file(body, terminal.weather_site)
        if not name:
            continue
        document = fetch(
            url + name,
            user_agent=config.capture.user_agent,
            timeout=config.capture.timeout_seconds,
            max_retries=config.capture.max_retries,
        )
        if not document.ok or not document.text:
            last_error = document.error or "empty document"
            continue
        return parse_forecast(document.text, terminal.weather_site)
    raise ShoreError(last_error)


# ---- Storage ---------------------------------------------------------------------------


def store_forecast(
    conn: sqlite3.Connection,
    config: Config,
    terminal: Terminal,
    forecast: ShoreForecast,
    *,
    fetched_at: datetime,
) -> int:
    stored = 0
    for period in forecast.periods:
        cur = conn.execute(
            """INSERT OR IGNORE INTO shore_forecast
                   (route, terminal, site, service_date, issued_at, fetched_at, station,
                    region, kind, period, condition, pop, temp_high, temp_low, summary,
                    fetch_status, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)""",
            (
                config.route.id,
                terminal.code,
                forecast.site,
                period.service_date,
                iso(forecast.issued_at),
                iso(fetched_at),
                forecast.station,
                forecast.region,
                period.kind,
                period.period,
                period.condition,
                period.pop,
                period.temp_high,
                period.temp_low,
                period.summary,
                forecast.raw,
            ),
        )
        stored += cur.rowcount or 0
    conn.commit()
    return stored


def _record_problem(
    conn: sqlite3.Connection,
    config: Config,
    terminal: Terminal,
    at: datetime,
    error: str,
) -> None:
    """A gap, recorded against the date it was noticed.

    `issued_at` carries the fetch time because there is no issue to speak of, and the unique
    key then keeps one failure per attempt rather than collapsing them all into one row.
    """
    conn.execute(
        """INSERT OR IGNORE INTO shore_forecast
               (route, terminal, site, service_date, issued_at, fetched_at, kind,
                fetch_status, error)
           VALUES (?, ?, ?, ?, ?, ?, 'day', 'error', ?)""",
        (
            config.route.id,
            terminal.code,
            terminal.weather_site,
            local(at, config.tz).date().isoformat(),
            iso(at),
            iso(at),
            error[:500],
        ),
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection) -> bool:
    """Whether this database has been through the schema that introduced the shore forecast.

    Checked rather than assumed, for the same reason `marine._table_exists` is: `ferrycast
    serve` does not create tables, and an install upgraded without `init` must lose the
    forecast rather than the page.
    """
    return (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'shore_forecast'"
        ).fetchone()[0]
        > 0
    )


@dataclass
class ShoreSummary:
    """What ECCC says it will be doing at one terminal on one date."""

    service_date: str
    terminal: str
    site: str
    station: str | None
    region: str | None
    issued_at: str
    stale_hours: int
    kind: str
    period: str | None
    condition: str | None
    pop: int | None
    temp_high: int | None
    temp_low: int | None
    summary: str | None

    @property
    def temp_label(self) -> str:
        """Which end of the day the temperature on file is. ECCC gives one, not both."""
        return "Low" if self.temp_high is None and self.temp_low is not None else "High"

    @property
    def temp_c(self) -> int | None:
        return self.temp_high if self.temp_high is not None else self.temp_low

    @property
    def issued_label(self) -> str:
        if self.stale_hours < 1:
            return "issued in the last hour"
        if self.stale_hours < 48:
            return f"issued {self.stale_hours} h ago"
        return f"issued {self.stale_hours // 24} days ago"

    @property
    def stale(self) -> bool:
        """Old enough that presenting it as current would overstate it. As on the water."""
        return self.stale_hours >= 24


def summary(
    conn: sqlite3.Connection,
    config: Config,
    *,
    terminal: str,
    service_date: date,
    now: datetime | None = None,
) -> ShoreSummary | None:
    """The forecast in force for one terminal on one date.

    The newest issue wins, and the day half beats the night half issued with it: a ferry
    queue is a daytime problem, and the day period is the one carrying the high.
    """
    if not _table_exists(conn):
        return None
    try:
        station = config.route.terminal(terminal)
    except ConfigError:
        return None
    if not station.configured_for_weather:
        return None

    row = conn.execute(
        """SELECT * FROM shore_forecast
            WHERE route = ? AND terminal = ? AND service_date = ? AND fetch_status = 'ok'
            ORDER BY issued_at DESC, kind = 'day' DESC
            LIMIT 1""",
        (config.route.id, terminal, service_date.isoformat()),
    ).fetchone()
    if row is None:
        return None

    issued = parse_iso(row["issued_at"])
    return ShoreSummary(
        service_date=row["service_date"],
        terminal=row["terminal"],
        site=row["site"],
        station=row["station"],
        region=row["region"],
        issued_at=row["issued_at"],
        stale_hours=max(0, int(((now or now_utc()) - issued).total_seconds() // 3600)),
        kind=row["kind"],
        period=row["period"],
        condition=row["condition"],
        pop=row["pop"],
        temp_high=row["temp_high"],
        temp_low=row["temp_low"],
        summary=row["summary"],
    )


def refresh(conn: sqlite3.Connection, config: Config, *, at: datetime | None = None) -> dict:
    """Fetch each configured terminal's forecast and record it. Never raises.

    A refresh that fails changes nothing a reader sees: the last forecast is still in the
    table and is still served, with its age attached. Only `/health` treats staleness as a
    problem — a stale forecast is an operational fault, not a reason to withhold what ECCC
    last said.
    """
    fetched_at = at or now_utc()
    terminals = [t for t in config.route.terminals if t.configured_for_weather]
    if not terminals:
        return {"ok": False, "skipped": True, "rows": 0, "terminals": []}

    results = []
    listings: dict[str, str | None] = {}
    with JobRun(conn, "shore") as run:
        for terminal in terminals:
            run.attempted += 1
            try:
                forecast = fetch_forecast(config, terminal, at=fetched_at, listings=listings)
            except ShoreError as exc:
                _record_problem(conn, config, terminal, fetched_at, str(exc))
                results.append({"terminal": terminal.code, "ok": False, "error": str(exc)})
                continue
            rows = store_forecast(conn, config, terminal, forecast, fetched_at=fetched_at)
            run.succeeded += 1
            results.append(
                {
                    "terminal": terminal.code,
                    "ok": True,
                    "rows": rows,
                    "issued_at": iso(forecast.issued_at),
                    "station": forecast.station,
                    "periods": len(forecast.periods),
                }
            )

    return {
        "ok": any(r["ok"] for r in results),
        "rows": sum(r.get("rows", 0) for r in results),
        "terminals": results,
    }
