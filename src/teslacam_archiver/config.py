"""Configuration (TOML). See ``config.example.toml`` for every option."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .build import BuildSettings

DEFAULT_CONFIG = os.path.expanduser(
    os.environ.get("TESLACAM_CONFIG", "~/.config/teslacam-archiver/config.toml"))
DEFAULT_STATE = "~/.local/state/teslacam-archiver"


class ConfigError(ValueError):
    pass


def local_zone_name() -> str:
    """The system time zone name (e.g. 'America/New_York'), or 'UTC'."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz.lstrip(":")
    try:
        real = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in real:
            return real.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


@dataclass
class Config:
    library: Dict[str, Any]
    state_dir: str
    timezone: str
    cars: List[Tuple[str, str]] = field(default_factory=list)
    build: BuildSettings = field(default_factory=BuildSettings)
    quiescent_seconds: int = 600
    recent_gap_seconds: int = 75
    after_event: Optional[str] = None      # shell command run after each event is built
    source: str = ""

    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as e:
            raise ConfigError(f"unknown timezone {self.timezone!r}") from e

    @property
    def ledger_path(self) -> str:
        return os.path.join(self.state_dir, "ledger.sqlite")

    @property
    def scratch(self) -> str:
        return os.path.join(self.state_dir, "scratch")


def from_dict(d: Dict[str, Any], source: str = "") -> Config:
    lib = dict(d.get("library") or {})
    if not lib.get("path"):
        raise ConfigError("[library] path is required")
    lib.setdefault("backend", "local")
    if lib["backend"] == "local":
        lib["path"] = os.path.expanduser(lib["path"])
    b = d.get("build") or {}
    settings = BuildSettings(
        encoder=b.get("encoder", "auto"), bitrate=str(b.get("bitrate", "6M")),
        fps=int(b.get("fps", 30)), segment_workers=int(b.get("segment_workers", 4)),
        poster_width=int(b.get("poster_width", 480)))
    cars = []
    for c in d.get("cars") or []:
        if not c.get("label"):
            raise ConfigError("each [[cars]] entry needs a label")
        cars.append((str(c.get("until") or ""), str(c["label"])))
    p = d.get("pipeline") or {}
    return Config(
        library=lib,
        state_dir=os.path.expanduser((d.get("state") or {}).get("dir", DEFAULT_STATE)),
        timezone=(d.get("car") or {}).get("timezone") or local_zone_name(),
        cars=cars, build=settings,
        quiescent_seconds=int(p.get("quiescent_seconds", 600)),
        recent_gap_seconds=int(p.get("recent_gap_seconds", 75)),
        after_event=(d.get("hooks") or {}).get("after_event"),
        source=source)


def load(path: Optional[str] = None) -> Config:
    path = os.path.expanduser(path or DEFAULT_CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no config at {path} — run `teslacam-archiver init` to create one")
    with open(path, "rb") as f:
        return from_dict(tomllib.load(f), source=path)
