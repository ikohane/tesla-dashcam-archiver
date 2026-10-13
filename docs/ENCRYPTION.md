# Dashcam clip encryption (Tesla software 2026.20+)

**Short version:** if your car has the AMD Ryzen infotainment computer and is
on software 2026.20 or later, it may write new dashcam and Sentry clips in an
encrypted form that only your Tesla account can unlock. Neither Sentry USB nor
this tool can read those files. Turn the feature off in the car if you want an
automatic archive; otherwise you must decrypt clips through Tesla's website.

## What changed

Tesla software **2026.20** (rolled out from June 2026) added *Dashcam Clip
Encryption*: new Sentry and Dashcam clips are stored on the USB drive as
encrypted containers instead of plain MP4 files, with a key tied to the Tesla
account the car is linked to. The stated purpose is privacy: footage on a
stolen USB drive is useless without the owner's account.

* Where the feature exists it is **on by default** and **can be turned off**:
  **Controls › Safety**, in the dashcam / USB storage options ("Encrypt Dashcam
  Recordings"). The setting may only appear while a USB drive is plugged in.
* Encrypted clips appear under `TeslaCam/EncryptedClips/` rather than in
  `SavedClips` / `SentryClips`.
* Decryption is online only: the in-car viewer, the Tesla app, or
  **https://dashcam.tesla.com**, where you sign in, select the encrypted files,
  and the browser fetches the key and decrypts them locally ("Download All"
  saves a ZIP). There is no documented offline or command-line decryption.
* Clips recorded **before** the update, or with the feature off, stay plain MP4.

## Which cars

It depends on the car's infotainment computer, not on the model name:

| Infotainment computer | Typical cars | Encryption |
|---|---|---|
| **AMD Ryzen** | Model 3 and Model Y built since about late 2021; refreshed Model S/X (2021+); Cybertruck | **yes**, on 2026.20+ where rolled out |
| Intel Atom (MCU2) | earlier Model 3/Y; earlier Model S/X with the MCU2 upgrade | no |
| MCU1 | older Model S/X | no |

Reports also say mainland-China cars are excluded and that the rollout is by
region, so two identical cars can differ for a while.

**How to tell which computer you have:** on the car's screen open
**Controls › Software** and look for *Additional Vehicle Information*; it lists
the infotainment processor. If it says AMD Ryzen, plan for encryption.

**Real-world note:** the reference car for this project (a US 2022 Model 3 AWD,
AMD) had no encryption toggle and was still writing plain MP4s in October
2026, months after 2026.20 shipped. Check your own car; don't assume.

## What this means for an automatic archive

| Situation | What to do |
|---|---|
| Intel or MCU1 car | nothing |
| AMD car, setting present | turn **off** "Encrypt Dashcam Recordings" before installing the Pi |
| AMD car, no setting yet | nothing for now; this tool will warn the day an `EncryptedClips` folder appears |
| You want to keep encryption on | no unattended archive is possible today; decrypt batches at dashcam.tesla.com and `teslacam-archiver import-local` the decrypted folder |

Sentry USB's own documentation says the same: "Encrypted files cannot be read
by Sentry USB. To continue using Sentry USB, please disable the encryption."

## How this tool behaves

* `run` never moves anything under `EncryptedClips/`. It counts the files,
  logs a warning pointing here, and records the count in the run log.
* `import-local` refuses a folder that contains encrypted clips, so you don't
  end up with a half-imported stick; decrypt first.

## The privacy trade-off

Turning encryption off means the footage on the Pi's SD card is readable by
anyone who takes the Pi. In this setup that exposure is limited: the Pi copies
events to your library within minutes of arriving home and keeps only a
rolling buffer. The library itself then needs ordinary protection: storage
only you can reach, and care with shared cloud folders.

## Sources

* Not a Tesla App, "Tesla launches new web-based dashcam viewer" (June 2026):
  https://www.notateslaapp.com/news/4236/tesla-launches-new-web-based-dashcam-viewer
* Tesla Oracle, using dashcam.tesla.com to decrypt clips:
  https://www.teslaoracle.com/2026/06/06/heres-how-to-use-teslas-online-tool-to-unlock-encrypted-dashcam-video-clips-update-2026-20-or-later/
* ClipSalvage, encrypted clip format and affected hardware:
  https://www.clipsalvage.com/articles/batch-decrypt-tesla-clips.html
* Sentry USB (Rusty), README and wiki on disabling encryption:
  https://github.com/Sentry-Six/Sentry-USB-Rusty
