"""End-to-end tests. The library is a temp folder (LocalStore) and the footage
is tiny ffmpeg test patterns, so promote → build → index runs for real.

    python -m unittest discover -s tests
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from teslacam_archiver import events as E                     # noqa: E402
from teslacam_archiver import pipeline as P                   # noqa: E402
from teslacam_archiver.config import ConfigError, from_dict   # noqa: E402
from teslacam_archiver.library import MAX_ATTEMPTS, Ledger    # noqa: E402
from teslacam_archiver.stores import LocalStore, SshStore, StoreError, make_store  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
_CLIPS = {}


def clip(seconds: float = 2.0) -> bytes:
    if seconds not in _CLIPS:
        d = tempfile.mkdtemp()
        out = os.path.join(d, "c.mp4")
        subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size=320x240:rate=30:duration={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", out], check=True)
        with open(out, "rb") as f:
            _CLIPS[seconds] = f.read()
        shutil.rmtree(d)
    return _CLIPS[seconds]


def read(path: str, mode: str = "r"):
    with open(path, mode) as f:
        return f.read()


def png(path: str) -> None:
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=64x48", "-frames:v", "1", path], check=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib = os.path.join(self.tmp, "library")
        os.makedirs(self.lib)
        self.store = LocalStore(self.lib)
        self.cfg = from_dict({"library": {"backend": "local", "path": self.lib},
                              "state": {"dir": os.path.join(self.tmp, "state")},
                              "car": {"timezone": "America/New_York"},
                              "cars": [{"until": "2022-01-01", "label": "Old car"},
                                       {"label": "New car"}]})
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, rel: str, data: bytes = b"", old: bool = True) -> str:
        p = rel if rel.startswith("/") else os.path.join(self.lib, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if old:
            os.utime(p, (1_600_000_000, 1_600_000_000))
        return p

    def event(self, root, kind_dir, name, minutes=("12-00-00",), cams=E.CAMS_4, meta=None,
              thumb=True, data=None):
        day = name[:10]
        for m in minutes:
            for c in cams:
                self.put(f"{root}/{kind_dir}/{name}/{day}_{m}-{c}.mp4", data or clip())
        if meta is not None:
            self.put(f"{root}/{kind_dir}/{name}/event.json", json.dumps(meta).encode())
        if thumb:
            png(self.put(f"{root}/{kind_dir}/{name}/thumb.png"))

    def run_job(self, **kw):
        return P.run(self.cfg, store=self.store, log=self.logs.append, **kw)

    def promote(self, src=E.INCOMING, **kw):
        led = Ledger(self.cfg.ledger_path) if os.path.isdir(self.cfg.state_dir) else None
        if led is None:
            os.makedirs(self.cfg.state_dir)
            led = Ledger(self.cfg.ledger_path)
        try:
            return P.promote(self.store, led, src, origin="test", log=self.logs.append, **kw)
        finally:
            led.close()

    def ledger(self):
        c = sqlite3.connect(self.cfg.ledger_path)
        c.row_factory = sqlite3.Row
        return closing(c)


class GroupingTests(unittest.TestCase):
    def test_kinds_drives_encrypted_and_sidecars(self):
        L = [("SavedClips/2021-06-01_10-00-00/2021-06-01_09-59-00-front.mp4", 10, 0),
             ("SavedClips/2021-06-01_10-00-00/event.json", 1, 0),
             ("SavedClips/2021-06-01_10-00-00/thumb.png", 1, 0),
             ("SavedClips/2021-06-01_10-00-00/event.mp4", 1, 0),
             ("SavedClips/2021-06-01_10-00-00/@eaDir/x.jpg", 1, 0),
             ("SentryClips/2021-06-02_01-00-00/2021-06-02_00-59-00-back.mp4", 10, 0),
             ("RecentClips/2021-06-03_08-00-00-front.mp4", 10, 0),
             ("RecentClips/2021-06-03_08-01-01-front.mp4", 10, 0),
             ("RecentClips/2021-06-03_08-05-00-front.mp4", 10, 0),
             ("RecentClips/.2021-06-03_08-06-00-front.mp4.Xyz", 10, 0),
             ("EncryptedClips/abc.bin", 10, 0),
             ("SavedClips/2021-06-01_10-00-00/notes.txt", 1, 0)]
        evs, enc, ign = E.group_raw(L)
        self.assertEqual([e.key for e in evs],
                         ["recent/2021-06-03_08-00-00", "recent/2021-06-03_08-05-00",
                          "saved/2021-06-01_10-00-00", "sentry/2021-06-02_01-00-00"])
        self.assertEqual(len(evs[2].files), 4)
        self.assertEqual(list(evs[2].media()), ["2021-06-01_09-59-00"])   # event.mp4 is not a minute
        self.assertEqual(enc, ["EncryptedClips/abc.bin"])
        self.assertIn("SavedClips/2021-06-01_10-00-00/notes.txt", ign)
        self.assertEqual(evs[3].composite_rel, "composite/2021/2021-06-02_01-00-00_sentry.mp4")
        self.assertEqual(evs[3].dest_dir, "events/2021/SentryClips/2021-06-02_01-00-00")

    def test_pi_prefix_and_titled_folder(self):
        evs, _, ign = E.group_raw([
            ("TeslaCam/SentryClips/2026-11-01_10-00-00/2026-11-01_09-59-00-left_pillar.mp4", 5, 0),
            ("SavedClips/2020-06-14_15-32-11_Graduation/2020-06-14_15-31-00-front.mp4", 5, 0)])
        self.assertEqual([e.key for e in evs], ["saved/2020-06-14_15-32-11_Graduation",
                                                "sentry/2026-11-01_10-00-00"])
        self.assertEqual(ign, [])
        from zoneinfo import ZoneInfo
        md = E.event_metadata(evs[0], {}, ZoneInfo("America/New_York"))
        self.assertEqual(md["caption"], "Graduation (saved dashcam clip)")
        self.assertEqual(md["taken_at"], "2020-06-14T15:32:11-04:00")

    def test_metadata_time_zone_gps_and_car(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        cars = [("2022-01-01", "Old car"), ("", "New car")]
        ev = E.Event("sentry", "2023-07-25_18-54-51")
        md = E.event_metadata(ev, {"timestamp": "2023-07-25T18:54:06", "city": "Boston",
                                   "est_lat": "42.3451", "est_lon": "-71.0846",
                                   "reason": "sentry_aware_object_detection"}, tz, cars)
        self.assertEqual(md["taken_at"], "2023-07-25T18:54:06-04:00")
        self.assertEqual(md["caption"], "Sentry Mode event: object detected nearby — Boston")
        self.assertEqual(md["car"], "New car")
        self.assertEqual(E.event_metadata(E.Event("saved", "2021-01-05_08-00-00"), {}, tz, cars)
                         ["taken_at"], "2021-01-05T08:00:00-05:00")
        self.assertEqual(E.car_for("2021-12-31_00-00-00", cars), "Old car")
        self.assertIsNone(E.event_metadata(ev, {"est_lat": "0", "est_lon": "0"}, tz)["gps_lat"])


class ConfigTests(unittest.TestCase):
    def test_requirements_and_backends(self):
        with self.assertRaises(ConfigError):
            from_dict({"library": {}})
        with self.assertRaises(StoreError):
            make_store({"backend": "ssh", "path": "/x"})
        with self.assertRaises(StoreError):
            make_store({"backend": "ftp", "path": "/x"})
        self.assertIsInstance(make_store({"backend": "local", "path": "/tmp/x"}), LocalStore)

    def test_ssh_command_is_bounded_and_carries_options(self):
        s = SshStore(host="nas.local", base="/volume1/v/lib", user="me", key="~/.ssh/k",
                     port=2222, jump="me@gw")
        argv = s.ssh_base()
        joined = " ".join(argv)
        for opt in ("BatchMode=yes", "ServerAliveInterval=30", "ServerAliveCountMax=3",
                    "ProxyJump=me@gw", "-p 2222", "IdentitiesOnly=yes"):
            self.assertIn(opt, joined)
        self.assertEqual(argv[-1], "me@nas.local")
        self.assertEqual(s.abspath("events/x"), "/volume1/v/lib/events/x")
        self.assertEqual(s.abspath("/elsewhere/TeslaCam"), "/elsewhere/TeslaCam")


class PromoteTests(Base):
    def test_moves_into_tree_and_is_idempotent(self):
        self.event(E.INCOMING, "SentryClips", "2020-03-15_06-55-40", meta={"city": "Boston"})
        self.put(f"{E.INCOMING}/RecentClips/2021-12-14_10-43-17-front.mp4", b"x")
        res = self.promote(quiescent_s=None)
        self.assertEqual((res["events"], res["moved"]), (2, 4 + 2 + 1))
        dest = os.path.join(self.lib, "events/2020/SentryClips/2020-03-15_06-55-40")
        self.assertEqual(len(os.listdir(dest)), 6)
        self.assertFalse(os.path.exists(os.path.join(self.lib, E.INCOMING,
                                                     "SentryClips/2020-03-15_06-55-40")))
        self.assertEqual(self.promote(quiescent_s=None)["files"], 0)

    def test_resume_removes_identical_source_only(self):
        self.event(E.INCOMING, "SavedClips", "2021-06-01_10-00-00", cams=["front", "back"], thumb=False)
        src = f"{E.INCOMING}/SavedClips/2021-06-01_10-00-00"
        dst = "events/2021/SavedClips/2021-06-01_10-00-00"
        shutil.copy2(os.path.join(self.lib, src, "2021-06-01_12-00-00-front.mp4"),
                     self.put(f"{dst}/2021-06-01_12-00-00-front.mp4"))
        self.put(f"{dst}/2021-06-01_12-00-00-back.mp4", b"different")
        res = self.promote(quiescent_s=None)
        self.assertEqual((res["dup_removed"], len(res["conflicts"])), (1, 1))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, src, "2021-06-01_12-00-00-back.mp4")))

    def test_waits_for_quiet_and_reports_encryption(self):
        self.event(E.INCOMING, "SentryClips", "2026-10-01_10-00-00", thumb=False)
        self.put(f"{E.INCOMING}/SentryClips/2026-10-01_10-00-00/2026-10-01_12-01-00-front.mp4",
                 b"x", old=False)
        self.put(f"{E.INCOMING}/EncryptedClips/2026-10-01_11-00-00/a.enc", b"x")
        res = self.promote(quiescent_s=600)
        self.assertEqual((res["events"], res["waiting"], res["encrypted"]), (0, 1, 1))
        self.assertTrue(any("encrypted" in m for m in self.logs))
        self.assertEqual(self.promote(quiescent_s=0)["events"], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.lib, E.INCOMING, "EncryptedClips/2026-10-01_11-00-00/a.enc")))

    def test_adopt_moves_an_existing_dump(self):
        dump = os.path.join(self.tmp, "OldCar/TeslaCam")
        self.event(dump, "SavedClips", "2019-05-01_10-00-00", thumb=False)
        res = P.adopt(self.cfg, dump, store=self.store, log=self.logs.append)
        self.assertEqual(res["moved"], 4)
        self.assertTrue(os.path.isdir(os.path.join(self.lib, "events/2019/SavedClips/2019-05-01_10-00-00")))
        self.assertFalse(os.path.exists(os.path.join(dump, "SavedClips")))


class BuildTests(Base):
    META = {"timestamp": "2023-07-25T18:54:06", "city": "Boston", "est_lat": "42.3451",
            "est_lon": "-71.0846", "reason": "sentry_aware_object_detection"}

    def test_end_to_end_index_and_rebuild(self):
        self.event(E.INCOMING, "SentryClips", "2023-07-25_18-54-51",
                   minutes=("18-53-50", "18-54-51"), meta=self.META)
        self.event(E.INCOMING, "SavedClips", "2021-07-26_09-00-00", cams=["front", "back"], thumb=False)
        self.cfg.quiescent_seconds = 0
        rec = self.run_job()
        self.assertEqual((rec["status"], rec["built"], rec["failed"]), ("done", 2, 0), self.logs)
        comp = os.path.join(self.lib, "composite/2023/2023-07-25_18-54-51_sentry.mp4")
        self.assertTrue(os.path.isfile(comp))
        poster = os.path.join(self.lib, "posters/2023/2023-07-25_18-54-51_sentry.jpg")
        # the car's own 64-px thumb.png, not a 480-px frame of the video
        from teslacam_archiver.build import ffprobe_video
        self.assertEqual(ffprobe_video(poster)["width"], 64)
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "posters/2021/2021-07-26_09-00-00_saved.jpg")))
        with self.ledger() as c:
            r = c.execute("SELECT * FROM events WHERE kind='sentry'").fetchone()
            self.assertEqual((r["status"], r["width"], r["height"]), ("done", 1920, 1440))
            self.assertEqual(r["taken_at"], "2023-07-25T18:54:06-04:00")
            self.assertEqual(r["car"], "New car")
            self.assertGreater(r["duration_sec"], 3.5)
            self.assertEqual(c.execute("SELECT car FROM events WHERE kind='saved'").fetchone()[0], "Old car")
        lib = json.loads(read(os.path.join(self.lib, "library.json")))
        self.assertEqual(len(lib["events"]), 2)
        page = read(os.path.join(self.lib, "index.html"))
        self.assertIn("composite/2023/2023-07-25_18-54-51_sentry.mp4", page)
        self.assertEqual(sorted(os.listdir(self.cfg.scratch)), [])          # scratch cleaned
        self.assertEqual(self.run_job()["built"], 0)                        # nothing to do
        self.put("events/2021/SavedClips/2021-07-26_09-00-00/2021-07-26_12-00-00-left_repeater.mp4", clip())
        self.assertEqual(self.run_job()["built"], 1)                        # raw changed → rebuilt

    def test_failed_event_is_held_after_max_attempts(self):
        self.put("events/2022/SentryClips/2022-05-01_10-00-00/2022-05-01_09-59-00-front.mp4", b"junk")
        for _ in range(MAX_ATTEMPTS):
            self.assertEqual(self.run_job()["failed"], 1)
        rec = self.run_job()
        self.assertEqual((rec["built"], rec["failed"]), (0, 0))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "composite/2022")))

    def test_corrupt_minute_and_undecodable_camera(self):
        name = "2022-05-02_10-00-00"
        self.event("events/2022", "SentryClips", name, minutes=("09-58-00", "09-59-00"), thumb=False,
                   data=clip(3.0))
        for c in E.CAMS_4:   # first minute: garbage in every camera → skipped
            self.put(f"events/2022/SentryClips/{name}/2022-05-02_09-58-00-{c}.mp4", b"garbage")
        good = clip(3.0)     # second minute: one camera probes fine but decodes to nothing
        a, b = good.index(b"mdat") + 4, good.index(b"moov") - 4
        self.put(f"events/2022/SentryClips/{name}/2022-05-02_09-59-00-left_repeater.mp4",
                 good[:a] + bytes(b - a) + good[b:])
        rec = self.run_job()
        self.assertEqual(rec["built"], 1, self.logs)
        with self.ledger() as c:
            self.assertGreater(c.execute("SELECT duration_sec FROM events").fetchone()[0], 2.8)

    def test_six_camera_layout(self):
        self.event("events/2026", "SentryClips", "2026-10-02_10-00-00", cams=E.CAMS_6, thumb=False)
        self.assertEqual(self.run_job()["built"], 1, self.logs)
        with self.ledger() as c:
            self.assertEqual(tuple(c.execute("SELECT width, height FROM events").fetchone()), (1920, 960))

    def test_short_upload_is_refused(self):
        class Truncating(LocalStore):
            def push(self, local, rel):
                super().push(local, rel)
                if rel.endswith(".mp4"):
                    with open(self.abspath(rel), "r+b") as f:
                        f.truncate(100)
        self.store = Truncating(self.lib)
        self.event("events/2022", "SavedClips", "2022-05-03_10-00-00", thumb=False)
        rec = self.run_job()
        self.assertEqual((rec["built"], rec["failed"]), (0, 1))
        with self.ledger() as c:
            self.assertIn("uploaded size", c.execute("SELECT error FROM events").fetchone()[0])

    def test_after_event_hook(self):
        out = os.path.join(self.tmp, "hook.json")
        self.cfg.after_event = f"cat > {out}"
        self.event("events/2023", "SavedClips", "2023-01-01_10-00-00", thumb=False)
        self.assertEqual(self.run_job()["built"], 1)
        got = json.loads(read(out))
        self.assertEqual(got["composite"], "composite/2023/2023-01-01_10-00-00_saved.mp4")


class ImportTests(Base):
    def source(self):
        src = os.path.join(self.tmp, "STICK", "TeslaCam")
        for rel, data in [("SentryClips/2023-06-26_14-11-46/2023-06-26_14-10-26-front.mp4", b"front"),
                          ("SentryClips/2023-06-26_14-11-46/event.json", b"{}"),
                          ("SavedClips/2023-07-01_10-00-00/2023-07-01_09-59-00-back.mp4", b"back"),
                          ("RecentClips/2023-07-02_08-00-00-front.mp4", b"recent")]:
            self.put(os.path.join(src, rel), data)
        return os.path.dirname(src)

    def test_copy_verify_never_touch_source(self):
        stick = self.source()
        res = P.import_local(self.cfg, stick, store=self.store, log=self.logs.append)
        self.assertEqual((res["files"], res["verified"], res["not_verified_count"]), (4, 4, 0))
        self.assertTrue(os.path.isfile(os.path.join(
            self.lib, "events/2023/RecentClips/2023-07-02_08-00-00/2023-07-02_08-00-00-front.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(
            stick, "TeslaCam/SavedClips/2023-07-01_10-00-00/2023-07-01_09-59-00-back.mp4")))
        self.assertTrue(os.path.isfile(res["audit"]))
        again = P.import_local(self.cfg, stick, store=self.store, log=self.logs.append)
        self.assertEqual(again["verified"], 4)

    def test_differing_file_is_not_overwritten(self):
        stick = self.source()
        dst = "events/2023/SavedClips/2023-07-01_10-00-00/2023-07-01_09-59-00-back.mp4"
        self.put(dst, b"OTHER")
        res = P.import_local(self.cfg, stick, store=self.store, log=self.logs.append)
        self.assertEqual((res["verified"], res["not_verified_count"]), (3, 1))
        self.assertEqual(read(os.path.join(self.lib, dst), "rb"), b"OTHER")


if __name__ == "__main__":
    unittest.main()
