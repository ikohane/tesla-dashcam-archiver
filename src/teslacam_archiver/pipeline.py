"""promote → build → index, plus importing and adopting existing footage.

    incoming/…   (where Sentry USB drops finished events)
        │ promote: server-side move once nothing has changed for 10 minutes
        ▼
    events/<year>/<Kind>/<event>/   raw camera files + event.json + thumb.png
        │ build: copy to a scratch dir, tile with ffmpeg, upload, check size
        ▼
    composite/<year>/<event>_<kind>.mp4   +   posters/<year>/<event>_<kind>.jpg
        │ index
        ▼
    library.json + index.html at the library root
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from . import build as B
from .config import Config
from .events import (COMPOSITE, EVENTS, INCOMING, KINDS, POSTERS, Event, event_metadata,
                     group_normalized, group_raw, plan_moves)
from .library import Ledger, index_html, library_json, now_iso
from .stores import LocalStore, StoreError, make_store, sha256_file

Log = Callable[[str], None]


class Busy(RuntimeError):
    pass


class JobLock:
    """One job at a time per state directory (a scheduler tick and a manual
    run must never interleave)."""

    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "job.lock")

    def __enter__(self):
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise Busy("another teslacam-archiver job is running")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def job_running(state_dir: str) -> bool:
    try:
        with JobLock(state_dir):
            return False
    except Busy:
        return True


def write_progress(cfg: Config, snap: Dict[str, Any]) -> None:
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        tmp = os.path.join(cfg.state_dir, "progress.json.tmp")
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, os.path.join(cfg.state_dir, "progress.json"))
    except OSError:
        pass


def read_progress(cfg: Config) -> Dict[str, Any]:
    try:
        with open(os.path.join(cfg.state_dir, "progress.json")) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return {"state": "idle"}
    if snap.get("state") == "running" and not job_running(cfg.state_dir):
        snap["state"] = "interrupted"
    return snap


# ---- promote ------------------------------------------------------------------

def promote(store: LocalStore, ledger: Ledger, src_root: str, *, origin: str,
            quiescent_s: Optional[int] = 600, recent_gap_s: int = 75,
            dry_run: bool = False, log: Log = print) -> Dict[str, Any]:
    """Move complete events from a raw TeslaCam tree into events/<year>/<Kind>/<event>/.

    ``quiescent_s=None`` moves everything at once (old footage nobody is writing)."""
    now, listing = store.list(src_root)
    events, encrypted, ignored = group_raw(listing, recent_gap_s=recent_gap_s)
    ready = [e for e in events if quiescent_s is None or now - e.newest_mtime() >= quiescent_s]
    moves = plan_moves(ready, src_root)
    res: Dict[str, Any] = {"events": len(ready), "files": len(moves),
                           "waiting": len(events) - len(ready),
                           "encrypted": len(encrypted), "ignored": len(ignored),
                           "moved": 0, "dup_removed": 0, "gone": 0, "conflicts": [], "errors": []}
    if encrypted:
        log(f"⚠ {len(encrypted)} encrypted clip file(s) in {src_root}: they cannot be read. "
            "Turn off dashcam encryption in the car (Controls › Safety) or decrypt them at "
            "https://dashcam.tesla.com and use `import-local`. See docs/ENCRYPTION.md.")
    if dry_run or not moves:
        return res
    log(f"promote: {len(ready)} event(s), {len(moves)} file(s) from {src_root}"
        + (f" ({res['waiting']} still being written)" if res["waiting"] else ""))
    res.update(store.move_many(moves))
    for ev in ready:
        ledger.upsert(ev, origin=origin)
    ledger.conn.commit()
    dirs = {os.path.dirname(s) for s, _ in moves}
    store.rmdir_empty(d for d in dirs if d != src_root and os.path.basename(d) not in KINDS)
    if res["conflicts"] or res["errors"]:
        log(f"promote: {len(res['conflicts'])} conflict(s), {len(res['errors'])} error(s), "
            f"left in place: {(res['conflicts'] + res['errors'])[:5]}")
    return res


# ---- build --------------------------------------------------------------------

def read_event_json(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def fetch_event(store: LocalStore, ev: Event, scratch: str) -> str:
    work = os.path.join(scratch, f"{ev.kind}-{ev.name}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        store.pull(sorted(ev.files), work)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work


def run_hook(cmd: str, entry: Dict[str, Any], store: LocalStore, log: Log) -> None:
    """``[hooks] after_event``: a shell command run after each event is built.
    It gets the event as JSON on stdin and in $TESLACAM_EVENT_JSON, plus
    $TESLACAM_COMPOSITE / $TESLACAM_POSTER (paths as the library sees them)."""
    env = dict(os.environ,
               TESLACAM_EVENT_JSON=json.dumps(entry),
               TESLACAM_COMPOSITE=store.abspath(entry["composite"]),
               TESLACAM_POSTER=store.abspath(entry["poster"]) if entry.get("poster") else "",
               TESLACAM_LIBRARY=store.describe())
    r = subprocess.run(cmd, shell=True, input=json.dumps(entry), capture_output=True,
                       text=True, env=env, timeout=600)
    if r.returncode != 0:
        log(f"  hook failed (rc={r.returncode}): {r.stderr.strip()[-200:]}")


def process_event(cfg: Config, store: LocalStore, ledger: Ledger, ev: Event,
                  work: Optional[str] = None, log: Log = print) -> Dict[str, Any]:
    """Build one event's composite and poster, upload them, record the result.

    The ledger says 'done' only after the composite is on the library with the
    right size, so a crash at any point just rebuilds the event next time."""
    work = work or fetch_event(store, ev, cfg.scratch)
    try:
        local = {rel: os.path.join(work, os.path.basename(rel)) for rel in ev.files}
        md = event_metadata(ev, read_event_json(local.get(ev.sidecar("event.json") or "")),
                            cfg.tz, cfg.cars)
        t0 = time.time()
        final, footage = B.build_composite(ev, work, local, md, cfg.build, log=log)
        info = B.ffprobe_video(final)
        size = os.path.getsize(final)
        store.push(final, ev.composite_rel)
        got = store.size(ev.composite_rel)
        if got != size:
            raise StoreError(f"uploaded size {got} != {size}")
        poster_local = os.path.join(work, "poster.jpg")
        poster_rel = None
        if B.make_poster(local.get(ev.sidecar("thumb.png") or ""), final, poster_local,
                         info["duration_sec"] or footage, cfg.build.poster_width):
            store.push(poster_local, ev.poster_rel)
            poster_rel = ev.poster_rel
        entry = {"event_key": ev.key, "kind": ev.kind, "name": ev.name,
                 "composite": ev.composite_rel, "poster": poster_rel,
                 "size_bytes": size, "sha256": sha256_file(final),
                 "duration_sec": info["duration_sec"] or footage,
                 "width": info["width"], "height": info["height"],
                 "taken_at": md["taken_at"], "caption": md["caption"], "city": md["city"],
                 "gps_lat": md["gps_lat"], "gps_lon": md["gps_lon"], "reason": md["reason"],
                 "car": md["car"]}
        ledger.upsert(ev, status="done", error=None, attempts=0, signature=ev.signature(),
                      raw_files=len(ev.files), raw_bytes=sum(s for s, _ in ev.files.values()),
                      **{k: v for k, v in entry.items() if k not in ("event_key", "kind", "name")})
        ledger.conn.commit()
        log(f"  ✓ {ev.key}: {footage:.0f}s of footage, {size / 1e6:.0f} MB, "
            f"built in {time.time() - t0:.0f}s")
        if cfg.after_event:
            run_hook(cfg.after_event, entry, store, log)
        return entry
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_index(store: LocalStore, ledger: Ledger) -> int:
    events = ledger.done_events()
    store.write_text("library.json", library_json(events))
    store.write_text("index.html", index_html(events))
    return len(events)


def run(cfg: Config, *, store: Optional[LocalStore] = None, max_minutes: float = 45.0,
        max_events: Optional[int] = None, log: Log = print) -> Dict[str, Any]:
    """One scheduled tick: promote incoming, build pending events within a
    time budget (the rest continue next tick), refresh the index."""
    rec: Dict[str, Any] = {"started_at": now_iso(), "status": "running", "built": 0, "failed": 0}
    with JobLock(cfg.state_dir):
        os.makedirs(cfg.scratch, exist_ok=True)
        ledger = Ledger(cfg.ledger_path)
        rid = ledger.start_run()
        progress: Dict[str, Any] = {"state": "running", "started_at": rec["started_at"],
                                    "phase": "starting"}
        write_progress(cfg, progress)
        try:
            B.require_tools()
            store = store or make_store(cfg.library)
            store.mkdirs([INCOMING, EVENTS, COMPOSITE, POSTERS])
            progress["phase"] = "promote"
            write_progress(cfg, progress)
            p = promote(store, ledger, INCOMING, origin="incoming",
                        quiescent_s=cfg.quiescent_seconds, recent_gap_s=cfg.recent_gap_seconds,
                        log=log)
            rec.update(promoted_events=p["events"], promoted_files=p["files"],
                       waiting=p["waiting"], encrypted=p["encrypted"],
                       conflicts=len(p["conflicts"]) + len(p["errors"]))
            _now, listing = store.list(EVENTS)
            todo = ledger.pending(group_normalized(listing))
            limit = len(todo) if max_events is None else min(len(todo), max_events)
            deadline = time.time() + max_minutes * 60
            # Copying an event to this computer takes about as long as encoding
            # it, so the next event is fetched in the background.
            pool = ThreadPoolExecutor(max_workers=1)
            ahead = None
            try:
                for i, ev in enumerate(todo[:limit]):
                    if time.time() > deadline:
                        break
                    progress.update(phase="build", detail=f"{ev.key} ({i + 1}/{len(todo)})",
                                    pct=int(100 * i / max(1, limit)))
                    write_progress(cfg, progress)
                    cur, ahead = (ahead or pool.submit(fetch_event, store, ev, cfg.scratch)), None
                    try:
                        work = cur.result()
                        if i + 1 < limit and time.time() < deadline:
                            ahead = pool.submit(fetch_event, store, todo[i + 1], cfg.scratch)
                        process_event(cfg, store, ledger, ev, work=work, log=log)
                        rec["built"] += 1
                    except Exception as e:  # noqa: BLE001 — one bad event must not stop the run
                        rec["failed"] += 1
                        n = ledger.record_failure(ev, f"{type(e).__name__}: {e}")
                        log(f"  ✗ {ev.key} (attempt {n}): {type(e).__name__}: {e}")
            finally:
                if ahead is not None:
                    try:
                        shutil.rmtree(ahead.result(), ignore_errors=True)
                    except Exception:  # noqa: BLE001
                        pass
                pool.shutdown(wait=True)
            rec["remaining"] = len(todo) - rec["built"]
            if rec["built"] or not store.size("index.html"):
                progress["phase"] = "index"
                write_progress(cfg, progress)
                write_index(store, ledger)
            rec["status"] = "done"
        except Exception as e:  # noqa: BLE001
            rec.update(status="error", error=f"{type(e).__name__}: {e}")
            log(f"teslacam-archiver: {rec['error']}")
        finally:
            ledger.finish_run(rid, rec)
            ledger.close()
            write_progress(cfg, {"state": rec["status"], "result": rec, "finished_at": now_iso()})
    return rec


# ---- bringing in existing footage --------------------------------------------------

def adopt(cfg: Config, src: str, *, store: Optional[LocalStore] = None, dry_run: bool = False,
          log: Log = print) -> Dict[str, Any]:
    """Move an existing TeslaCam dump that already lives where the library
    lives (same disk, or same NAS) into events/. A move, not a copy."""
    with JobLock(cfg.state_dir):
        store = store or make_store(cfg.library)
        if not src.startswith("/"):
            raise StoreError("adopt needs an absolute path on the library's disk or server")
        ledger = Ledger(cfg.ledger_path)
        try:
            store.mkdirs([INCOMING, EVENTS, COMPOSITE, POSTERS])
            res = promote(store, ledger, src.rstrip("/"), origin=f"adopt:{src}",
                          quiescent_s=None, recent_gap_s=cfg.recent_gap_seconds,
                          dry_run=dry_run, log=log)
            if not dry_run and res["files"]:
                store.rmdir_empty([f"{src.rstrip('/')}/{k}" for k in KINDS])
        finally:
            ledger.close()
    return res


def import_local(cfg: Config, src_dir: str, *, store: Optional[LocalStore] = None,
                 log: Log = print) -> Dict[str, Any]:
    """Copy a TeslaCam tree on this computer (a USB stick, an old download)
    into events/, then sha256-check every file on both sides. Never changes
    or deletes the source and never overwrites a file already in the library.
    An audit of every file → destination → hash is written to the state dir."""
    root = os.path.abspath(os.path.expanduser(src_dir)).rstrip("/")
    if os.path.isdir(os.path.join(root, "TeslaCam")):
        root = os.path.join(root, "TeslaCam")
    listing = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".") and d != "@eaDir"]
        for fn in fns:
            if fn.startswith("."):
                continue
            p = os.path.join(dp, fn)
            st = os.stat(p)
            listing.append((os.path.relpath(p, root), st.st_size, st.st_mtime))
    events, encrypted, ignored = group_raw(listing, recent_gap_s=cfg.recent_gap_seconds)
    if encrypted:
        raise StoreError(f"{len(encrypted)} encrypted clip file(s): decrypt them first at "
                         "https://dashcam.tesla.com (see docs/ENCRYPTION.md)")
    if not events:
        raise StoreError(f"no SavedClips/SentryClips/RecentClips under {src_dir}")
    with JobLock(cfg.state_dir):
        store = store or make_store(cfg.library)
        plan: Dict[str, str] = {}
        for ev in events:
            for rel in ev.files:
                plan[os.path.join(root, rel)] = f"{ev.dest_dir}/{os.path.basename(rel)}"
        if len(set(plan.values())) != len(plan):
            raise StoreError("two source files map to the same destination; not importing")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        os.makedirs(cfg.scratch, exist_ok=True)
        tree = tempfile.mkdtemp(prefix=f"import-{stamp}-", dir=cfg.scratch)
        try:
            for src, dst in plan.items():
                link = os.path.join(tree, os.path.relpath(dst, EVENTS))
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(src, link)
            total = sum(os.path.getsize(p) for p in plan)
            log(f"import: {len(events)} event(s), {len(plan)} file(s), {total / 1e9:.1f} GB "
                f"→ {store.describe()}/{EVENTS}")
            store.mkdirs([INCOMING, EVENTS, COMPOSITE, POSTERS])
            store.upload_tree(tree, EVENTS)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
        log("import: checking sha256 on both sides")
        with ThreadPoolExecutor(max_workers=4) as pool:
            local_h = dict(zip(plan, pool.map(sha256_file, plan)))
        remote_h = store.sha256_many(list(plan.values()),
                                     sizes={plan[p]: os.path.getsize(p) for p in plan})
        verified = {p for p, d in plan.items() if local_h[p] and local_h[p] == remote_h.get(d)}
        bad = sorted(set(plan) - verified)
        ledger = Ledger(cfg.ledger_path)
        try:
            for ev in events:
                ledger.upsert(ev, origin=f"import:{src_dir}")
            ledger.conn.commit()
        finally:
            ledger.close()
        audit_dir = os.path.join(cfg.state_dir, "imports")
        os.makedirs(audit_dir, exist_ok=True)
        audit = os.path.join(audit_dir, f"import-{stamp}.json")
        res = {"events": len(events), "files": len(plan), "bytes": total,
               "verified": len(verified), "not_verified_count": len(bad),
               "not_verified": bad[:50], "ignored": ignored[:50], "audit": audit}
        with open(audit, "w") as f:
            json.dump({"src": src_dir, "library": store.describe(), "result": res,
                       "files": [{"src": p, "dst": d, "sha256": local_h[p], "verified": p in verified}
                                 for p, d in plan.items()]}, f, indent=1)
    log(f"import: {len(verified)}/{len(plan)} verified"
        + (f", {len(bad)} NOT verified" if bad else "") + f"; audit {audit}")
    return res
