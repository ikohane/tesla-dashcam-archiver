# Setting up the Pi (Sentry USB)

[Sentry USB](https://github.com/Sentry-Six/Sentry-USB-Rusty) turns a Raspberry
Pi into a USB drive for the car that copies finished clips to your archive over
Wi-Fi. New installs use the Rust rewrite, "Sentry USB Rusty"; there is no
prebuilt image, you flash stock Raspberry Pi OS and run its installer. Allow an
hour the first time.

## 0. Before you start

* **Save what is on the car's current USB stick.** Plug it into your computer
  and run `teslacam-archiver import-local /Volumes/<stick>` (copies and
  SHA-256-checks every file, never modifies the stick).
* **Encryption:** on AMD-computer cars with software 2026.20+, turn off
  *Encrypt Dashcam Recordings* under Controls › Safety. See [ENCRYPTION.md](ENCRYPTION.md).
* **Archive destination ready:** a share and an account for the Pi. See
  [DESTINATIONS.md](DESTINATIONS.md). On a Synology: Control Panel › User ›
  Create, Read/Write on the one shared folder only, and **Deny** every
  application (the Pi needs only SMB, which follows the folder permission).

## 1. Flash the card (on your computer)

The Pi stays unplugged; only the microSD card goes into the computer.

1. Raspberry Pi Imager → Device **Raspberry Pi 4** → OS **Raspberry Pi OS
   (other) › Raspberry Pi OS Lite (64-bit)** → Storage: the card (check the size).
2. Edit Settings: username + password, your Wi-Fi (country code), Services ›
   **SSH with password**. **Leave the hostname blank**; the installer sets it.
3. Leave *Raspberry Pi Connect* off: it needs a Raspberry Pi account and isn't
   used.

## 2. Install (on your desk)

Put the card in the Pi and power it from any USB-C charger. After a minute:

```bash
ssh YOURUSER@raspberrypi.local
```

```bash
sudo apt update && sudo apt upgrade -y
```

```bash
sudo -i
```

```bash
curl -fsSL https://usb.sentry-six.com | bash
```

Two to five minutes. The SSH session may drop near the end when the Pi renames
itself `sentryusb`; that's expected.

## 3. The setup wizard (`http://sentryusb.local`)

| Step | Answer | Why |
|---|---|---|
| Privacy | **No thanks** | "Yes, count me" is preselected |
| Network | hostname `sentryusb`; Wi-Fi Access Point **off** | type the hostname: the grey text is a placeholder |
| Storage | Dashcam **50 GB** (shown as `50G` on Review), ExFAT on, no music/lightshow/boombox | the rest of the card holds snapshots used while archiving; a value without "G" is ambiguous |
| Archive | **CIFS / SMB**: server `nas.local` (or its IP), share `<share>/<path to library>/incoming`, user/password of the Pi's account, CIFS version blank | then **Test Connection** |
| What to archive | **Saved** and **Sentry** on, **Recent** off | Recent is the rolling buffer of all driving (7–10 GB/hour); anything worth keeping arrives as Saved or Sentry |
| Config backup | Archive Server | lands in `incoming/backups/`, which this tool ignores |
| Keep Awake | **None** to start | parking + scheduled charging keep USB power on long enough; BLE later if clips back up. TeslaFi/Tessie need a Tesla login renewed every few months |
| 12 V outlet | off | only for Pis on the 12 V socket |
| Security | set a web username/password | |
| Advanced | your time zone | |
| Review | check every row, then **Apply & Run Setup** | going Back can silently reset a field |

The Pi reboots, possibly more than once. Its dashboard then says *Waiting for
car*. Install its offered Wi-Fi firmware update; dismiss SentryCloud unless
you want it.

## 4. Into the car

```bash
ssh YOURUSER@sentryusb.local 'sudo poweroff'
```

Glovebox port → 1 ft cable → the Pi's **USB-C** port. Within a minute
**Controls › Safety** shows the drive (for example *1.4 GB / 50.0 GB*) instead
of *Insert USB drive to enable Dashcam*. Red LED = power, flickering green =
the car writing.

## Wi-Fi reach

If the car sees the Pi but the Pi never shows up on your network from the
garage, the Pi's Wi-Fi can't reach. Check with a phone at the open glovebox,
then lock the Pi to **2.4 GHz**, which carries much farther. (Measured: −63 dBm
on 5 GHz vs −44 dBm on 2.4 GHz indoors; on 5 GHz it never connected from the
glovebox, on 2.4 GHz it did at −67 dBm.)

Do this with the Pi on your desk. Two Sentry USB quirks:

* the system disk is **read-only** (so power cuts can't corrupt it), and
* NetworkManager runs sandboxed, so `nmcli connection modify` fails with
  *Read-only file system* even after you make the disk writable. Edit the
  netplan file instead.

```bash
ssh YOURUSER@sentryusb.local
```

```bash
sudo mount -o remount,rw /
```

Find the file for your network and keep a copy:

```bash
sudo grep -l 'YOUR-SSID' /etc/netplan/*.yaml
```

```bash
sudo cp -p /etc/netplan/90-NM-XXXX.yaml /root/wifi-before-2.4GHz.yaml
```

Edit it (`sudo nano /etc/netplan/90-NM-XXXX.yaml`) and add one line under your
network's name, indented like `auth:`:

```yaml
      access-points:
        "YOUR-SSID":
          band: "2.4GHz"
          auth:
            key-management: "psk"
```

Apply and check. The session may drop for a few seconds while it reconnects:

```bash
sudo netplan generate && sudo nmcli connection reload && sudo nmcli connection up "netplan-wlan0-YOUR-SSID"
```

```bash
iw dev wlan0 link
```

`freq: 24xx` means 2.4 GHz. A reboot makes the disk read-only again and keeps
the setting. If it doesn't reconnect, restore the copy from the SD card or
re-flash; doing this at your desk makes that painless.

## How archiving behaves

* **The Pi archives once each time the car arrives home**, as soon as the share
  is reachable. Afterwards its log says *Waiting for archive to be
  unreachable*: a clip you save while parked at home goes up on the **next**
  arrival. Its log is `/mutable/archiveloop.log`.
* To archive now: `sudo /root/bin/force_sync.sh`. It runs
  `pkill -USR1 -f archiveloop`, which signals **every** process whose command
  line contains "archiveloop", including your own (`tail -f
  /mutable/archiveloop.log` in the same command will be killed). Run it on its
  own.
* The car keeps recording whether or not the Pi can reach Wi-Fi; clips wait on
  the card until the next successful archive.
* `teslacam-archiver` moves an event out of `incoming/` once nothing in it has
  changed for 10 minutes, so a newly archived event shows up in the gallery
  within about 40 minutes with a 30-minute schedule.

## Optional: key-based admin access

So scripts (or you) can administer the Pi without typing a password, add a
public key for root while the disk is writable:

```bash
sudo sh -c 'mount -o remount,rw / && mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "ssh-ed25519 AAAA… you@computer" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys; mount -o remount,ro / || true'
```

Revoke by deleting that line.
