# teslacam-archiver

Keep every Tesla Dashcam and Sentry Mode clip, automatically, without ever
pulling a USB stick or logging in to anything — and browse them as one video
per event instead of thousands of one-minute, one-camera files.

```
 car ──USB──▶ Raspberry Pi (Sentry USB) ──home Wi-Fi──▶ your library ◀── this tool (on a Mac or Linux box)
              looks like a USB drive        SMB/NFS/rsync/rclone         promote → tile → index
```

## The use case

A Tesla records four (newer cars: six) cameras to a USB drive in the glovebox,
one MP4 per camera per minute. A single 10-minute Sentry event is 40+ files and
over 1 GB. Getting them off the car means pulling the stick, and the official
viewers (the phone app and the 2026 web viewer) are for looking at a clip, not
for keeping years of them.

This project is the second half of a two-part setup:

1. **In the car**, a Raspberry Pi running [Sentry USB](https://github.com/Sentry-Six/Sentry-USB-Rusty)
   replaces the USB stick. The car records to it as if it were a stick; when the
   car comes home, the Pi copies finished events over Wi-Fi to a folder you
   choose (a NAS share, a folder on your computer, or a cloud drive).
2. **On a computer**, `teslacam-archiver` runs every 30 minutes and turns that
   drop folder into a library:
   * **promote**: moves each finished event into `events/<year>/<Kind>/<event>/`
     once nothing has changed for 10 minutes;
   * **build**: tiles all cameras of an event into **one video**
     (`composite/<year>/<event>_<kind>.mp4`, 2×2 or 3×2), with the date, place
     and reason from the car's `event.json` in the file's metadata;
   * **poster**: keeps the car's own thumbnail (the picture the Tesla app shows)
     as `posters/…jpg`;
   * **index**: writes `index.html`, a self-contained gallery you can open from
     the folder in any browser, and `library.json` for scripts.

No Tesla account, API token or app login is involved at any point: the only
credential in the chain is the share password the Pi uses, which never expires.

Already have footage? `import-local` copies a USB stick or an old download into
the library with a SHA-256 check of every file, and `adopt` moves a TeslaCam
dump that already sits on the same disk or NAS. Without a Pi at all you can
still use this tool: plug the stick into the computer now and then and run
`import-local`.

## Read this first: dashcam encryption depends on your car

From software **2026.20**, cars with the **AMD Ryzen** infotainment computer
(Model 3 and Model Y built since about late 2021, refreshed Model S/X,
Cybertruck) can encrypt new dashcam clips with a key tied to your Tesla
account. Encrypted clips **cannot be read by Sentry USB or by this tool**.
Intel Atom (MCU2) cars are not affected. Where the feature exists it is on by
default and can be turned off under **Controls › Safety** (dashcam / USB
storage options); the rollout is staged by region, so your car may not show
the setting yet. This tool detects an `EncryptedClips` folder, leaves it alone
and tells you. Details, how to tell which computer your car has, and the
privacy trade-off: **[docs/ENCRYPTION.md](docs/ENCRYPTION.md)**.

## Hardware

What the reference setup uses (about $110–150 in 2026); alternatives and
lessons in **[docs/HARDWARE.md](docs/HARDWARE.md)**.

| Part | Used | Notes |
|---|---|---|
| Computer | Raspberry Pi 4 Model B (2 GB is plenty) | Pi 5 also supported by Sentry USB; Pi Zero 2 W works but is slower and 2.4 GHz-only |
| Storage | 256 GB **high-endurance** microSD | dashcam writes wear out ordinary cards |
| Cable | 1 ft USB-A → USB-C **data** cable, 3 A | goes into the Pi's USB-C port (power + data); not the four USB-A ports |
| Case | Argon NEO (aluminium) | a metal case + glovebox weakens Wi-Fi: lock the Pi to 2.4 GHz (see below) or use a plastic case |
| Car port | the glovebox USB-A port (2021+ Model 3/Y); front console port on older cars | rear ports are charge-only |

**Wi-Fi reach is the thing most likely to go wrong.** In the reference car the
Pi never connected from the glovebox on 5 GHz. Locking it to 2.4 GHz fixed it
(indoors, same spot: −63 dBm on 5 GHz vs −44 dBm on 2.4 GHz; from the glovebox
−67 dBm, enough to upload). How: [docs/PI_SETUP.md](docs/PI_SETUP.md#wi-fi-reach).

## Where the files go: choose a destination

Many people don't have a NAS. Pick whichever fits; details and the matching Pi
settings are in **[docs/DESTINATIONS.md](docs/DESTINATIONS.md)**.

| You have | Pi archives to | This tool's `[library]` |
|---|---|---|
| A NAS (Synology, QNAP, TrueNAS, Unraid) | SMB share on the NAS | `backend = "ssh"` (nothing mounted) or `"local"` on a mounted share |
| Just a computer (Mac, Linux, Windows) | a shared folder on that computer | `backend = "local"` |
| Cloud storage | Sentry USB's rclone → Google Drive / Dropbox / S3… | `backend = "local"` on the desktop-synced folder |
| No Pi, just the USB stick | — | any; run `import-local /Volumes/<stick>` now and then |

Space: raw footage is about 7–10 GB per hour of recording; a composite is
about 450 MB per 10-minute event at the default 6 Mbit/s.

## Install

Runs on macOS or Linux (on Windows, inside WSL). Needs Python 3.11+, ffmpeg and
rsync 3 (macOS: `brew install ffmpeg rsync pipx`; Debian/Ubuntu:
`sudo apt install ffmpeg rsync pipx`). No Python packages beyond the standard library.

```bash
git clone https://github.com/ikohane/tesla-dashcam-archiver.git
cd tesla-dashcam-archiver
pipx install .                  # puts `teslacam-archiver` in ~/.local/bin
teslacam-archiver init          # writes ~/.config/teslacam-archiver/config.toml
$EDITOR ~/.config/teslacam-archiver/config.toml
teslacam-archiver check         # tools, time zone, library reachable and writable
```

Every option is explained in
[`src/teslacam_archiver/config.example.toml`](src/teslacam_archiver/config.example.toml).

## Use

```bash
teslacam-archiver run                        # promote + build (45-minute budget) + index
teslacam-archiver import-local /Volumes/TESLADRIVE   # copy + verify a stick (never modifies it)
teslacam-archiver adopt /volume1/video/OldTeslaCam   # move an existing dump on the same disk/NAS
teslacam-archiver status                     # counts, recent runs, failures
teslacam-archiver index                      # rewrite index.html / library.json
```

Then open `<library>/index.html`. Run it on a schedule (every 30 minutes is
plenty; an idle run is one directory listing): launchd, systemd and cron
recipes are in **[docs/SCHEDULING.md](docs/SCHEDULING.md)**.

To plug the clips into your own catalog or photo manager, set
`[hooks] after_event`: it runs after each event is built and receives the
event (paths, time, place, reason) as JSON.

## Setting up the Pi

Step by step, with the exact wizard answers, the 2.4 GHz fix and the gotchas
(the Pi archives once per arrival home; its system disk is read-only):
**[docs/PI_SETUP.md](docs/PI_SETUP.md)**.

## Safety rules the code keeps

* **Nothing is ever deleted.** A move whose destination already exists removes
  the source only if `cmp` says the two files are byte-identical; otherwise it
  is reported as a conflict and both stay. Folders are removed with `rmdir`
  only. `import-local` never touches the source and never overwrites a file
  already in the library.
* **An event counts as done only after its composite is in the library at the
  right size.** A crash at any point means the event is simply rebuilt.
* **Events whose raw files change are rebuilt in place**; an event that fails
  three times on the same bytes is held back until its files change, so one
  broken clip can't burn CPU forever.
* **Damaged footage is normal.** A minute where every camera is corrupt is
  skipped; a camera that decodes to nothing becomes a black tile; one that
  ends early freezes on its last frame. The minute keeps its length.
* **Remote commands are bounded.** Over SSH everything runs under `timeout`
  with keep-alives, because some NAS SSH servers don't stop a command when the
  connection drops.

## Privacy

Dashcam and Sentry footage shows people, faces, licence plates and, through
`event.json`, where your car was. The gallery page carries `noindex` but has no
access control of its own: keep the library on storage only you can reach, and
think twice before putting it on a public web server or a shared cloud folder.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests run the real pipeline against a temporary folder with small
ffmpeg-generated clips, including corrupt minutes, a camera that decodes to
nothing, a truncated upload and a conflicting file. Each safety rule above has
a test that fails if the rule is removed.

## Origin

Written for a personal media archive (where the composites are also catalogued
in a larger photo/video catalog) and generalised here so the destination is
your choice. MIT licence.
