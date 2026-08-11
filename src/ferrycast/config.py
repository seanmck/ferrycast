"""Configuration loading.

Everything the PRD flags as an open question (webcam URLs, the timetable, deck-space page
layout) is configuration rather than code, so it can be corrected without a release.
"""

from __future__ import annotations

import os
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


class ConfigError(Exception):
    """Raised when configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class Terminal:
    code: str
    name: str
    destination: str
    webcam_url: str = ""
    deck_space_url: str = ""

    @property
    def configured_for_capture(self) -> bool:
        return bool(self.webcam_url)


@dataclass(frozen=True)
class Route:
    id: str
    name: str
    terminals: tuple[Terminal, ...]

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
    model: str = "claude-haiku-4-5"
    prompt_version: str = "v1"
    max_frames_per_run: int = 200
    max_image_width: int = 896
    monthly_budget_usd: float = 5.0
    input_usd_per_mtok: float = 1.0
    output_usd_per_mtok: float = 5.0
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
    # A web request that spends money is a footgun, so on-demand checks from the browser
    # are opt-in and separately capped even though the UI is household-only.
    allow_on_demand_checks: bool = False
    on_demand_daily_cap: int = 40
    # Public origin, e.g. "https://ferrycast.example.com". Open Graph needs absolute URLs,
    # and behind a TLS-terminating proxy the app cannot always work out its own scheme.
    # Left empty, the URL the request arrived on is used instead.
    base_url: str = ""


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
            )
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
    return Route(
        id=raw.get("id", "route"),
        name=raw.get("name", raw.get("id", "route")),
        terminals=tuple(terminals),
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
        source_path=config_path,
    )
