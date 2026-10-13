"""teslacam-archiver command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib import resources
from typing import List, Optional

from . import __version__
from . import build as B
from .config import DEFAULT_CONFIG, ConfigError, load, local_zone_name
from .library import Ledger
from .pipeline import (Busy, adopt, import_local, job_running, read_progress, run,
                       write_index)
from .stores import SshStore, StoreError, make_store


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def cmd_init(a) -> int:
    path = os.path.expanduser(a.config or DEFAULT_CONFIG)
    if os.path.exists(path) and not a.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    text = resources.files("teslacam_archiver").joinpath("config.example.toml").read_text()
    text = text.replace('timezone = "America/New_York"', f'timezone = "{local_zone_name()}"')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    print(f"wrote {path} — set [library] path, then run `teslacam-archiver check`")
    return 0


def cmd_check(a) -> int:
    cfg = load(a.config)
    ok = True
    print(f"config:   {cfg.source}")
    print(f"timezone: {cfg.timezone}")
    for t in ("ffmpeg", "ffprobe"):
        p = shutil.which(t)
        print(f"{t}:   {p or 'MISSING'}")
        ok &= bool(p)
    if ok:
        print(f"encoder:  {B.resolve_encoder(cfg.build.encoder)}")
    store = make_store(cfg.library)
    print(f"library:  {store.describe()} ({store.name})")
    if isinstance(store, SshStore):
        reach = store.ping()
        print(f"ssh:      {'ok' if reach else 'UNREACHABLE'}")
        ok &= reach
        if reach:
            try:
                from .stores import pick_rsync
                print(f"rsync:    {pick_rsync()}")
            except StoreError as e:
                print(f"rsync:    {e}")
                ok = False
    else:
        parent = store.base if os.path.isdir(store.base) else os.path.dirname(store.base)
        w = os.access(parent, os.W_OK)
        print(f"writable: {'yes' if w else 'NO'}")
        ok &= w
    print(f"state:    {cfg.state_dir}")
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def cmd_run(a) -> int:
    cfg = load(a.config)
    if job_running(cfg.state_dir):
        print("another teslacam-archiver job is running; nothing to do")
        return 0
    rec = run(cfg, max_minutes=a.max_minutes, max_events=a.max_events)
    _print(rec)
    return 0 if rec["status"] == "done" else 1


def cmd_import(a) -> int:
    res = import_local(load(a.config), a.src)
    _print(res)
    return 0 if not res["not_verified_count"] else 1


def cmd_adopt(a) -> int:
    res = adopt(load(a.config), a.path, dry_run=a.dry_run)
    _print(res)
    return 0 if not (res.get("conflicts") or res.get("errors")) else 1


def cmd_index(a) -> int:
    cfg = load(a.config)
    ledger = Ledger(cfg.ledger_path)
    try:
        n = write_index(make_store(cfg.library), ledger)
    finally:
        ledger.close()
    print(f"index written: {n} events")
    return 0


def cmd_status(a) -> int:
    cfg = load(a.config)
    ledger = Ledger(cfg.ledger_path)
    try:
        _print({**ledger.summary(), "progress": read_progress(cfg)})
    finally:
        ledger.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="teslacam-archiver",
                                 description="Archive Tesla Dashcam/Sentry clips into a browsable library.")
    ap.add_argument("--config", help=f"config file (default {DEFAULT_CONFIG}, or $TESLACAM_CONFIG)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="write a starter config file")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("check", help="check tools, config and the library location").set_defaults(fn=cmd_check)
    s = sub.add_parser("run", help="promote new events, build composites, refresh the index")
    s.add_argument("--max-minutes", type=float, default=45.0)
    s.add_argument("--max-events", type=int)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("import-local", help="copy + verify a TeslaCam folder (USB stick) into the library")
    s.add_argument("src")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("adopt", help="move an existing TeslaCam dump on the library's disk/server into it")
    s.add_argument("path")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_adopt)
    sub.add_parser("index", help="rewrite library.json and index.html").set_defaults(fn=cmd_index)
    sub.add_parser("status", help="ledger summary, recent runs, failures").set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    try:
        return a.fn(a)
    except (ConfigError, StoreError, Busy) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
