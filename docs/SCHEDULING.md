# Running it on a schedule

Every 30 minutes is plenty. An idle run is one directory listing; a run with
work builds for at most `--max-minutes` (default 45) and leaves the rest for
the next run. A lock prevents two runs from overlapping, so a slow run and the
next tick never collide.

Schedulers start programs with a minimal `PATH`. ffmpeg (and Homebrew's rsync on
a Mac) must be on it, or the run stops with a clear error.

## macOS: launchd

Copy [`scheduling/com.example.teslacam-archiver.plist`](../scheduling/com.example.teslacam-archiver.plist)
to `~/Library/LaunchAgents/`, adjust the paths, then:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.teslacam-archiver.plist
```

Run it now: `launchctl kickstart gui/$(id -u)/com.example.teslacam-archiver`.
Logs go to `~/Library/Logs/teslacam-archiver.log`. `caffeinate -i` keeps the
Mac from idling to sleep in the middle of a build.

## Linux: systemd user timer

Copy [`scheduling/teslacam-archiver.service`](../scheduling/teslacam-archiver.service)
and [`scheduling/teslacam-archiver.timer`](../scheduling/teslacam-archiver.timer)
to `~/.config/systemd/user/`, then:

```bash
systemctl --user daemon-reload && systemctl --user enable --now teslacam-archiver.timer
```

Logs: `journalctl --user -u teslacam-archiver`. On a headless server, run
`loginctl enable-linger $USER` so the timer runs while you're logged out.

## cron

```cron
*/30 * * * * PATH=/usr/local/bin:/usr/bin:/bin $HOME/.local/bin/teslacam-archiver run >> $HOME/teslacam-archiver.log 2>&1
```

## Checking on it

```bash
teslacam-archiver status
```

shows counts, the last runs (with encrypted-clip warnings), and any events that
failed. An event that fails three times on the same files is held back until
its files change; fix or remove the files, and it is retried automatically.
