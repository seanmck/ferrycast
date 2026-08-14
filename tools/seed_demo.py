#!/usr/bin/env python3
"""Fill a database with a plausible season of collected data, for looking at the app.

FerryCast is correctly blank for weeks after it is installed — the record it answers from
is one it has to watch happen. That makes the interesting states of the UI (a deep bucket,
an arrival curve, the bounds from the line, a full pipeline page) impossible to see until
a season has gone by, which is a bad way to review a design decision.

So this writes the *evidence*, not the answers. It lays down deck-space rows, camera
frames and their lane readings, forecasts and first-hand reports exactly where the real
collectors would have put them, then runs the real `aggregate` over the lot. Every outcome
on screen is therefore derived by the same code that will derive the real ones: if the
classifier is wrong, this is wrong the same way, which is the only version of this script
worth having.

    python tools/seed_demo.py --reset

Nothing here talks to BC Ferries, ECCC or the vision model, and nothing it writes is
represented to the user as real: the database it fills is a local file the app is pointed
at by hand. Delete it and the app is a fresh install again.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ferrycast.aggregate import aggregate_range
from ferrycast.config import Config, load_config
from ferrycast.db import init_db
from ferrycast.holidays import holiday_name, is_long_weekend
from ferrycast.lanes import MODEL as LANE_MODEL
from ferrycast.lanes import PROMPT_VERSION as LANE_PROMPT_VERSION
from ferrycast.schedule import (
    day_type,
    load_schedule_cached,
    sailings_for_day,
    season,
)
from ferrycast.timeutil import combine_local, iso, local, now_utc, parse_hhmm

VESSEL = "Malaspina Sky"

# The board's own wording for a deck loaded to capacity. Matched by `notice_says_full`,
# so it has to stay verbatim.
FULL_NOTE = "Loading maximum number of vehicles"

# How demand splits. These are shapes, not measurements: a summer Friday is the worst
# sailing of the week and a February Tuesday is empty, and everything else lies between.
DAY_FACTOR = {"weekday": 0.60, "friday": 0.98, "saturday": 0.92, "sunday_holiday": 1.00}
SEASON_FACTOR = {"winter": 0.52, "shoulder": 0.76, "peak_summer": 1.00}

# The two ends of the crossing do not fill on the same days. Northbound (out of Earls
# Cove, towards Powell River) is the Friday-evening exodus; southbound is Sunday's return.
DIRECTION_FACTOR = {
    ("friday", "ERL"): 1.14,
    ("friday", "SLT"): 0.90,
    ("saturday", "ERL"): 1.05,
    ("sunday_holiday", "SLT"): 1.16,
    ("sunday_holiday", "ERL"): 0.88,
}

# Lanes visible to each camera. Saltery Bay looks down a lane grid; Earls Cove faces the
# approach road and can resolve fewer of them, which is why the calibration is per
# terminal and the two are not interchangeable.
LANES = {"SLT": 8, "ERL": 6}

FULLNESS_BANDS = (
    (0.02, "empty"),
    (0.28, "light"),
    (0.58, "moderate"),
    (0.92, "heavy"),
    (99.0, "overflowing"),
)

# What ECCC was saying. Weighted so that most days are unremarkable, a strong-wind day
# turns up most weeks, and a gale is a winter event — which is what makes the page's
# "no sailing cancelled in these conditions" claim worth anything when it appears.
WIND_BANDS = [
    ("light", 0.70, (5, 15), ["Wind light.", "Wind northwest {kt} knots.", "Wind variable {kt} knots."]),
    ("light", 0.00, (5, 15), []),
    ("strong", 0.22, (22, 33), ["Wind southeast {kt} knots.", "Wind northwest {kt} knots."]),
    ("gale", 0.07, (35, 48), ["Wind southeast {kt} knots.", "Wind southeast {kt} knots diminishing to 25 near midnight."]),
]

SHORE_CONDITIONS = [
    ("Sunny", 0, (19, 27)),
    ("A mix of sun and cloud", 10, (16, 23)),
    ("Cloudy", 20, (13, 20)),
    ("Showers", 70, (11, 17)),
    ("Rain", 90, (9, 15)),
]

# Manual event tags. The auto ones (long weekends, stat holidays) come from the calendar
# in `schedule.day_tags`; these are the local knowledge nobody's calendar has.
MANUAL_TAGS = [
    ("2026-05-16", "event", "Powell River Blackberry Festival weekend"),
    ("2026-06-20", "event", "Sunshine Coast Art Crawl"),
    ("2026-07-04", "event", "Kathaumixw choral festival — first departures"),
    ("2026-07-25", "event", "Powell River Logger Sports"),
    ("2026-08-08", "closure", "Highway 101 single-lane south of Earls Cove"),
]


def band_for(ratio: float) -> str:
    for limit, name in FULLNESS_BANDS:
        if ratio < limit:
            return name
    return "overflowing"


def arrived_share(minutes_before: float) -> float:
    """Share of a sailing's demand already in the compound, `minutes_before` departure.

    Convex on purpose: a ferry queue is not a steady trickle. Half of it turns up in the
    last forty minutes, which is exactly why the "arrive before" time is worth publishing
    and an average wait would not be.
    """
    if minutes_before >= 120:
        return 0.0
    if minutes_before <= 0:
        return 1.0
    return ((120 - minutes_before) / 120) ** 1.35


class Simulator:
    """One route's season, as the collectors would have recorded it."""

    def __init__(self, conn: sqlite3.Connection, config: Config, rng: random.Random):
        self.conn = conn
        self.config = config
        self.rng = rng
        self.route = config.route.id
        self.capacity = config.aggregate.vessel_capacity
        self.blocks = load_schedule_cached(config.schedule_path)
        self.destinations = config.route.destinations
        # Per-date wind band, decided once so the forecast, the cancellations and the
        # weather panel are all telling the same story.
        self.wind: dict[date, tuple[str, int, str]] = {}
        # (terminal, date) -> list of sailing dicts, so frames and deck space are generated
        # from one demand model rather than two that could disagree.
        self.days: dict[date, list[dict]] = {}

    # ---------------------------------------------------------------- the demand model

    def wind_for(self, day: date) -> tuple[str, int, str]:
        if day not in self.wind:
            weights = []
            for band, weight, _, _ in WIND_BANDS:
                if not weight:
                    continue
                # Gales belong to the winter half of the year; a July gale in the Strait
                # would be a story, not a Tuesday.
                if band == "gale" and season(day) == "peak_summer":
                    weight *= 0.15
                elif band == "strong" and season(day) == "peak_summer":
                    weight *= 0.5
                weights.append((band, weight))
            total = sum(w for _, w in weights)
            pick = self.rng.random() * total
            band = weights[-1][0]
            for name, weight in weights:
                pick -= weight
                if pick <= 0:
                    band = name
                    break
            low, high = next(r for b, w, r, _ in WIND_BANDS if b == band and w)
            knots = self.rng.randint(low, high)
            phrasings = next(p for b, w, _, p in WIND_BANDS if b == band and w)
            wind = self.rng.choice(phrasings).format(kt=knots)
            self.wind[day] = (band, knots, wind)
        return self.wind[day]

    def demand(self, day: date, origin: str, depart_hhmm: str) -> float:
        """Vehicle-equivalents wanting on one sailing."""
        kind = day_type(day)
        hour = int(depart_hhmm[:2]) + int(depart_hhmm[3:]) / 60

        factor = DAY_FACTOR[kind] * SEASON_FACTOR[season(day)]
        factor *= DIRECTION_FACTOR.get((kind, origin), 1.0)
        # Mid-afternoon is the crush; the first and last boats of the day are not.
        factor *= 0.42 + 0.80 * math.exp(-(((hour - 14.0) / 4.4) ** 2))
        if hour < 8:
            factor *= 0.75
        if is_long_weekend(day):
            factor *= 1.22
        if holiday_name(day):
            factor *= 1.08
        # Day-to-day weather and whatever else the model cannot see.
        factor *= math.exp(self.rng.gauss(0.0, 0.17))
        return max(3.0, self.capacity * 1.02 * factor)

    def build_day(self, day: date) -> list[dict]:
        """Every sailing on `day`, with the numbers both collectors will report."""
        if day in self.days:
            return self.days[day]

        band, _, _ = self.wind_for(day)
        # A gale cancels a run of sailings rather than a scattering of them: the wind does
        # not pick boats out of a timetable.
        cancelled_from = None
        if band == "gale" and self.rng.random() < 0.55:
            cancelled_from = self.rng.choice([9, 13, 16])

        sailings = []
        carry_in = {code: 0.0 for code in self.config.route.codes}
        for sailing in sailings_for_day(
            self.blocks, day, self.route, self.destinations, self.config.tz
        ):
            origin = sailing.origin
            hour = int(sailing.depart_hhmm[:2])
            cancelled = cancelled_from is not None and hour >= cancelled_from

            # What this sailing inherited from the one before it. Read before the running
            # total is advanced, because the compound the camera sees starts here.
            inherited = carry_in[origin]
            demand = 0.0 if cancelled else self.demand(day, origin, sailing.depart_hhmm)
            waiting = inherited + demand
            if cancelled:
                # Nobody boards, and everybody stays for the next one — which is what makes
                # a cancelled sailing worse than a full one for the rest of the day.
                boarded, residual = 0.0, waiting
            else:
                boarded = min(waiting, self.capacity)
                residual = waiting - boarded
            carry_in[origin] = residual

            # A busy sailing leaves late: loading a full deck takes longer than loading
            # half of one, and the board publishes the minute it actually went.
            lateness = 0
            if not cancelled:
                pressure = waiting / self.capacity
                lateness = max(0, int(self.rng.gauss(4 + 9 * max(0.0, pressure - 0.8), 4)))

            sailings.append(
                {
                    "origin": origin,
                    "depart_hhmm": sailing.depart_hhmm,
                    "departure": sailing.scheduled_departure,
                    "actual": sailing.scheduled_departure + timedelta(minutes=lateness),
                    "lateness": lateness,
                    "carry_in": inherited,
                    "demand": demand,
                    "waiting": waiting,
                    "boarded": boarded,
                    "residual": residual,
                    "cancelled": cancelled,
                    "filled": (not cancelled) and waiting >= self.capacity,
                }
            )
        self.days[day] = sailings
        return sailings

    # ------------------------------------------------------------------- deck space (R2)

    def write_deck_space(self, day: date) -> int:
        """The published conditions page, scraped on the quarter hour.

        Percentages are written here even though this route's real board has never
        published one — the parser and the classifier both handle them, and a seeded
        database that omitted them could not show the arrival curve or the "arrive before"
        time that the percentage is what produces.
        """
        rows = []
        for s in self.build_day(day):
            departure = s["departure"]
            grid = departure.replace(minute=(departure.minute // 15) * 15, second=0, microsecond=0)
            observed = grid - timedelta(minutes=120)
            while observed <= departure + timedelta(minutes=30):
                minutes_before = (departure - observed).total_seconds() / 60
                after = minutes_before < 0

                if s["cancelled"]:
                    rows.append((observed, None, "cancelled", None))
                    observed += timedelta(minutes=15)
                    continue

                queued = s["carry_in"] + s["demand"] * arrived_share(minutes_before)
                percent = max(0, int(round(100 * (1 - queued / self.capacity))))
                percent = min(100, percent)

                note = None
                if after:
                    # The capacity notice and the departure time both appear on the board
                    # only once the vessel has gone.
                    note = FULL_NOTE if s["filled"] else None
                departed = (
                    local(s["actual"], self.config.tz).strftime("%H:%M") if after else None
                )
                # Once the vessel has gone the board stops quoting space aboard it and
                # publishes the departure time and the operator's note instead. Which is
                # why a sailing can be known to have filled with no zero ever recorded:
                # the deck went from "2%" to gone, and only the notice says what happened
                # in between. Those are exactly the sailings where the bounds from the
                # line are the only arrival guidance there is.
                rows.append((observed, None if after else percent, note, departed))
                observed += timedelta(minutes=15)

            self.conn.executemany(
                """INSERT OR IGNORE INTO deck_space
                       (route, terminal, observed_at, service_date, sailing_hhmm,
                        percent_available, vessel, status_text, departed_hhmm, fetch_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')""",
                [
                    (
                        self.route,
                        s["origin"],
                        iso(observed),
                        day.isoformat(),
                        s["depart_hhmm"],
                        percent,
                        VESSEL,
                        note,
                        departed,
                    )
                    for observed, percent, note, departed in rows
                ],
            )
            rows = []
        return 1

    # ----------------------------------------------------------- frames and lanes (R1/R3)

    def compound(self, terminal: str, day: date, moment: datetime) -> float | None:
        """How many vehicle-equivalents are standing in the compound at `moment`.

        The compound is not a queue per sailing — it is one tarmac that fills and empties —
        so this is built from whichever departure is next, and drops to that sailing's
        residual as the vessel loads. The drop is a single frame wide on purpose: across
        the sailings read by hand, a packed compound went to bare asphalt between one frame
        and the next, and that transition is the whole outcome.
        """
        day_sailings = [s for s in self.build_day(day) if s["origin"] == terminal]
        for s in day_sailings:
            # Loading starts before the advertised time; by five minutes out the tarmac is
            # already down to whatever will not fit.
            clears_at = s["actual"] - timedelta(minutes=5)
            if moment >= clears_at:
                continue
            if s["cancelled"]:
                # Nothing loads, so the compound simply keeps filling.
                return s["carry_in"] + s["demand"] * arrived_share(
                    (s["departure"] - moment).total_seconds() / 60
                )
            minutes_before = (s["departure"] - moment).total_seconds() / 60
            return s["carry_in"] + s["demand"] * arrived_share(minutes_before)
        return day_sailings[-1]["residual"] if day_sailings else 0.0

    def write_frames(self, day: date, outage: set[tuple[str, date]]) -> int:
        """One frame per camera every `interval_minutes`, and the free lane reading of it.

        Lane geometry costs nothing and runs on every capture, which is why the archive is
        eager and the vision model is not. Night frames are still captured and still
        stored — they are marked unusable rather than skipped, so the gap is visible in the
        data instead of being silently absent from it.
        """
        interval = self.config.capture.interval_minutes
        written = 0
        for terminal in self.config.route.codes:
            broken = (terminal, day) in outage
            frames = []
            observations = []
            moment = combine_local(day, parse_hhmm("00:00"), self.config.tz)
            end = moment + timedelta(days=1)
            while moment < end:
                if moment > now_utc():
                    break
                hour = local(moment, self.config.tz).hour
                # A camera outage is a run of error rows, not a hole: a gap you cannot see
                # is indistinguishable from a night nobody drove.
                if broken and 8 <= hour < 17:
                    frames.append(
                        (
                            self.route,
                            terminal,
                            iso(moment),
                            day.isoformat(),
                            None,
                            None,
                            "error",
                            "connection timed out after 20.0s",
                        )
                    )
                    moment += timedelta(minutes=interval)
                    continue

                path = f"{terminal}/{day.isoformat()}/{iso(moment).replace(':', '')}.jpg"
                frames.append(
                    (
                        self.route,
                        terminal,
                        iso(moment),
                        day.isoformat(),
                        path,
                        self.rng.randint(28_000, 46_000),
                        "ok",
                        None,
                    )
                )
                # Dark, or the lens is streaming with rain. Either way geometry refuses to
                # report rather than reporting a bare compound it cannot actually see.
                dark = hour >= 22 or hour < 5
                weather_out = self.wind_for(day)[0] == "gale" and self.rng.random() < 0.12
                queued = self.compound(terminal, day, moment) or 0.0
                ratio = max(0.0, queued / self.capacity)
                observations.append(
                    (
                        iso(moment),
                        terminal,
                        min(LANES[terminal], int(round(LANES[terminal] * min(1.0, ratio)))),
                        int(ratio > 1.15),
                        "obscured" if (dark or weather_out) else "clear",
                        band_for(ratio),
                        round(self.rng.uniform(0.78, 0.95), 2),
                        0 if (dark or weather_out) else 1,
                    )
                )
                moment += timedelta(minutes=interval)

            self.conn.executemany(
                """INSERT OR IGNORE INTO frames
                       (route, terminal, captured_at, service_date, path, bytes,
                        width, height, status, error)
                   VALUES (?, ?, ?, ?, ?, ?, 320, 240, ?, ?)""",
                frames,
            )
            for captured_at, term, lanes, beyond, visibility, fullness, conf, usable in observations:
                frame_id = self.conn.execute(
                    "SELECT id FROM frames WHERE terminal = ? AND captured_at = ?",
                    (term, captured_at),
                ).fetchone()
                if frame_id is None:
                    continue
                self.conn.execute(
                    """INSERT OR IGNORE INTO observations
                           (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                            queue_beyond_frame, ferry_at_dock, visibility, fullness,
                            confidence, usable, input_tokens, output_tokens, cost_usd,
                            created_at, raw)
                       VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, 0, 0, 0, ?, ?)""",
                    (
                        frame_id[0],
                        LANE_PROMPT_VERSION,
                        LANE_MODEL,
                        lanes,
                        beyond,
                        visibility,
                        fullness,
                        conf,
                        usable,
                        captured_at,
                        json.dumps(
                            {
                                "lanes_occupied": lanes,
                                "fullness": fullness,
                                "compound_visible": bool(usable),
                                "confidence": conf,
                            },
                            separators=(",", ":"),
                        ),
                    ),
                )
                written += 1
        return written

    def write_vision_reads(self, days: int) -> float:
        """The handful of frames somebody actually paid to have read.

        The default deployment never runs the vision model on a schedule, so a realistic
        database has a few dozen of these and not thousands: the mornings a person stood
        at the terminal and typed `ferrycast check`.
        """
        version = self.config.vision.prompt_version
        cutoff = iso(now_utc() - timedelta(days=days))
        candidates = self.conn.execute(
            """SELECT f.id, f.captured_at, o.fullness, o.lanes_occupied
                 FROM frames f
                 JOIN observations o ON o.frame_id = f.id AND o.prompt_version = ?
                WHERE f.status = 'ok' AND f.captured_at >= ? AND o.usable = 1
                ORDER BY f.captured_at""",
            (LANE_PROMPT_VERSION, cutoff),
        ).fetchall()
        if not candidates:
            return 0.0

        # Four frames per check, on a scattering of mornings — `on_demand_max_frames` plus
        # the fresh one the command captures for itself.
        # The most recent reads are always in: the last thing anybody did with this
        # install was look at today, and the pipeline page's "latest frame" should say so.
        picks = self.rng.sample(candidates, min(len(candidates), 40)) + candidates[-4:]
        spend = 0.0
        for row in picks:
            input_tokens = self.rng.randint(1100, 1400)
            output_tokens = self.rng.randint(90, 160)
            cost = (
                input_tokens / 1_000_000 * self.config.vision.input_usd_per_mtok
                + output_tokens / 1_000_000 * self.config.vision.output_usd_per_mtok
            )
            spend += cost
            self.conn.execute(
                """INSERT OR IGNORE INTO observations
                       (frame_id, prompt_version, model, vehicle_count, lanes_occupied,
                        queue_beyond_frame, ferry_at_dock, visibility, fullness, confidence,
                        usable, input_tokens, output_tokens, cost_usd, created_at)
                   VALUES (?, ?, ?, NULL, ?, 0, 0, 'clear', ?, ?, 1, ?, ?, ?, ?)""",
                (
                    row["id"],
                    version,
                    self.config.vision.model,
                    row["lanes_occupied"],
                    row["fullness"],
                    round(self.rng.uniform(0.72, 0.93), 2),
                    input_tokens,
                    output_tokens,
                    round(cost, 6),
                    row["captured_at"],
                ),
            )
        return spend

    # ------------------------------------------------------------------------- forecasts

    def write_marine(self, day: date) -> None:
        marine = self.config.route.marine
        if marine is None:
            return
        band, knots, wind = self.wind_for(day)
        issued = combine_local(day, parse_hhmm("04:00"), self.config.tz)
        warning = None
        if band == "gale":
            warning = "Gale warning in effect."
        elif band == "strong" and self.rng.random() < 0.4:
            warning = "Strong wind warning in effect."
        self.conn.execute(
            """INSERT OR IGNORE INTO marine_forecast
                   (route, site, service_date, issued_at, fetched_at, area, location, kind,
                    period, wind, visibility, warning, wind_max_kt, band, fetch_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'regular', ?, ?, ?, ?, ?, ?, 'ok')""",
            (
                self.route,
                marine.site,
                day.isoformat(),
                iso(issued),
                iso(issued + timedelta(minutes=6)),
                "Strait of Georgia",
                marine.location,
                "Today",
                wind,
                self.rng.choice([None, None, "Fog patches dissipating this morning."]),
                warning,
                knots,
                band,
            ),
        )

    def write_shore(self, day: date) -> None:
        condition, pop, temps = self.rng.choice(SHORE_CONDITIONS)
        low, high = temps
        issued = combine_local(day, parse_hhmm("05:00"), self.config.tz)
        warm = 1.0 if season(day) == "peak_summer" else (0.75 if season(day) == "shoulder" else 0.45)
        for terminal in self.config.route.terminals:
            if not terminal.configured_for_weather:
                continue
            temp_high = int(round((low + (high - low) * self.rng.random()) * warm)) + 4
            self.conn.execute(
                """INSERT OR IGNORE INTO shore_forecast
                       (route, terminal, site, service_date, issued_at, fetched_at, station,
                        region, kind, period, condition, pop, temp_high, temp_low, summary,
                        fetch_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'day', 'Today', ?, ?, ?, ?, ?, 'ok')""",
                (
                    self.route,
                    terminal.code,
                    terminal.weather_site,
                    day.isoformat(),
                    iso(issued),
                    iso(issued + timedelta(minutes=4)),
                    "Powell River" if terminal.code == "SLT" else "Sechelt",
                    (
                        "Sunshine Coast - Saltery Bay to Powell River"
                        if terminal.code == "SLT"
                        else "Sunshine Coast - Gibsons to Earls Cove"
                    ),
                    condition,
                    pop,
                    temp_high,
                    temp_high - self.rng.randint(6, 10),
                    f"{condition}. High {temp_high}. UV index 6 or high.",
                ),
            )

    # --------------------------------------------------------------------------- reports

    def write_reports(self, start: date, end: date) -> int:
        """First-hand accounts, filed the way a household actually files them.

        Concentrated on the sailings people care about — summer Fridays and Sundays — and
        thin everywhere else, because nobody reports on the 06:30 in February. That skew is
        the point: the pooled bounds on the page have to be built from a realistic
        scattering of reports, not from one per sailing.
        """
        written = 0
        cutoff = local(now_utc(), self.config.tz)
        day = max(start, end - timedelta(days=120))
        while day <= end:
            kind = day_type(day)
            base = {"friday": 0.5, "sunday_holiday": 0.42, "saturday": 0.3}.get(kind, 0.10)
            if season(day) == "peak_summer":
                base *= 1.5
            for s in self.build_day(day):
                if s["cancelled"] or s["departure"] >= cutoff:
                    continue
                hour = int(s["depart_hhmm"][:2])
                weight = base * (1.3 if 10 <= hour <= 18 else 0.4)
                if self.rng.random() > weight * 0.75:
                    continue

                # Who reports: somebody who was turned away is far likelier to file than
                # somebody for whom nothing happened.
                missed = s["residual"] > 3 and self.rng.random() < 0.75

                # Where they were in the line, and therefore when they joined it. This has
                # to be derived from their *position* rather than from the clock: whether
                # somebody got on was decided by how many vehicles were ahead of them, and
                # the whole point of the bound the page draws from this is that the same
                # arrival time means different things on different days.
                if missed:
                    position = self.rng.uniform(self.capacity, s["waiting"])
                else:
                    position = self.rng.uniform(0.08 * self.capacity, self.capacity)
                share = (position - s["carry_in"]) / max(1.0, s["demand"])
                share = max(0.03, min(0.999, share))
                minutes_before = 120 * (1 - share ** (1 / 1.35))
                joined = s["departure"] - timedelta(minutes=round(minutes_before))

                waited = None
                if missed:
                    waited = 2 if s["residual"] > self.capacity else 1
                    # Not everyone knows how long they ended up waiting, and the form does
                    # not force it — an unanswered wait stays `filled` rather than becoming
                    # a number nobody said.
                    if self.rng.random() < 0.35:
                        waited = None

                fullness = (
                    "full"
                    if s["filled"]
                    else self.rng.choice(["room", "half", "half", "nearly_full"])
                )
                written += self._file_report(s, day, missed, waited, joined, fullness)

                # Now and then two people file on the same sailing, which is the case worth
                # having in the data: the page has to reconcile "I got on" with "I didn't"
                # without letting the second erase the first.
                if self.rng.random() < 0.16:
                    other_missed = not missed and s["filled"]
                    position = (
                        self.rng.uniform(self.capacity, max(s["waiting"], self.capacity + 4))
                        if other_missed
                        else self.rng.uniform(0.08 * self.capacity, self.capacity)
                    )
                    share = max(
                        0.03, min(0.999, (position - s["carry_in"]) / max(1.0, s["demand"]))
                    )
                    written += self._file_report(
                        s,
                        day,
                        other_missed,
                        None,
                        s["departure"]
                        - timedelta(minutes=round(120 * (1 - share ** (1 / 1.35)))),
                        self.rng.choice(["half", "nearly_full", "full"]),
                    )
            day += timedelta(days=1)
        return written

    def _file_report(self, s, day, missed, waited, joined, fullness) -> int:
        self.conn.execute(
            """INSERT INTO sailing_reports
                   (route, origin, service_date, depart_hhmm, joined_queue_at,
                    departed_at, boarded, sailings_waited, deck_fullness, source,
                    submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'web', ?)""",
            (
                self.route,
                s["origin"],
                day.isoformat(),
                s["depart_hhmm"],
                iso(joined),
                # Half a memory is still worth filing, and the form asks for only one
                # answer — so most reports carry gaps.
                iso(s["actual"]) if self.rng.random() < 0.6 else None,
                0 if missed else 1,
                waited,
                fullness if not missed else None,
                iso(s["actual"] + timedelta(minutes=self.rng.randint(20, 400))),
            ),
        )
        return 1

    # ------------------------------------------------------------------ tags and job runs

    def write_tags(self) -> None:
        for service_date, tag, note in MANUAL_TAGS:
            self.conn.execute(
                """INSERT OR IGNORE INTO event_tags (service_date, tag, note, source, created_at)
                   VALUES (?, ?, ?, 'manual', ?)""",
                (service_date, tag, note, iso(now_utc())),
            )

    def write_job_runs(self, days: int) -> None:
        """What the scheduler would have to show for itself."""
        now = now_utc()
        plan = [
            ("capture", 5, 2),
            ("scrape", 15, 2),
            ("marine", 360, 1),
            ("shore", 360, 1),
            ("aggregate", 1440, 1),
        ]
        for job, every, attempted in plan:
            moment = now - timedelta(days=days)
            while moment <= now:
                ok = self.rng.random() > 0.01
                self.conn.execute(
                    """INSERT INTO job_runs (job, started_at, finished_at, ok, attempted,
                                             succeeded, detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job,
                        iso(moment),
                        iso(moment + timedelta(seconds=self.rng.randint(1, 9))),
                        int(ok),
                        attempted,
                        attempted if ok else 0,
                        None if ok else "read timeout",
                    ),
                )
                moment += timedelta(minutes=every)


def reset(conn: sqlite3.Connection) -> None:
    for table in (
        "observations",
        "frames",
        "deck_space",
        "sailing_records",
        "sailings",
        "sailing_reports",
        "marine_forecast",
        "shore_forecast",
        "event_tags",
        "job_runs",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/ferrycast.toml")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--frame-days",
        type=int,
        default=32,
        help="how many recent days get camera frames (the archive is bounded; deck space "
        "goes back to --start regardless)",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--reset", action="store_true", help="empty the tables first")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    conn = init_db(config.db_path)
    if args.reset:
        reset(conn)

    today = local(now_utc(), config.tz).date()
    end = args.end or today
    start = args.start or (end - timedelta(days=180))
    rng = random.Random(args.seed)
    sim = Simulator(conn, config, rng)

    print(f"seeding {start} .. {end}  ({(end - start).days + 1} days)")

    # A camera goes down for a couple of days, because one always does, and the pipeline
    # page is worth nothing if it has never had to show a bad day.
    outage = {
        ("ERL", end - timedelta(days=9)),
        ("ERL", end - timedelta(days=8)),
        ("SLT", end - timedelta(days=3)),
    }

    frame_start = end - timedelta(days=args.frame_days - 1)
    day = start
    frames = 0
    timetabled = 0
    while day <= end:
        # A date the timetable does not cover produces nothing, and says so at the end
        # rather than looking like a collection failure. The deployed schedule covers one
        # season at a time by design, so this is the normal case at the edges.
        if sim.build_day(day):
            timetabled += 1
        sim.write_deck_space(day)
        sim.write_marine(day)
        sim.write_shore(day)
        if day >= frame_start:
            frames += sim.write_frames(day, outage)
        if (day - start).days % 20 == 0:
            conn.commit()
            print(f"  {day} ...")
        day += timedelta(days=1)

    # Two days of forecast ahead of today, so a board for tomorrow is not blank.
    for ahead in (1, 2):
        sim.write_marine(end + timedelta(days=ahead))
        sim.write_shore(end + timedelta(days=ahead))

    spend = sim.write_vision_reads(days=min(args.frame_days, 30))
    reports = sim.write_reports(start, end)
    sim.write_tags()
    sim.write_job_runs(days=14)
    conn.commit()

    print(
        f"  {timetabled} of {(end - start).days + 1} days had a timetable; "
        f"frames {frames}, reports {reports}, vision spend ${spend:.2f}"
    )
    print("aggregating ...")
    totals = aggregate_range(conn, config, start, end)
    conn.commit()

    total = sum(totals.values())
    print(f"  {total} sailing records")
    for outcome, count in totals.items():
        if count:
            print(f"    {outcome:<13} {count:>5}  {count / total:>5.1%}")


if __name__ == "__main__":
    main()
