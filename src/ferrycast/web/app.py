"""R5 — the "day like today" web app.

The page is server-rendered with the next departure already answered, so the first paint
carries the answer rather than waiting on a round trip. Everything is inline: one request,
no CDN, no fonts to fetch — it has to work on a phone with one bar at the side of Highway 101.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from ..config import Config, load_config
from ..db import connect
from ..export import export
from ..maintenance import health_report
from ..query import (
    OUTCOME_LABELS,
    arrival_curve,
    query_distribution,
    sailing_times,
    upcoming_sailings,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(config_path: str | None = None) -> FastAPI:
    config = load_config(config_path)
    app = FastAPI(title="FerryCast", docs_url="/api/docs", redoc_url=None)
    app.state.config = config

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
        chosen_time = time or (default.depart_hhmm if default and not origin else None)
        if chosen_time not in times:
            chosen_time = times[0] if times else None

        distribution = None
        if chosen_time:
            distribution = query_distribution(
                conn,
                config,
                origin=chosen_origin,
                target_date=chosen_date,
                depart_hhmm=chosen_time,
            ).to_dict()

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
        chosen = time or (times[0] if times else None)
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
