"""R5 — the "day like today" web app.

The page is server-rendered with the next departure already answered, so the first paint
carries the answer rather than waiting on a round trip. CSS stays inline: one request for
the document, no CDN — it has to work on a phone with one bar at the side of Highway 101.
The "Deep Water" theme's three typefaces are the one exception, and they are self-hosted
and subset to the characters this app can render (58 KB in total) rather than fetched from
a third party.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

from ..config import Config, load_config
from ..db import connect
from ..export import export
from ..maintenance import capture_strip, health_report, latest_observation
from ..query import (
    OUTCOME_LABELS,
    OUTCOME_LABELS_SHORT,
    arrival_curve,
    default_sailing_time,
    query_distribution,
    sailing_times,
    upcoming_sailings,
)
from ..schedule import day_type, season
from ..timeutil import combine_local, local, now_utc, parse_hhmm
from .preview import health_preview, index_preview

STATIC = Path(__file__).parent / "static"
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


DAY_TYPE_SHORT = {
    "weekday": "Weekday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday_holiday": "Sunday / holiday",
}

SEASON_SHORT = {"peak_summer": "peak summer", "shoulder": "shoulder", "winter": "winter"}


def _countdown(config: Config, service_date: date, depart_hhmm: str) -> str | None:
    """How long until a departure, or None once it is in the past.

    The theme leads with this next to the departure time, so it has to stay readable at
    every distance: minutes for the sailing you are running for, days for one you are
    planning around. A stale countdown would be worse than none at all.
    """
    departure = combine_local(service_date, parse_hhmm(depart_hhmm), config.tz)
    minutes = int((departure - local(now_utc(), config.tz)).total_seconds() // 60)
    if minutes < 0:
        return None
    if minutes < 60:
        return f"in {minutes} min"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"in {hours} h" if rest == 0 else f"in {hours} h {rest} min"
    days = minutes // (24 * 60)
    return "tomorrow" if days == 1 else f"in {days} days"


def create_app(config_path: str | None = None) -> FastAPI:
    config = load_config(config_path)
    app = FastAPI(title="FerryCast", docs_url="/api/docs", redoc_url=None)
    app.state.config = config
    # The theme's CSS is inline on every page, which keeps the document to one request but
    # costs about 23 KB uncompressed. Gzip takes that to under 7 KB — the difference
    # between the two on a single bar of signal is the whole <2s budget.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def get_config() -> Config:
        return app.state.config

    def get_conn(config: Config = Depends(get_config)):
        conn = connect(config.db_path, create=False)
        try:
            yield conn
        finally:
            conn.close()

    def _parse_date(value: str | None) -> date:
        if not value:
            return date.today()
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(400, f"bad date {value!r}; expected YYYY-MM-DD") from exc

    def _validate_origin(config: Config, origin: str) -> str:
        if origin not in config.route.codes:
            raise HTTPException(
                400, f"unknown terminal {origin!r}; expected one of {', '.join(config.route.codes)}"
            )
        return origin

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        origin: str | None = None,
        service_date: str | None = None,
        time: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        upcoming = upcoming_sailings(config, origin=origin, limit=1)
        default = upcoming[0] if upcoming else None

        chosen_origin = _validate_origin(
            config, origin or (default.origin if default else config.route.codes[0])
        )
        chosen_date = _parse_date(
            service_date or (default.service_date.isoformat() if default else None)
        )
        times = sailing_times(config, chosen_origin, chosen_date)
        # The form resubmits all three fields together, so switching terminal arrives here
        # carrying the *other* terminal's departure time — which is never in this one's
        # timetable. That has to fall back to the next sailing rather than the first of the
        # day, or one tap on the segmented control lands you on this morning's 06:30.
        chosen_time = time if time in times else default_sailing_time(config, times, chosen_date)

        distribution = None
        if chosen_time:
            distribution = query_distribution(
                conn,
                config,
                origin=chosen_origin,
                target_date=chosen_date,
                depart_hhmm=chosen_time,
            ).to_dict()

        kind = DAY_TYPE_SHORT.get(day_type(chosen_date), day_type(chosen_date))
        bucket = SEASON_SHORT.get(season(chosen_date), season(chosen_date))

        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "route": config.route,
                "terminals": config.route.terminals,
                "origin": chosen_origin,
                "service_date": chosen_date.isoformat(),
                "times": times,
                "selected_time": chosen_time,
                "distribution": distribution,
                "labels": OUTCOME_LABELS,
                "short_labels": OUTCOME_LABELS_SHORT,
                "strapline": f"{kind} · {bucket}",
                "countdown": (
                    _countdown(config, chosen_date, chosen_time) if chosen_time else None
                ),
                "preview": index_preview(
                    request,
                    config,
                    origin=chosen_origin,
                    service_date=chosen_date.isoformat(),
                    selected_time=chosen_time,
                    distribution=distribution,
                ),
            },
        )

    @app.get("/health", response_class=HTMLResponse)
    def health_page(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        """The pipeline's own dashboard — `/api/health` is the same data as JSON.

        A 30-day window rather than the digest's 7: this page is read to answer "is the
        dataset healthy", where one bad afternoon should not swing the headline number.
        """
        return TEMPLATES.TemplateResponse(
            request,
            "health.html",
            {
                "report": health_report(conn, config, window_days=30),
                "strip": capture_strip(conn, config),
                "latest": latest_observation(conn, config),
                "cadence": (
                    f"{config.capture.interval_minutes} min cadence"
                    if config.capture.scheduled
                    else "capture on demand"
                ),
                "preview": health_preview(request, config),
            },
        )

    @app.get("/api/query")
    def api_query(
        origin: str,
        service_date: str | None = None,
        time: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        _validate_origin(config, origin)
        target = _parse_date(service_date)
        times = sailing_times(config, origin, target)
        chosen = time or default_sailing_time(config, times, target)
        if not chosen:
            raise HTTPException(404, "no sailings scheduled from this terminal on that date")
        return query_distribution(
            conn, config, origin=origin, target_date=target, depart_hhmm=chosen
        ).to_dict()

    @app.get("/api/arrival-curve")
    def api_arrival_curve(
        origin: str,
        time: str,
        service_date: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        _validate_origin(config, origin)
        return arrival_curve(
            conn,
            config,
            origin=origin,
            target_date=_parse_date(service_date),
            depart_hhmm=time,
        )

    @app.get("/api/sailings")
    def api_sailings(
        origin: str | None = None,
        service_date: str | None = None,
        config: Config = Depends(get_config),
    ):
        if origin:
            _validate_origin(config, origin)
        if service_date:
            target = _parse_date(service_date)
            return {"date": target.isoformat(), "times": sailing_times(config, origin or config.route.codes[0], target)}
        return [
            {
                "origin": s.origin,
                "destination": s.destination,
                "service_date": s.service_date.isoformat(),
                "depart_hhmm": s.depart_hhmm,
                "day_type": s.day_type,
                "season": s.season,
            }
            for s in upcoming_sailings(config, origin=origin)
        ]

    @app.post("/api/check")
    def api_check(
        origin: str,
        service_date: str | None = None,
        time: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        """On-demand read of the current queue. Costs money, so it is opt-in and capped."""
        from ..status import check_and_compare

        if not config.web.allow_on_demand_checks:
            raise HTTPException(
                403,
                "on-demand checks are disabled; set [web] allow_on_demand_checks = true "
                "to enable them (each one spends a small amount on vision calls)",
            )
        _validate_origin(config, origin)

        today = date.today().isoformat()
        used_today = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE created_at >= ?", (today,)
        ).fetchone()[0]
        if used_today >= config.web.on_demand_daily_cap:
            raise HTTPException(
                429,
                f"daily on-demand cap of {config.web.on_demand_daily_cap} frames reached",
            )

        return check_and_compare(
            conn,
            config,
            origin=origin,
            target_date=_parse_date(service_date) if service_date else None,
            depart_hhmm=time,
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Crawlers and older browsers ask for this path regardless of the <link> tag."""
        return FileResponse(STATIC / "brand" / "favicon.ico", media_type="image/x-icon")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        """Liveness only — deliberately does no database work.

        A platform healthcheck must not fail just because no data has been collected yet,
        or the first deploy would be marked unhealthy before the first scrape.
        """
        return "ok"

    @app.get("/api/schedule")
    def api_schedule(
        conn: sqlite3.Connection = Depends(get_conn), config: Config = Depends(get_config)
    ):
        from ..scheduler import describe

        return describe(conn, config)

    @app.get("/api/health")
    def api_health(
        conn: sqlite3.Connection = Depends(get_conn), config: Config = Depends(get_config)
    ):
        report = health_report(conn, config)
        return {**report.__dict__, "healthy": report.healthy}

    @app.get("/export/{dataset}.{fmt}")
    def api_export(
        dataset: str,
        fmt: str,
        since: str | None = None,
        until: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            body = export(conn, dataset, fmt, since=since, until=until)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        media = "text/csv" if fmt == "csv" else "application/json"
        return PlainTextResponse(
            body,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{dataset}.{fmt}"'},
        )

    return app


app = None


def get_app() -> FastAPI:
    """Entry point for `uvicorn ferrycast.web.app:get_app --factory`."""
    return create_app()
