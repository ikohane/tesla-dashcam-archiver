"""Tiling an event's cameras into one video with ffmpeg.

Each minute is drawn onto a black canvas of fixed length with every camera
overlaid at its grid position, then the minutes are concatenated without
re-encoding. Lessons from real footage that shaped this:

* Tesla clips are often partly or wholly undecodable (a repeater minute can
  probe as 60 s and decode to zero frames). The canvas, not the cameras,
  sets the length: a camera that ends early freezes on its last frame, one
  that yields nothing stays black. (Tiling with xstack stopped at the
  shortest camera and produced an empty minute when one camera was empty.)
* A camera that never yields a single frame stops ffmpeg from building the
  filter graph, so a failed minute is retried without those cameras.
* Decode errors are expected in damaged clips; ``-max_error_rate 1`` keeps
  ffmpeg from giving up on them.
* Hardware *decoding* (VideoToolbox) rejects some Tesla front-camera streams;
  decoding is always software. Hardware *encoding* is fine and much faster.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .events import CAMS_4, CAMS_6, Event


@dataclass
class BuildSettings:
    encoder: str = "auto"          # auto | h264_videotoolbox | libx264 | h264_nvenc | …
    bitrate: str = "6M"
    fps: int = 30
    segment_workers: int = 4
    tile_4: Tuple[int, int] = (960, 720)     # 2×2 grid → 1920×1440
    tile_6: Tuple[int, int] = (640, 480)     # 3×2 grid when pillar cameras exist → 1920×960
    poster_width: int = 480


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not found on PATH ({os.environ.get('PATH')}). "
                           "Install ffmpeg; schedulers (launchd, cron) often need the PATH set explicitly.")


_ENCODER_CACHE: Dict[str, str] = {}


def resolve_encoder(name: str) -> str:
    if name and name != "auto":
        return name
    if "auto" not in _ENCODER_CACHE:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=30).stdout
        _ENCODER_CACHE["auto"] = "h264_videotoolbox" if " h264_videotoolbox " in out else "libx264"
    return _ENCODER_CACHE["auto"]


def encoder_args(encoder: str, bitrate: str, fps: int) -> List[str]:
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", bitrate,
                "-maxrate", bitrate, "-bufsize", bitrate, "-g", str(fps * 2)]
    if encoder == "h264_videotoolbox":
        return ["-c:v", encoder, "-b:v", bitrate, "-profile:v", "high", "-g", str(fps * 2)]
    return ["-c:v", encoder, "-b:v", bitrate, "-g", str(fps * 2)]


def ffprobe_duration(path: str) -> Optional[float]:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def ffprobe_video(path: str) -> Dict[str, Any]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    return {"width": st.get("width"), "height": st.get("height"),
            "duration_sec": float(d.get("format", {}).get("duration") or 0) or None}


def first_frame_ok(path: str) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                        "-show_entries", "frame=pts", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=120)
    return bool(r.stdout.strip())


def encode_segment(cams: Dict[str, str], out: str, layout: List[str], cols: int,
                   tile: Tuple[int, int], s: BuildSettings, _retry: bool = True) -> float:
    """Tile one minute of footage. Returns the segment duration."""
    tw, th = tile
    durs = {c: ffprobe_duration(p) for c, p in cams.items()}
    good = [c for c in layout if durs.get(c)]
    if not good:
        raise RuntimeError("no readable camera in segment")
    dur = max(durs[c] for c in good)
    rows = (len(layout) + cols - 1) // cols
    args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-max_error_rate", "1", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={cols * tw}x{rows * th}:r={s.fps}:d={dur:.3f}"]
    filters, last = [], "[0:v]"
    for k, cam in enumerate(good):
        args += ["-i", cams[cam]]
        r, c = divmod(layout.index(cam), cols)
        filters.append(f"[{k + 1}:v]fps={s.fps},scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                       f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1[c{k}]")
        filters.append(f"{last}[c{k}]overlay={c * tw}:{r * th}:eof_action=repeat:shortest=0[o{k}]")
        last = f"[o{k}]"
    filters.append(f"{last}format=yuv420p[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]", "-t", f"{dur:.3f}",
             *encoder_args(resolve_encoder(s.encoder), s.bitrate, s.fps), "-an", out]
    r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
        usable = {c: cams[c] for c in good if first_frame_ok(cams[c])}
        if _retry and 0 < len(usable) < len(good):
            return encode_segment(usable, out, layout, cols, tile, s, _retry=False)
        raise RuntimeError(f"ffmpeg segment failed: {r.stderr.strip()[-300:]}")
    return dur


def build_composite(ev: Event, work: str, local_files: Dict[str, str], md: Dict[str, Any],
                    s: BuildSettings, log=print) -> Tuple[str, float]:
    """Tile every minute, then concatenate (stream copy). Returns (path, seconds)."""
    segs = ev.media()
    if not segs:
        raise RuntimeError("event has no video segments")
    cams_seen = {c for seg in segs.values() for c in seg}
    six = bool(cams_seen & {"left_pillar", "right_pillar"})
    layout, cols, tile = (CAMS_6, 3, s.tile_6) if six else (CAMS_4, 2, s.tile_4)
    jobs = []
    with ThreadPoolExecutor(max_workers=max(1, s.segment_workers)) as pool:
        for i, (stamp, cams) in enumerate(segs.items()):
            out = os.path.join(work, f"seg{i:04d}.mp4")
            jobs.append((stamp, out, pool.submit(encode_segment,
                                                 {c: local_files[r] for c, r in cams.items()},
                                                 out, layout, cols, tile, s)))
    parts, total = [], 0.0
    for stamp, out, fut in jobs:
        try:
            total += fut.result()
            parts.append(out)
        except RuntimeError as e:
            if len(segs) == 1:
                raise
            log(f"  skip minute {stamp}: {e}")    # one corrupt minute must not sink the event
    if not parts:
        raise RuntimeError("no minute could be encoded")
    lst = os.path.join(work, "concat.txt")
    with open(lst, "w") as f:
        f.write("".join(f"file '{p}'\n" for p in parts))
    final = os.path.join(work, os.path.basename(ev.composite_rel))
    meta = ["-metadata", f"title={md['caption']}",
            "-metadata", f"creation_time={md['utc'].strftime('%Y-%m-%dT%H:%M:%S.000000Z')}",
            "-metadata", "make=Tesla", "-metadata", f"model={md.get('car') or 'Tesla'}"]
    if md.get("gps_lat") is not None:
        meta += ["-metadata", f"location={md['gps_lat']:+08.4f}{md['gps_lon']:+09.4f}/"]
    r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart",
                        *meta, final], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {r.stderr.strip()[-300:]}")
    got = ffprobe_duration(final) or 0
    if got < total - 2 * len(parts) - 1:
        raise RuntimeError(f"composite is {got:.0f}s, expected ~{total:.0f}s")
    for p in parts:
        os.unlink(p)
    return final, total


def make_poster(thumb_png: Optional[str], composite: str, dst: str,
                duration: Optional[float], width: int = 480) -> bool:
    """The car's own thumb.png (what the Tesla app shows) as a JPEG; falls back
    to a frame from the middle of the composite."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    sources: List[Tuple[Optional[float], str]] = []
    if thumb_png and os.path.isfile(thumb_png):
        sources.append((None, thumb_png))
    for t in ([duration / 2] if duration and duration > 2 else []) + [1.0, 0.0]:
        sources.append((t, composite))
    for t, src in sources:
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
        if t is not None:
            args += ["-ss", f"{t:.2f}"]
        args += ["-i", src, "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", dst]
        r = subprocess.run(args, capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return True
    return False
