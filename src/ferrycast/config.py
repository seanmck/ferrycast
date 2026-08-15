"""Configuration loading.

Everything the PRD flags as an open question (webcam URLs, the timetable, deck-space page
layout) is configuration rather than code, so it can be corrected without a release.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = Path("config/ferrycast.toml")
CONFIG_ENV_VAR = "FERRYCAST_CONFIG"

# Deployment-specific overrides. A container gets its storage path and camera URLs from the
# environment, so the same committed config works locally and on a host like Railway.
DATA_DIR_ENV_VAR = "FERRYCAST_DATA_DIR"
WEBCAM_ENV_PREFIX = "FERRYCAST_WEBCAM_"
DECK_SPACE_ENV_PREFIX = "FERRYCAST_DECKSPACE_"
# The public origin is only known once the app is deployed, so it comes from the
# environment rather than the committed config.
BASE_URL_ENV_VAR = "FERRYCAST_BASE_URL"
# The two switches that let a request spend money. Overridable from the environment because
# a spend toggle has to be killable without a commit, a build and a deploy — on a host like
# Railway that is a variable change and a restart.
ALLOW_BACKFILL_ENV_VAR = "FERRYCAST_ALLOW_BACKFILL"
ALLOW_CHECKS_ENV_VAR = "FERRYCAST_ALLOW_CHECKS"
BACKFILL_CAP_ENV_VAR = "FERRYCAST_BACKFILL_DAILY_FRAME_CAP"
# Analytics. The project key is environment-only and never a config key: `config/ferrycast.toml`
# is committed into the image (see the Dockerfile), and a fork that copied it would start
# reporting somebody else's traffic into this project. The off switch is separate from the key
# so it can be thrown without deleting the credential.
POSTHOG_KEY_ENV_VAR = "FERRYCAST_POSTHOG_KEY"
POSTHOG_HOST_ENV_VAR = "FERRYCAST_POSTHOG_HOST"
ANALYTICS_ENV_VAR = "FERRYCAST_ANALYTICS"


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


#: The camera named by a terminal's own `webcam_url`. Every frame captured before cameras
#: were nameable is this one, and the migration backfills it accordingly.
MAIN_CAMERA = "main"

#: Which reader understands a camera's view. `lanes` counts occupancy across a marshalling
#: grid; `queue_extent` measures how far back a queue reaches along a road.
CAMERA_KINDS = ("lanes", "queue_extent")


@dataclass(frozen=True)
class Camera:
    """An extra camera at a terminal, beyond the one its `webcam_url` names.

    Extra cameras exist because one view does not answer everything. Earls Cove's own camera
    overlooks the marshalling lanes; a highway camera down the road sees the overflow, which
    only happens once those lanes are full and is therefore the more decisive of the two.

    `replay_index_url` is the unusual field and the reason this is worth configuring rather
    than hardcoding. Most camera feeds publish only the current image, so a frame not taken
    is gone — but some publish a rolling window of recent stills, which makes the archive
    recoverable after the fact and turns capture from a job that must never miss into one
    that merely has to run before the window rolls over.
    """

    id: str
    name: str = ""
    kind: str = "queue_extent"
    #: The current still. Optional: a camera archived only from its replay window needs none.
    image_url: str = ""
    #: Returns a JSON array of timestamps, newest window first or last — order is not relied
    #: on. Leave blank and this camera has no retrievable history.
    replay_index_url: str = ""
    #: Template for one archived still. Must contain `{timestamp}`, filled from the index.
    replay_frame_url: str = ""
    #: How to read an index timestamp. The default matches DriveBC's `20260814200001`.
    replay_timestamp_format: str = "%Y%m%d%H%M%S"

    @property
    def has_replay(self) -> bool:
        return bool(self.replay_index_url and self.replay_frame_url)


@dataclass(frozen=True)
class Terminal:
    code: str
    name: str
    destination: str
    webcam_url: str = ""
    deck_space_url: str = ""
    # Whether this terminal's camera actually shows the berth. Earls Cove's points up the
    # approach road and never sees a vessel, so "no ferry in any frame" there means nothing
    # — while at a berth-facing camera it is evidence a sailing did not run. Defaults to
    # false so an unverified camera cannot manufacture cancellations; set it true only
    # after looking at a frame.
    camera_sees_berth: bool = False
    # Which ECCC city forecast covers this end of the crossing. Per terminal rather than per
    # route: the two ends are a strait apart and get different weather, and the page answers
    # for the one you are leaving from. Both keys or neither — the province is part of the
    # feed's path, so a site code without one cannot be fetched.
    weather_site: str = ""
    weather_province: str = ""
    # Which way a vessel points once it has left this terminal — "W" leaving Earls Cove,
    # "E" leaving Saltery Bay. Needed because the vessel tracker never names the port a ship
    # is docked at, so its heading once moving is the only thing that says which terminal it
    # just left. Left empty, no departure is ever inferred from tracking for this terminal:
    # a wrong bearing would attribute the other direction's sailing to this one.
    outbound_bearing: str = ""
    # Extra cameras at this terminal, beyond the one `webcam_url` names.
    cameras: tuple[Camera, ...] = ()

    @property
    def configured_for_capture(self) -> bool:
        return bool(self.webcam_url)

    def camera(self, camera_id: str) -> Camera:
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        raise ConfigError(f"terminal {self.code!r} has no camera {camera_id!r}")

    @property
    def replay_cameras(self) -> tuple[Camera, ...]:
        return tuple(cam for cam in self.cameras if cam.has_replay)

    @property
    def configured_for_weather(self) -> bool:
        return bool(self.weather_site and self.weather_province)


@dataclass(frozen=True)
class MarineArea:
    """Which ECCC marine forecast covers the water this route crosses.

    `location` names the sub-area, because a marine area is often split and taking the first
    would answer about the wrong half: the Strait of Georgia is forecast north and south of
    Nanaimo separately, and this route is entirely in the north half. Left unset the first
    location is used, which is a guess and is recorded as such on the row.
    """

    site: str
    domain: str = "pacific"
    location: str = ""


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    terminals: tuple[Terminal, ...]
    # Optional: without it the marine forecast is simply not collected or shown. A route
    # whose operator never cancels for weather has no use for it.
    marine: MarineArea | None = None
    # The operator's live vessel tracker for this route. Optional, and only worth setting
    # where a terminal has no departures board of its own — it is the sole source of a
    # departure time for such a direction, and it can say nothing else.
    vessel_tracking_url: str = ""

    def terminal(self, code: str) -> Terminal:
        for t in self.terminals:
            if t.code == code:
                return t
        raise ConfigError(f"unknown terminal {code!r} for route {self.id!r}")

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(t.code for t in self.terminals)

    @property
    def destinations(self) -> dict[str, str]:
        """origin terminal code -> destination terminal code."""
        return {t.code: t.destination for t in self.terminals}


@dataclass(frozen=True)
class CaptureConfig:
    interval_minutes: int = 15
    timeout_seconds: float = 20.0
    max_retries: int = 2
    user_agent: str = "ferrycast/0.1 (personal archival project; low rate)"
    # Whether frames are archived on a schedule. On by default, and it costs only disk:
    # extraction can always be run later, but a frame not captured at 14:15 is gone for
    # good. Set false if disk is tight and you accept never being able to look back.
    scheduled: bool = True


@dataclass(frozen=True)
class VisionConfig:
    model: str = "claude-sonnet-5"
    prompt_version: str = "v2"
    max_frames_per_run: int = 200
    max_image_width: int = 896
    monthly_budget_usd: float = 5.0
    input_usd_per_mtok: float = 3.0
    output_usd_per_mtok: float = 15.0
    min_confidence: float = 0.35
    skip_dark_frames: bool = True
    dark_luma_threshold: float = 24.0

    # Which frames are worth paying for when classifying a sailing, as minutes relative to
    # its scheduled departure (negative = before). Aggregation needs the queue shortly
    # before departure and what was left after it settles — not every frame in the window.
    essential_offsets_minutes: tuple[int, ...] = (-30, -10, 15, 30)
    essential_tolerance_minutes: int = 10

    # `ferrycast check` — the on-demand path.
    on_demand_max_frames: int = 3
    on_demand_max_age_minutes: int = 90


@dataclass(frozen=True)
class WebConfig:
    # A web request that spends money is a footgun, so on-demand checks through the web
    # endpoint are opt-in and separately capped even on a household-only deployment.
    allow_on_demand_checks: bool = False
    # The "fill in this slot's history" button. Off by default for the same reason as the
    # line above: a public URL that spends money on a stranger's tap is a bad default. The
    # cap counts frames, not taps, so one expensive slot cannot exhaust the day on its own.
    allow_on_demand_backfill: bool = False
    backfill_daily_frame_cap: int = 120
    on_demand_daily_cap: int = 40
    # Public origin, e.g. "https://ferrycast.example.com". Open Graph needs absolute URLs,
    # and behind a TLS-terminating proxy the app cannot always work out its own scheme.
    # Left empty, the URL the request arrived on is used instead.
    base_url: str = ""


@dataclass(frozen=True)
class AnalyticsConfig:
    """Server-side PostHog: who reaches the app, and whether it answered them.

    Deliberately server-side. A tag in the document would be a blocking round trip to a CDN
    before the answer paints, on a phone with one bar at the side of Highway 101 — and the
    only third party the browser is asked to contact is the terminal camera, which is lazy
    and below the fold. Everything worth counting here (which sailing was asked about,
    whether there was enough history to answer it, whether anyone filed a report) is visible
    in the request the app is already serving, so the page phones nobody. The collectors'
    own remotes are a separate matter: they run off the request path, and this joins them.

    Off unless `api_key` is set, which only the environment can do.
    """

    # A kill switch that does not require deleting the key: `FERRYCAST_ANALYTICS=off`.
    enabled: bool = True
    host: str = "https://us.i.posthog.com"
    # Environment-only — see POSTHOG_KEY_ENV_VAR. Present on the dataclass because the rest
    # of the app should read one resolved object rather than reach for os.environ.
    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.api_key)


@dataclass(frozen=True)
class AggregateConfig:
    vessel_capacity: int = 125
    lookback_minutes: int = 120
    departure_grace_minutes: int = 20
    settle_minutes: int = 12
    post_window_minutes: int = 45
    residual_threshold: int = 5


@dataclass(frozen=True)
class QueryConfig:
    min_sample: int = 5
    time_tolerance_minutes: int = 15
    relaxed_time_tolerance_minutes: int = 60
    # Only the most recent N comparable sailings count. Without a cap the distribution
    # accumulates forever, and a sailing from two timetables and a different vessel ago
    # weighs as much as last week's — ageing quietly, while still looking well-evidenced.
    max_samples: int = 5


@dataclass(frozen=True)
class RetentionConfig:
    keep_frames_days: int = 400
    downsample_after_days: int = 120
    downsample_keep_per_hour: int = 1
    # With extraction deferred, an unread frame is the only record of that moment. Pruning
    # skips those by default so a cheap extraction policy can't quietly destroy history.
    keep_unextracted: bool = True
    # ...but "keep everything unread forever" is a disk leak. After this many days, unread
    # frames are thinned to the ones an extraction would actually look at (the essential
    # offsets around each departure). Roughly halves disk while leaving every sailing still
    # analysable in full. Set 0 to keep every unread frame.
    thin_unextracted_after_days: int = 45


@dataclass(frozen=True)
class Config:
    timezone_name: str
    data_dir: Path
    db_path: Path
    frames_dir: Path
    schedule_path: Path
    routes: tuple[Route, ...]
    active_route_id: str
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    aggregate: AggregateConfig = field(default_factory=AggregateConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    source_path: Path | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def route(self) -> Route:
        """The route this install collects for. v1 is deliberately single-route."""
        return self.route_by_id(self.active_route_id)

    def route_by_id(self, route_id: str) -> Route:
        for route in self.routes:
            if route.id == route_id:
                return route
        known = ", ".join(r.id for r in self.routes)
        raise ConfigError(f"unknown route {route_id!r}; known routes: {known}")


# Anything not clearly affirmative is off. A spend switch must not be turned on by a typo,
# and "false"/"0"/"" set by a deploy script must never read as true — which is what
# bool(os.environ[...]) would do for every one of them.
_TRUE = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _dataclass_from(cls, raw: dict, section: str):
    fields = cls.__dataclass_fields__
    unknown = set(raw) - set(fields)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    # TOML has no tuple type, so coerce lists for fields that want one.
    coerced = {
        key: tuple(value)
        if isinstance(value, list) and isinstance(fields[key].default, tuple)
        else value
        for key, value in raw.items()
    }
    return cls(**coerced)


def _parse_cameras(terminal_code: str, entries: list) -> tuple[Camera, ...]:
    cameras: list[Camera] = []
    seen: set[str] = set()
    for entry in entries:
        camera_id = str(entry.get("id", "")).strip()
        if not camera_id:
            raise ConfigError(f"a camera on terminal {terminal_code!r} is missing `id`")
        # The id names a directory under the frame store and a calibration file, so it has
        # to survive being a path segment. Rejecting the awkward ones here beats discovering
        # them when a capture writes somewhere unintended.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", camera_id):
            raise ConfigError(
                f"camera id {camera_id!r} on terminal {terminal_code!r} must be letters, "
                "digits, dots, dashes or underscores — it names a directory and a file"
            )
        if camera_id == MAIN_CAMERA:
            raise ConfigError(
                f"camera id {MAIN_CAMERA!r} on terminal {terminal_code!r} is reserved for "
                "the camera named by that terminal's `webcam_url`; give this one its own id"
            )
        if camera_id in seen:
            raise ConfigError(
                f"terminal {terminal_code!r} declares camera {camera_id!r} twice"
            )
        seen.add(camera_id)

        kind = str(entry.get("kind", "queue_extent")).strip()
        if kind not in CAMERA_KINDS:
            raise ConfigError(
                f"camera {camera_id!r} on terminal {terminal_code!r} has kind {kind!r}; "
                f"it must be one of {', '.join(CAMERA_KINDS)}"
            )

        index_url = entry.get("replay_index_url", "")
        frame_url = entry.get("replay_frame_url", "")
        if bool(index_url) != bool(frame_url):
            raise ConfigError(
                f"camera {camera_id!r} on terminal {terminal_code!r} needs both "
                "`replay_index_url` and `replay_frame_url`, or neither — an index of "
                "timestamps is no use without the address to fetch one from"
            )
        if frame_url and "{timestamp}" not in frame_url:
            raise ConfigError(
                f"camera {camera_id!r} on terminal {terminal_code!r} has a "
                "`replay_frame_url` with no `{timestamp}` in it, so every archived frame "
                "would be fetched from the same address"
            )
        if not index_url and not entry.get("image_url"):
            raise ConfigError(
                f"camera {camera_id!r} on terminal {terminal_code!r} has neither an "
                "`image_url` nor a replay feed, so nothing could ever be captured from it"
            )

        cameras.append(
            Camera(
                id=camera_id,
                name=entry.get("name", ""),
                kind=kind,
                image_url=entry.get("image_url", ""),
                replay_index_url=index_url,
                replay_frame_url=frame_url,
                replay_timestamp_format=entry.get(
                    "replay_timestamp_format", "%Y%m%d%H%M%S"
                ),
            )
        )
    return tuple(cameras)


def _parse_route(raw: dict) -> Route:
    terminals_raw = raw.get("terminals") or []
    if len(terminals_raw) != 2:
        raise ConfigError("[route] must define exactly two terminals (one per direction)")
    terminals = []
    for entry in terminals_raw:
        for required in ("code", "name", "destination"):
            if not entry.get(required):
                raise ConfigError(f"terminal entry missing {required!r}")
        code = entry["code"]
        terminals.append(
            Terminal(
                code=code,
                name=entry["name"],
                destination=entry["destination"],
                webcam_url=os.environ.get(
                    f"{WEBCAM_ENV_PREFIX}{code.upper()}", entry.get("webcam_url", "")
                ),
                deck_space_url=os.environ.get(
                    f"{DECK_SPACE_ENV_PREFIX}{code.upper()}", entry.get("deck_space_url", "")
                ),
                camera_sees_berth=bool(entry.get("camera_sees_berth", False)),
                weather_site=entry.get("weather_site", ""),
                weather_province=entry.get("weather_province", ""),
                outbound_bearing=str(entry.get("outbound_bearing", "")).strip().upper(),
                cameras=_parse_cameras(code, entry.get("cameras") or []),
            )
        )
        bearing = str(entry.get("outbound_bearing", "")).strip().upper()
        if bearing and (set(bearing) - set("NESW") or not bearing):
            raise ConfigError(
                f"terminal {code!r} has outbound_bearing {entry['outbound_bearing']!r}; "
                "it must be a compass point such as N, W or NW"
            )
        if bool(entry.get("weather_site")) != bool(entry.get("weather_province")):
            raise ConfigError(
                f"terminal {code!r} needs both `weather_site` and `weather_province`, or "
                "neither — the province is part of the forecast's path"
            )
    codes = {t.code for t in terminals}
    for t in terminals:
        if t.destination not in codes:
            raise ConfigError(
                f"terminal {t.code!r} points at destination {t.destination!r}, "
                "which is not part of this route"
            )
        if t.destination == t.code:
            raise ConfigError(f"terminal {t.code!r} cannot sail to itself")
    marine_raw = raw.get("marine")
    marine = None
    if marine_raw:
        if not marine_raw.get("site"):
            raise ConfigError("[route.marine] needs a `site` (the ECCC marine area code)")
        marine = _dataclass_from(MarineArea, marine_raw, "route.marine")

    return Route(
        id=raw.get("id", "route"),
        name=raw.get("name", raw.get("id", "route")),
        terminals=tuple(terminals),
        marine=marine,
        vessel_tracking_url=raw.get("vessel_tracking_url", ""),
    )


def _parse_routes(raw: dict, active_id: str | None) -> tuple[tuple[Route, ...], str]:
    """Parse the route section(s) and decide which one this install tracks.

    v1 tracks a single route, but the file format already accepts several — TOML's
    `[[route]]` array form — so adding one later is a config edit rather than a format
    change. Everything downstream is keyed by route id, so the routes that are defined but
    not active simply have no data collected against them.
    """
    section = raw.get("route")
    if not section:
        raise ConfigError("missing [route] section")

    entries = section if isinstance(section, list) else [section]
    routes = tuple(_parse_route(entry) for entry in entries)

    ids = [r.id for r in routes]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ConfigError(f"duplicate route id(s): {', '.join(sorted(duplicates))}")

    if active_id:
        if active_id not in ids:
            raise ConfigError(
                f"active_route {active_id!r} is not defined; known routes: {', '.join(ids)}"
            )
        return routes, active_id

    if len(routes) > 1:
        raise ConfigError(
            "several routes are defined, so [app] active_route must name the one to track "
            f"(one of: {', '.join(ids)}). v1 collects for a single route at a time."
        )
    return routes, routes[0].id


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit)
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | os.PathLike | None = None) -> Config:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        raise ConfigError(
            f"no config at {config_path}. Copy config/ferrycast.example.toml to "
            f"{config_path} and fill in the webcam and deck-space URLs."
        )
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    app = raw.get("app", {})
    base = config_path.parent
    # A mounted volume path comes from the environment, so the committed config does not
    # have to know where it will be deployed.
    data_dir = Path(os.environ.get(DATA_DIR_ENV_VAR) or app.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = (base / data_dir).resolve()

    schedule_path = Path(app.get("schedule_path", "schedule.toml"))
    if not schedule_path.is_absolute():
        schedule_path = (base / schedule_path).resolve()

    routes, active_route_id = _parse_routes(raw, app.get("active_route"))

    web = _dataclass_from(WebConfig, raw.get("web", {}), "web")
    if os.environ.get(BASE_URL_ENV_VAR):
        web = replace(web, base_url=os.environ[BASE_URL_ENV_VAR])
    if os.environ.get(ALLOW_BACKFILL_ENV_VAR) is not None:
        web = replace(web, allow_on_demand_backfill=_env_flag(ALLOW_BACKFILL_ENV_VAR))
    if os.environ.get(ALLOW_CHECKS_ENV_VAR) is not None:
        web = replace(web, allow_on_demand_checks=_env_flag(ALLOW_CHECKS_ENV_VAR))
    if os.environ.get(BACKFILL_CAP_ENV_VAR):
        try:
            web = replace(
                web, backfill_daily_frame_cap=int(os.environ[BACKFILL_CAP_ENV_VAR])
            )
        except ValueError as exc:
            raise ConfigError(
                f"{BACKFILL_CAP_ENV_VAR} must be a whole number, "
                f"got {os.environ[BACKFILL_CAP_ENV_VAR]!r}"
            ) from exc

    analytics_raw = dict(raw.get("analytics", {}))
    if "api_key" in analytics_raw:
        raise ConfigError(
            "[analytics] api_key does not belong in the config file — this file is committed "
            f"into the image. Set {POSTHOG_KEY_ENV_VAR} in the environment instead."
        )
    analytics = _dataclass_from(AnalyticsConfig, analytics_raw, "analytics")
    analytics = replace(analytics, api_key=os.environ.get(POSTHOG_KEY_ENV_VAR, "").strip())
    if os.environ.get(POSTHOG_HOST_ENV_VAR):
        analytics = replace(analytics, host=os.environ[POSTHOG_HOST_ENV_VAR].strip())
    if os.environ.get(ANALYTICS_ENV_VAR) is not None:
        analytics = replace(analytics, enabled=_env_flag(ANALYTICS_ENV_VAR))

    return Config(
        timezone_name=app.get("timezone", "America/Vancouver"),
        data_dir=data_dir,
        db_path=data_dir / app.get("db_name", "ferrycast.db"),
        frames_dir=data_dir / app.get("frames_dirname", "frames"),
        schedule_path=schedule_path,
        routes=routes,
        active_route_id=active_route_id,
        capture=_dataclass_from(CaptureConfig, raw.get("capture", {}), "capture"),
        vision=_dataclass_from(VisionConfig, raw.get("vision", {}), "vision"),
        aggregate=_dataclass_from(AggregateConfig, raw.get("aggregate", {}), "aggregate"),
        query=_dataclass_from(QueryConfig, raw.get("query", {}), "query"),
        retention=_dataclass_from(RetentionConfig, raw.get("retention", {}), "retention"),
        web=web,
        analytics=analytics,
        source_path=config_path,
    )
