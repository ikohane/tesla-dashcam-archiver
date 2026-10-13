"""TeslaCam layout: recognising events and their metadata.

The car writes one MP4 per camera per minute:

    TeslaCam/SentryClips/<event time>/<segment time>-<camera>.mp4   (+ event.json, thumb.png, event.mp4)
    TeslaCam/SavedClips/<event time>/…                               (same)
    TeslaCam/RecentClips/<segment time>-<camera>.mp4                 (rolling buffer, flat)
    TeslaCam/EncryptedClips/…                                        (2026.20+ encrypted clips)

An *event* is one Saved or Sentry folder, or one continuous drive of
RecentClips. The library keeps every event's raw files together under
``events/<year>/<Kind>/<event>/`` and builds one tiled video per event in
``composite/<year>/``.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Library layout (relative to the library root)
INCOMING = "incoming"
EVENTS = "events"
COMPOSITE = "composite"
POSTERS = "posters"

KINDS = {"SavedClips": "saved", "SentryClips": "sentry",
         "RecentClips": "recent", "TeslaTrackMode": "track"}
KIND_DIR = {v: k for k, v in KINDS.items()}
KIND_LABEL = {"saved": "Saved dashcam clip", "sentry": "Sentry Mode event",
              "recent": "Dashcam drive", "track": "Track Mode"}
ENCRYPTED_DIR = "EncryptedClips"

CAMS_4 = ["front", "back", "left_repeater", "right_repeater"]
CAMS_6 = ["left_pillar", "front", "right_pillar", "left_repeater", "back", "right_repeater"]

SEG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-([a-z_]+)\.mp4$")
# An event folder is named after the event time. A suffix is a title someone
# added by hand (e.g. "2020-06-14_15-32-11_Graduation") and is kept.
EVENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:[_ -][^/]+)?$")
# Kept with their event, never tiled: event.json (time, place, reason),
# thumb.png (the app's poster) and, from newer car software, event.mp4 (the
# app's 10-second preview).
SIDECARS = ("event.json", "thumb.png", "event.mp4")

REASONS = {
    "sentry_aware_object_detection": "object detected nearby",
    "sentry_aware_accel": "car bumped",
    "sentry_locked_handle_pulls": "door handle pulled",
    "sentry_panic_alarm": "alarm",
    "user_interaction_dashcam_icon_tapped": "saved from the dashcam icon",
    "user_interaction_dashcam_panel_save": "saved from the dashcam panel",
    "user_interaction_honk": "saved by honking",
    "user_interaction_dashcam_launcher_action_tapped": "saved from the launcher",
    "vehicle_auto_emergency_braking": "automatic emergency braking",
    "collision": "collision",
}

Listing = Iterable[Tuple[str, int, float]]   # (relative path, size, mtime-or-ctime)


def ts(name: str) -> datetime:
    return datetime.strptime(name[:19], "%Y-%m-%d_%H-%M-%S")


def event_title(name: str) -> Optional[str]:
    """The hand-added title after the timestamp, if any ("Graduation")."""
    t = name[20:].replace("_", " ").strip()
    return t or None


def car_for(name: str, cars: Sequence[Tuple[str, str]]) -> str:
    """Label of the car that recorded an event, from [(until_date, label), …]."""
    day = name[:10]
    for until, label in cars:
        if not until or day < until:
            return label
    return cars[-1][1] if cars else "Tesla"


@dataclass
class Event:
    kind: str                 # saved | sentry | recent | track
    name: str                 # YYYY-MM-DD_HH-MM-SS[_title]
    files: Dict[str, Tuple[int, float]] = field(default_factory=dict)  # path → (size, mtime)

    @property
    def kind_dir(self) -> str:
        return KIND_DIR[self.kind]

    @property
    def year(self) -> str:
        return self.name[:4]

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.name}"

    @property
    def dest_dir(self) -> str:
        return f"{EVENTS}/{self.year}/{self.kind_dir}/{self.name}"

    @property
    def composite_rel(self) -> str:
        return f"{COMPOSITE}/{self.year}/{self.name}_{self.kind}.mp4"

    @property
    def poster_rel(self) -> str:
        return f"{POSTERS}/{self.year}/{self.name}_{self.kind}.jpg"

    def media(self) -> Dict[str, Dict[str, str]]:
        """segment timestamp → camera → path."""
        segs: Dict[str, Dict[str, str]] = {}
        for rel in self.files:
            m = SEG_RE.match(os.path.basename(rel))
            if m:
                segs.setdefault(m.group(1), {})[m.group(2)] = rel
        return dict(sorted(segs.items()))

    def sidecar(self, base: str) -> Optional[str]:
        for rel in self.files:
            if os.path.basename(rel) == base:
                return rel
        return None

    def signature(self) -> str:
        """Identity of the raw files a composite is built from (names + sizes)."""
        h = hashlib.sha1()
        for rel in sorted(self.files):
            h.update(f"{os.path.basename(rel)}\0{self.files[rel][0]}\n".encode())
        return h.hexdigest()[:16]

    def newest_mtime(self) -> float:
        return max((m for _, m in self.files.values()), default=0.0)


def _kind_index(parts: List[str]) -> Optional[int]:
    for i, p in enumerate(parts):
        if p in KINDS or p == ENCRYPTED_DIR:
            return i
    return None


def group_raw(listing: Listing, recent_gap_s: int = 75
              ) -> Tuple[List[Event], List[str], List[str]]:
    """Group a raw TeslaCam tree (what the Pi or a USB stick holds) into events.

    Saved/Sentry: the folder named after the event time is the event.
    Recent (a flat rolling buffer): consecutive minutes form one drive; a gap
    longer than ``recent_gap_s`` between segment starts begins a new one.
    Returns (events, encrypted_files, ignored_files)."""
    events: Dict[str, Event] = {}
    loose: Dict[str, List[Tuple[str, int, float]]] = {}
    encrypted, ignored = [], []
    for rel, size, mtime in listing:
        parts = rel.split("/")
        base = parts[-1]
        if "@eaDir" in parts or base.startswith("."):
            continue
        i = _kind_index(parts)
        if i is None:
            ignored.append(rel)
            continue
        if parts[i] == ENCRYPTED_DIR:
            encrypted.append(rel)
            continue
        kind = KINDS[parts[i]]
        rest = parts[i + 1:]
        seg = SEG_RE.match(base)
        if kind != "recent" and len(rest) == 2 and EVENT_RE.match(rest[0]) \
                and (seg or base in SIDECARS):
            ev = events.setdefault(f"{kind}/{rest[0]}", Event(kind, rest[0]))
            ev.files[rel] = (size, mtime)
        elif seg:
            loose.setdefault(f"{kind}\0{seg.group(1)}", []).append((rel, size, mtime))
        else:
            ignored.append(rel)
    by_kind: Dict[str, List[str]] = {}
    for k in loose:
        kind, stamp = k.split("\0")
        by_kind.setdefault(kind, []).append(stamp)
    for kind, stamps in by_kind.items():
        stamps.sort()
        run_start, prev = None, None
        for stamp in stamps:
            if prev is None or (ts(stamp) - ts(prev)).total_seconds() > recent_gap_s:
                run_start = stamp
            ev = events.setdefault(f"{kind}/{run_start}", Event(kind, run_start))
            for rel, size, mtime in loose[f"{kind}\0{stamp}"]:
                ev.files[rel] = (size, mtime)
            prev = stamp
    return sorted(events.values(), key=lambda e: e.key), encrypted, ignored


def group_normalized(listing: Listing) -> List[Event]:
    """Events already in events/<year>/<Kind>/<name>/ (listing relative to events/)."""
    events: Dict[str, Event] = {}
    for rel, size, mtime in listing:
        parts = rel.split("/")
        if len(parts) != 4 or "@eaDir" in parts or parts[-1].startswith("."):
            continue
        _year, kdir, name, base = parts
        if kdir not in KINDS or not EVENT_RE.match(name):
            continue
        if not (SEG_RE.match(base) or base in SIDECARS):
            continue
        kind = KINDS[kdir]
        ev = events.setdefault(f"{kind}/{name}", Event(kind, name))
        ev.files[f"{EVENTS}/{rel}"] = (size, mtime)
    return sorted(events.values(), key=lambda e: e.key)


def plan_moves(events: List[Event], src_root: str) -> List[Tuple[str, str]]:
    """(source, destination) for each event file; destinations are library-relative."""
    moves = []
    for ev in events:
        for rel in sorted(ev.files):
            moves.append((f"{src_root}/{rel}", f"{ev.dest_dir}/{os.path.basename(rel)}"))
    return moves


def event_metadata(ev: Event, meta: Dict[str, Any], tz: tzinfo,
                   cars: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
    """Date, place and a human caption for an event, from its event.json.

    The car's clock is local time without a zone, so ``tz`` supplies it."""
    stamp = meta.get("timestamp")
    try:
        local = datetime.fromisoformat(stamp) if stamp else ts(ev.name)
    except ValueError:
        local = ts(ev.name)
    if ev.kind == "recent":
        local = ts(ev.name)            # a drive starts at its first segment
    aware = local.replace(tzinfo=tz) if local.tzinfo is None else local
    lat = lon = None
    try:
        lat, lon = float(meta["est_lat"]), float(meta["est_lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            lat = lon = None
    except (KeyError, TypeError, ValueError):
        pass
    city = (meta.get("city") or "").strip() or None
    reason_code = (meta.get("reason") or "").strip()
    reason = REASONS.get(reason_code) or (reason_code.replace("_", " ") if reason_code else None)
    title = event_title(ev.name)
    caption = (f"{title} ({KIND_LABEL[ev.kind].lower()})" if title else KIND_LABEL[ev.kind]) \
        + (f": {reason}" if reason else "") + (f" — {city}" if city else "")
    return {"taken_at": aware.isoformat(timespec="seconds"),
            "utc": aware.astimezone(timezone.utc),
            "gps_lat": lat, "gps_lon": lon, "city": city,
            "reason": reason_code or None, "caption": caption,
            "car": car_for(ev.name, cars) if cars else "Tesla"}
