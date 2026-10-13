# Choosing where the archive lives

Two things have to agree:

1. **where the Pi drops clips**: Sentry USB's archive setting, and
2. **where this tool finds them**: `[library]` in the config. The Pi's target
   must be `<library>/incoming`.

The tool runs on macOS or Linux (on Windows, run it in WSL). It needs to read
and write the library, either as a folder (`backend = "local"`) or over SSH
(`backend = "ssh"`).

## A. A NAS (Synology, QNAP, TrueNAS, Unraid…)

**Pi:** CIFS/SMB to the NAS, share path `<share>/TeslaDashcam/incoming`, with a
dedicated NAS account that can write only that share. (NFS and rsync also work.)

**Tool, option 1, SSH (recommended):** nothing to mount; moves and hashes
happen on the NAS, video copies use rsync.

```toml
[library]
backend = "ssh"
host = "nas.local"
user = "me"
key = "~/.ssh/id_ed25519"
path = "/volume1/video/TeslaDashcam"
```

Set up key login first (`ssh-copy-id me@nas.local`; on Synology enable SSH in
Control Panel › Terminal & SNMP and allow home folders). The NAS needs GNU
`find`, `cmp`, `mv`, `stat`, `sha256sum` and `timeout`, as DSM and most Linux
NAS systems have. Away from home, add `jump = "me@a-machine-at-home"`.

**Tool, option 2, mounted share:** mount the share on the computer
(Finder › Go › Connect to Server › `smb://nas.local/video`, or `/etc/fstab` on
Linux) and use `backend = "local"` with `path = "/Volumes/video/TeslaDashcam"`.
Simpler, slower for big moves, and the share must be mounted when the
schedule runs.

## B. No NAS: a folder on your computer

**Make a shared folder the Pi can write:**

* **macOS:** System Settings › General › Sharing › File Sharing › add the
  folder (for example `~/TeslaDashcam`); Options › enable SMB for a user. A
  dedicated "Sharing Only" user in Users & Groups is cleaner than your own login.
  The share name is the folder name.
* **Linux:** Samba (`[TeslaDashcam] path = /srv/TeslaDashcam`, writable, valid
  users = one account).
* **Windows:** share the folder to one local account. Run this tool in WSL with
  the folder at `/mnt/c/...`.

**Pi:** CIFS/SMB, server = the computer's name or IP, share
`TeslaDashcam/incoming`.

**Tool:**

```toml
[library]
backend = "local"
path = "~/TeslaDashcam"
```

**Catch:** the computer must be awake when the car gets home, or the Pi finds
no share and tries again on the next arrival. On a Mac enable *Wake for network
access*; a desktop that sleeps rarely works better than a laptop that travels.

## C. Cloud storage (Google Drive, Dropbox, OneDrive, S3…)

**Pi:** Sentry USB's **rclone** archive method to a folder in your cloud, e.g.
`gdrive:TeslaDashcam/incoming`.

**Tool:** run it on a computer that syncs that cloud folder (the Google Drive,
Dropbox or OneDrive desktop app), with `backend = "local"`:

```toml
[library]
backend = "local"
path = "~/Library/CloudStorage/GoogleDrive-you@example.com/My Drive/TeslaDashcam"
```

**Costs and cautions:** raw footage is 7–10 GB per recorded hour and Sentry
events add up fast; check your plan's storage. Make the folder **available
offline / always keep on this device**, otherwise the sync client hands the
tool placeholders instead of files. Composites and the gallery sync back up
too. Footage is sensitive (see the README's Privacy section): don't put the
library in a folder shared with others.

## D. No Pi: just the car's USB stick

Leave the stick in the car and every few weeks plug it into the computer:

```bash
teslacam-archiver import-local /Volumes/TESLADRIVE
teslacam-archiver run
```

`import-local` copies everything into the library with a SHA-256 check of every
file and writes an audit list; it never modifies the stick. Once you're happy,
format the stick in the car (Controls › Safety › Format USB Drive) and put it
back.

## Moving existing footage in

* **On the same disk or NAS as the library:** `teslacam-archiver adopt
  /absolute/path/TeslaCam` moves it in place: no copy, no extra space.
* **On another drive or computer:** `teslacam-archiver import-local <folder>`.

Folders you renamed by hand (`2020-06-14_15-32-11_Graduation`) keep their title,
which also becomes the event's caption.

## Space planning

| Item | Size |
|---|---|
| Raw footage | ~7–10 GB per recorded hour (four cameras) |
| One 10-minute Sentry event, raw | ~1–1.2 GB |
| Its composite (default 6 Mbit/s) | ~450 MB |
| Its poster | ~30 KB |

The raw files stay in `events/` alongside the composite. Delete raw events you
don't need yourself; the tool never does.
