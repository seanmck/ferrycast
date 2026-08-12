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
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

from ..backfill import fill_for_slot, fill_offer
from ..config import Config, load_config
from ..db import connect
from ..export import export
from ..maintenance import capture_strip, health_report, latest_observation
from ..marine import summary as marine_summary
from ..query import (
    OUTCOME_LABELS,
    OUTCOME_LABELS_SHORT,
    arrival_curve,
    collection_status,
    comparable_report_bounds,
    day_board,
    default_sailing_time,
    query_distribution,
    sailing_times,
    upcoming_sailings,
)
from ..reports import (
    DECK_FULLNESS,
    ReportError,
    describe_reports,
    fetch_reports,
    parse_clock,
    submit_report,
)
from ..schedule import day_type, season
from ..shore import summary as shore_summary
from ..timeutil import combine_local, local, local_date, now_utc, parse_hhmm
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
    now = local(now_utc(), config.tz)
    minutes = int((departure - now).total_seconds() // 60)
    if minutes < 0:
        return None
    if minutes < 60:
        return f"in {minutes} min"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"in {hours} h" if rest == 0 else f"in {hours} h {rest} min"
    # Past a day out the unit is the calendar, not the stopwatch. Counting elapsed 24-hour
    # blocks called Wednesday's 05:35 "tomorrow" at 20:00 on Monday, because 33 hours is
    # one whole day and a bit — wrong whenever the departure's wall-clock time falls
    # earlier in the day than now, which on a morning timetable is most of the evening.
    # Subtracting dates in the route's zone is also the only form immune to DST: the
    # weekend the clocks go back, one of those "days" is 25 hours long.
    days = (service_date - now.date()).days
    return "tomorrow" if days == 1 else f"in {days} days"


# How the board says a made-it share out loud, in the status column the desktop layout has
# room for and the phone does not. Only ever applied to a row with a deep enough bucket
# behind it: "expect to wait" off two sailings is an editorial, not a finding.
OUTLOOK_ROOM = 0.70
OUTLOOK_TOSSUP = 0.45


def _outlook(row) -> str:
    if not row.sufficient or not row.n:
        return ""
    if row.boarded_share >= OUTLOOK_ROOM:
        return "room most days"
    if row.boarded_share >= OUTLOOK_TOSSUP:
        return "toss-up"
    return "expect to wait"


def _worst_of_day(rows) -> str | None:
    """The sailing to avoid, or None when the day has no clear one.

    Needs at least two answerable sailings to be a comparison at all, and a genuinely bad
    one to be worth flagging — labelling the least-good boat of an easy Tuesday "worst of
    day" would read as a warning about a sailing that boards nine times in ten.
    """
    answerable = [row for row in rows if row.sufficient and row.n]
    if len(answerable) < 2:
        return None
    worst = min(answerable, key=lambda row: row.boarded_share)
    if worst.boarded_share >= OUTLOOK_TOSSUP:
        return None
    # A tie is nobody's warning: two sailings equally bad means neither is "the" worst.
    if sum(1 for row in answerable if row.boarded_share == worst.boarded_share) > 1:
        return None
    return worst.depart_hhmm


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Index spans where `flags` is true, as (start, end) inclusive."""
    spans, start = [], None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            spans.append((start, i - 1))
            start = None
    if start is not None:
        spans.append((start, len(flags) - 1))
    return spans


def _day_shape(rows) -> dict:
    """Where the easy and hard sailings of a day actually sit.

    The board already shows this one row at a time; the value of saying it again in a
    sentence is that "before 07:35, after 19:05" is a plan and ten percentages are not.

    Only rows with a deep enough bucket count, and a day where too few sailings are
    answerable gets no shape at all — a "hardest stretch" drawn from two known sailings out
    of ten is a claim about the eight nobody has data for.

    The bar is a proportion rather than a count, because a count is really a guess at how
    long a timetable is: a route with three crossings a day would never clear an absolute
    of four, and its three would be the whole day. Three answerable sailings and half the
    timetable, whichever is the harder to meet.
    """
    answerable = [row for row in rows if row.sufficient and row.n]
    if len(answerable) < 3 or len(answerable) * 2 < len(rows):
        return {}

    known = [row.sufficient and row.n for row in rows]
    easy = [bool(k) and row.boarded_share >= OUTLOOK_ROOM for k, row in zip(known, rows, strict=True)]
    hard = [bool(k) and row.boarded_share < OUTLOOK_TOSSUP for k, row in zip(known, rows, strict=True)]

    shape: dict = {}

    # The easy sailings of a ferry day are usually its ends, so they are described as ends
    # when they are — "before 07:35" is how somebody actually plans — and simply listed when
    # they turn up in the middle instead.
    ends = []
    if easy and easy[0]:
        first_hard = next((i for i, e in enumerate(easy) if not e), None)
        if first_hard:
            ends.append(f"before {rows[first_hard].depart_hhmm}")
    if easy and easy[-1]:
        last_hard = next((i for i in range(len(easy) - 1, -1, -1) if not easy[i]), None)
        if last_hard is not None and last_hard < len(easy) - 1:
            ends.append(f"after {rows[last_hard].depart_hhmm}")
    if ends:
        shape["easiest"] = ", ".join(ends)
    elif any(easy):
        shape["easiest"] = ", ".join(
            rows[i].depart_hhmm for i, e in enumerate(easy) if e
        )

    # The longest unbroken run of sailings people usually wait for. A single bad sailing is
    # a row on the board, not a stretch of the day.
    spans = _runs(hard)
    if spans:
        start, end = max(spans, key=lambda s: s[1] - s[0])
        if end > start:
            shape["hardest"] = f"{rows[start].depart_hhmm}–{rows[end].depart_hhmm}"
        elif len(spans) == 1:
            shape["hardest"] = rows[start].depart_hhmm
    return shape


def _board(
    conn: sqlite3.Connection,
    config: Config,
    *,
    origin: str,
    service_date: date,
    selected_time: str | None,
    next_out,
) -> tuple[list[dict], dict]:
    """The whole day's timetable, each sailing answered from its own history.

    This is the desktop view's premise: with room for ten rows the question stops being
    "what about the 15:25" — which the phone answers one page load at a time — and becomes
    "which boat should I take", which no single-sailing page can answer at all.

    Every row is a link to the page it already had, so the board needs no script and the
    detail beside it is rendered by exactly the code that renders it on a phone.

    Returns the rows and the shape of the day they make, which the strip above the board
    states in a sentence. Both come off the one `day_board` call — the shape is a reading of
    the same rows, not a second query.
    """
    rows = day_board(conn, config, origin=origin, target_date=service_date)
    if not rows:
        return [], {}

    now = local(now_utc(), config.tz)
    is_today = service_date == now.date()
    worst = _worst_of_day(rows)
    last_hhmm = rows[-1].depart_hhmm

    board = []
    for row in rows:
        departed = combine_local(service_date, parse_hhmm(row.depart_hhmm), config.tz) <= now
        # Only today has a next departure worth counting down to. On any other date the
        # countdown is a number of hours nobody is acting on, and the outlook — what this
        # sailing usually does — is the column's real job.
        is_next = bool(
            is_today
            and next_out
            and next_out.service_date == service_date
            and next_out.depart_hhmm == row.depart_hhmm
        )

        if departed:
            status, tone = "sailed", "past"
        elif is_next:
            status, tone = (_countdown(config, service_date, row.depart_hhmm) or ""), "next"
        elif row.depart_hhmm == worst:
            status, tone = "worst of day", "worst"
        elif row.depart_hhmm == last_hhmm:
            status, tone = "last boat", ""
        else:
            status, tone = _outlook(row), ""

        # The row's own sentence, for anyone who reaches it as a link rather than as a
        # line in a grid. The column headings are decoration to a screen reader — they
        # label cells it is never told about — so each row has to say what it is.
        if row.n:
            summary = (
                f"{row.depart_hhmm}, {round(row.boarded_share * 100)}% got on across "
                f"{row.n} comparable sailing{'' if row.n == 1 else 's'}"
            )
            if row.arrive_by:
                summary += f", typically full by {row.arrive_by}"
        else:
            summary = f"{row.depart_hhmm}, no comparable history yet"
        if status:
            summary += f" — {status}"

        board.append(
            {
                "time": row.depart_hhmm,
                "summary": summary,
                "n": row.n,
                "counts": row.counts,
                "boarded_pct": round(row.boarded_share * 100),
                "arrive_by": row.arrive_by,
                "relaxed": row.relaxed,
                "status": status,
                "tone": tone,
                "departed": departed,
                "selected": row.depart_hhmm == selected_time,
                # `safe=":"` keeps the departure readable as 16:30 rather than 16%3A30.
                # A colon is legal in a query string, and on this layout the row link is
                # the thing somebody copies out of the address bar to send to a passenger.
                "href": "/?"
                + urlencode(
                    {
                        "origin": origin,
                        "service_date": service_date.isoformat(),
                        "time": row.depart_hhmm,
                    },
                    safe=":",
                ),
            }
        )

    return board, _day_shape(rows)


def _now_index(board: list[dict]) -> int | None:
    """Which row the board's "now" rule sits above.

    None unless the day is genuinely part-run: before the first sailing the rule would sit
    at the top marking nothing, and after the last one there is no row left to sit above.
    """
    if not board or not board[0]["departed"]:
        return None
    return next((i for i, row in enumerate(board) if not row["departed"]), None)


# How long a browser may reuse the camera image. The page is server-rendered, so the URL
# carries a bucket number that changes on this cadence: a reload a minute later fetches a
# fresh frame, while a reload ten seconds later costs BC Ferries nothing.
WEBCAM_FRESHNESS_SECONDS = 60


def _live_webcam(
    config: Config, origin: str, service_date: date, depart_hhmm: str | None
) -> dict | None:
    """The terminal camera, but only when the chosen sailing is the next one out.

    A live picture is an answer to "should I leave now", which is a question only the next
    departure poses. Attached to a sailing next Tuesday it would be actively misleading —
    the queue in the frame is not that sailing's queue, and a photograph is persuasive in a
    way a caption cannot undo.
    """
    if not depart_hhmm:
        return None
    terminal = config.route.terminal(origin)
    if not terminal.webcam_url:
        return None

    upcoming = upcoming_sailings(config, origin=origin, limit=1)
    if not upcoming:
        return None
    next_out = upcoming[0]
    if next_out.service_date != service_date or next_out.depart_hhmm != depart_hhmm:
        return None

    separator = "&" if "?" in terminal.webcam_url else "?"
    bucket = int(now_utc().timestamp()) // WEBCAM_FRESHNESS_SECONDS
    return {
        "url": f"{terminal.webcam_url}{separator}t={bucket}",
        "terminal": terminal.name,
    }


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

    def _parse_date(config: Config, value: str | None) -> date:
        if not value:
            # The route's today, not the server's. This box runs on UTC, so `date.today()`
            # rolled over to tomorrow at 5pm on the coast — every date-less request through
            # a Sunshine Coast evening was answered about the following day's timetable.
            return local_date(now_utc(), config.tz)
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(400, f"bad date {value!r}; expected YYYY-MM-DD") from exc

    def _board_when(
        conn: sqlite3.Connection, config: Config, origin: str, day: date, hhmm: str | None
    ):
        """When the board said this sailing actually left, or None while it has not said.

        The datetime rather than a clock string: the header needs it against the schedule
        to say how late the boat was, and the report form needs it as HH:MM — one lookup
        serves both.
        """
        if not hhmm:
            return None
        from ..aggregate import _board_departure

        return _board_departure(conn, config.route.id, origin, day.isoformat(), hhmm, config)

    def _validate_origin(config: Config, origin: str) -> str:
        if origin not in config.route.codes:
            raise HTTPException(
                400, f"unknown terminal {origin!r}; expected one of {', '.join(config.route.codes)}"
            )
        return origin

    def _render_index(
        request: Request,
        conn: sqlite3.Connection,
        config: Config,
        origin: str | None,
        service_date: str | None,
        time: str | None,
        *,
        report_error: str | None = None,
        report_values: dict | None = None,
        report_saved: bool = False,
        fill_result: dict | None = None,
        status_code: int = 200,
    ):
        upcoming = upcoming_sailings(config, origin=origin, limit=1)
        default = upcoming[0] if upcoming else None

        chosen_origin = _validate_origin(
            config, origin or (default.origin if default else config.route.codes[0])
        )
        chosen_date = _parse_date(
            config, service_date or (default.service_date.isoformat() if default else None)
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

        # The same two bounds `describe_reports` gives for one sailing, pooled across the
        # comparable ones so they survive onto a date nobody has reported on yet. Passed
        # the deck-space cutline, which decides whether the turned-away bound has anything
        # to add — see `comparable_report_bounds`.
        line_bounds = None
        if chosen_time:
            line_bounds = comparable_report_bounds(
                conn,
                config,
                origin=chosen_origin,
                target_date=chosen_date,
                depart_hhmm=chosen_time,
                cutline_minutes_before=(
                    distribution["typical_fill_minutes_before"] if distribution else None
                ),
            )

        kind = DAY_TYPE_SHORT.get(day_type(chosen_date), day_type(chosen_date))
        bucket = SEASON_SHORT.get(season(chosen_date), season(chosen_date))

        # What people in the line said about this exact sailing. Only a sailing that has
        # gone can be reported on, so the form appears once it has departed and the panel
        # explains itself before then rather than vanishing.
        reports = None
        departed = False
        board_when = _board_when(conn, config, chosen_origin, chosen_date, chosen_time)
        # What the header says once the vessel has gone. The board's time gets the full
        # mirrored block only when it differs from the timetable — repeating the same
        # number would say nothing, so an on-time departure is a word in the countdown's
        # slot instead, as is a departed sailing the board never published a time for.
        # Nothing changes before the scheduled time: a boat the board says left early is
        # still the countdown's business until the timetable agrees it has gone.
        actual_departure = None
        departed_note = None
        if chosen_time:
            departure = combine_local(chosen_date, parse_hhmm(chosen_time), config.tz)
            departed = departure <= local(now_utc(), config.tz)
            reports = describe_reports(
                config,
                fetch_reports(
                    conn,
                    config.route.id,
                    chosen_origin,
                    chosen_date.isoformat(),
                    chosen_time,
                ),
                departure,
            )
            if departed and board_when is None:
                departed_note = "departed"
            elif departed:
                off_schedule = int((board_when - departure).total_seconds() // 60)
                if off_schedule == 0:
                    departed_note = "left on time"
                else:
                    actual_departure = {
                        "hhmm": board_when.astimezone(config.tz).strftime("%H:%M"),
                        "qual": (
                            f"{off_schedule} min late"
                            if off_schedule > 0
                            else f"{-off_schedule} min early"
                        ),
                    }

        # The live camera, shown only for the sailing you could still catch — the next
        # departure from this terminal. On any other sailing the picture answers a
        # question nobody asked: a compound full of cars has nothing to do with a
        # crossing three days out, and everything to do with being mistaken for it.
        webcam = _live_webcam(config, chosen_origin, chosen_date, chosen_time)

        # The desktop board. Built for every render because it is the desktop layout's
        # navigation and its first paint has to carry the answer, same as the phone's —
        # a day's worth of rows costs three queries, not one per sailing.
        board, day_shape = _board(
            conn,
            config,
            origin=chosen_origin,
            service_date=chosen_date,
            selected_time=chosen_time,
            next_out=default,
        )
        today = local_date(now_utc(), config.tz)

        # What ECCC says about the water on this date. Wind is the one thing that cancels a
        # sailing outright and the only condition on this page FerryCast does not observe
        # for itself, so it is quoted rather than summarised, and a warning is shown on any
        # date — including today's board, where withholding one would be indefensible.
        marine = marine_summary(conn, config, service_date=chosen_date)

        # And what it will be doing ashore, at the terminal you are leaving from. A
        # separate ECCC product about a separate place, so it is carried separately and
        # attributed to its own station rather than folded in beside the wind.
        shore = shore_summary(
            conn, config, terminal=chosen_origin, service_date=chosen_date
        )

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
                # ---- The desktop board and its chrome. Everything below is read only by
                # the wide layout; the phone's markup does not reference any of it.
                "board": board,
                "day_shape": day_shape,
                "marine": marine,
                "shore": shore,
                "now_index": _now_index(board),
                "board_totals": {
                    "sailings": len(board),
                    "records": sum(row["n"] for row in board),
                    # Whether any row had to widen its search. The board has no room to say
                    # which rung a row used, so it marks the count and the footer explains
                    # the mark — the panel beside it spells out the widening in full.
                    "relaxed": sum(1 for row in board if row["relaxed"]),
                },
                "is_today": chosen_date == today,
                "now_hhmm": local(now_utc(), config.tz).strftime("%H:%M"),
                # Plain prev/next links rather than a control: the board is a whole day, so
                # moving between days is the navigation, and it has to work without script.
                "prev_date": (chosen_date - timedelta(days=1)).isoformat(),
                "next_date": (chosen_date + timedelta(days=1)).isoformat(),
                "countdown": (
                    _countdown(config, chosen_date, chosen_time) if chosen_time else None
                ),
                # Shown when there is no distribution to show. A new install is correctly
                # blank for weeks, which is indistinguishable from a broken scraper unless
                # the page says what it has collected — for *this* sailing, not for the
                # route at large, or every page carries the same list and none of them
                # is about the sailing on screen.
                "collection": (
                    collection_status(
                        conn,
                        config,
                        origin=chosen_origin,
                        target_date=chosen_date,
                        depart_hhmm=chosen_time,
                    )
                    if chosen_time and not (distribution and distribution["n"])
                    else None
                ),
                "preview": index_preview(request, config),
                # What filling this slot's history would cost, when there is anything worth
                # filling. None hides the button entirely — including when the feature is
                # off, so a disabled deployment never shows a control that would 403.
                "fill_offer": (
                    fill_offer(
                        conn,
                        config,
                        origin=chosen_origin,
                        target_date=chosen_date,
                        depart_hhmm=chosen_time,
                        distribution=distribution,
                    )
                    if chosen_time and config.web.allow_on_demand_backfill
                    else None
                ),
                "fill_result": fill_result,
                # The board publishes the actual departure for *today's* sailings only, so
                # this pre-fills the field rather than replacing it: report on last Friday
                # and there is nothing to read, and only the person was there.
                "board_departed": (
                    board_when.astimezone(config.tz).strftime("%H:%M") if board_when else ""
                ),
                "actual_departure": actual_departure,
                "departed_note": departed_note,
                "line_bounds": line_bounds,
                "reports": reports,
                "webcam": webcam,
                "has_departed": departed,
                "fullness_options": DECK_FULLNESS,
                "report_error": report_error,
                "report_form": report_values or {},
                "report_saved": report_saved,
            },
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        origin: str | None = None,
        service_date: str | None = None,
        time: str | None = None,
        reported: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        return _render_index(
            request,
            conn,
            config,
            origin,
            service_date,
            time,
            report_saved=reported == "1",
        )

    def _store_report(
        conn: sqlite3.Connection,
        config: Config,
        *,
        origin: str,
        service_date: str,
        time: str,
        boarded: str,
        joined: str = "",
        departed: str = "",
        sailings_waited: str = "",
        deck_fullness: str = "",
    ) -> int:
        """Everything the two submission paths agree on, in one place."""
        try:
            day = date.fromisoformat(service_date)
        except ValueError as exc:
            raise ReportError(f"bad date {service_date!r}; expected YYYY-MM-DD") from exc
        if boarded not in ("yes", "no"):
            raise ReportError("say whether you got on")
        return submit_report(
            conn,
            config,
            origin=origin,
            service_date=day,
            depart_hhmm=time,
            boarded=boarded == "yes",
            joined=parse_clock(joined),
            departed=parse_clock(departed),
            sailings_waited=int(sailings_waited) if sailings_waited else None,
            deck_fullness=deck_fullness or None,
        )

    @app.post("/report", response_class=HTMLResponse)
    def post_report(
        request: Request,
        origin: str = Form(...),
        service_date: str = Form(...),
        time: str = Form(...),
        # Not `Form(...)`: an unanswered radio group is the one mistake a submitter can
        # actually make here, and it deserves the page's own message rather than a 422.
        boarded: str = Form(""),
        joined: str = Form(""),
        departed: str = Form(""),
        sailings_waited: str = Form(""),
        deck_fullness: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        """A sailing report from the page. Post/redirect/get, so a refresh cannot file it twice."""
        try:
            _store_report(
                conn,
                config,
                origin=origin,
                service_date=service_date,
                time=time,
                boarded=boarded,
                joined=joined,
                departed=departed,
                sailings_waited=sailings_waited,
                deck_fullness=deck_fullness,
            )
        except ReportError as exc:
            # Back to the same page with the form still filled in — a rejected report that
            # loses what was typed is a report that never gets filed again.
            return _render_index(
                request,
                conn,
                config,
                origin,
                service_date,
                time,
                report_error=str(exc),
                report_values={
                    "boarded": boarded,
                    "joined": joined,
                    "departed": departed,
                    "sailings_waited": sailings_waited,
                    "deck_fullness": deck_fullness,
                },
                status_code=400,
            )

        query = urlencode(
            {"origin": origin, "service_date": service_date, "time": time, "reported": "1"}
        )
        return RedirectResponse(f"/?{query}#report", status_code=303)

    def _reports_payload(
        conn: sqlite3.Connection, config: Config, origin: str, service_date: str, time: str
    ) -> dict:
        _validate_origin(config, origin)
        target = _parse_date(config, service_date)
        try:
            departure = combine_local(target, parse_hhmm(time), config.tz)
        except ValueError as exc:
            raise HTTPException(400, f"bad time {time!r}; expected HH:MM") from exc
        rows = fetch_reports(conn, config.route.id, origin, target.isoformat(), time)
        return describe_reports(config, rows, departure) or {"n": 0, "entries": []}

    @app.post("/fill", response_class=HTMLResponse)
    def post_fill(
        request: Request,
        origin: str = Form(...),
        service_date: str = Form(...),
        time: str = Form(...),
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        """Read the archived frames that answer this slot. Spends money, so it is guarded.

        Renders the result rather than redirecting: the point of the tap is to see what it
        bought, and a redirect would drop the "$0.02, 5 sailings" line the person just paid
        for. Re-submitting is harmless — extraction is idempotent, and a second run finds
        nothing to fill.
        """
        if not config.web.allow_on_demand_backfill:
            raise HTTPException(
                403,
                "on-demand backfill is disabled; set [web] allow_on_demand_backfill = true "
                "to enable it (each fill spends a small amount on vision calls)",
            )
        _validate_origin(config, origin)
        target = _parse_date(config, service_date)

        spent_today = _frames_read_today(conn, config)
        remaining = config.web.backfill_daily_frame_cap - spent_today
        if remaining <= 0:
            result = {
                "capped": True,
                "message": (
                    f"the daily limit of {config.web.backfill_daily_frame_cap} frames is "
                    "already used; the CLI is not capped"
                ),
            }
        else:
            outcome = fill_for_slot(
                conn,
                config,
                origin=origin,
                target_date=target,
                depart_hhmm=time,
                # Never let one tap spend more than the day has left.
                max_sailings=max(1, remaining // max(1, len(config.vision.essential_offsets_minutes))),
            )
            result = outcome.to_dict()

        return _render_index(
            request, conn, config, origin, service_date, time, fill_result=result
        )

    def _frames_read_today(conn: sqlite3.Connection, config: Config) -> int:
        """Frames read today, by any path.

        Counts frames rather than taps, so one expensive slot cannot exhaust the day while
        a cheap one barely registers. Deliberately not scoped to the web path: this is a
        spend cap, and money spent from the CLI is the same money. The CLI itself stays
        uncapped — someone with shell access can already edit the config.
        """
        today = local_date(now_utc(), config.tz).isoformat()
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM observations WHERE created_at >= ?", (today,)
            ).fetchone()[0]
            or 0
        )

    @app.post("/api/report")
    def api_report(
        origin: str,
        service_date: str,
        time: str,
        boarded: bool,
        joined: str = "",
        departed: str = "",
        sailings_waited: str = "",
        deck_fullness: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        try:
            report_id = _store_report(
                conn,
                config,
                origin=origin,
                service_date=service_date,
                time=time,
                boarded="yes" if boarded else "no",
                joined=joined,
                departed=departed,
                sailings_waited=sailings_waited,
                deck_fullness=deck_fullness,
            )
        except ReportError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": report_id, **_reports_payload(conn, config, origin, service_date, time)}

    @app.get("/api/reports")
    def api_reports(
        origin: str,
        service_date: str,
        time: str,
        conn: sqlite3.Connection = Depends(get_conn),
        config: Config = Depends(get_config),
    ):
        return _reports_payload(conn, config, origin, service_date, time)

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
        target = _parse_date(config, service_date)
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
            target_date=_parse_date(config, service_date),
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
            target = _parse_date(config, service_date)
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
            target_date=_parse_date(config, service_date) if service_date else None,
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
