# Running lambda-watcher in the background

```bash
lw start     # install and start it, now and after every reboot
lw stop      # stop it (--remove unregisters it as well)
lw restart   # after editing the config
lw           # is it running?
```

That is all most people need, and `lw setup` does it as part of first-time
setup. `lw start` writes the unit file for your platform, hands it to the
service manager and reports back — a launchd agent on macOS, a systemd user
service on Linux, a scheduled task on Windows.

Everything is registered as a **user** service. Nothing here needs sudo: the
watcher reads one person's downloads folder and writes one person's archive.

The rest of this page is what `lw start` does, for when you want to change it,
audit it, or do it yourself.

## When there is no service manager to use

On Linux without a systemd user session — WSL images and slim containers
frequently have none — `lw start` falls back to a plain detached process
recorded in `~/.lambda-watcher/watcher.pid`, and tells you so. It runs in the
background and survives the terminal closing, but nothing brings it back after
a reboot. Either run `lw start` again when you next log in (a shell profile is
a fine place for it), or give yourself a user session that persists:

```bash
sudo loginctl enable-linger $USER
```

---

## Doing it by hand

In every recipe below, replace `/path/to/.venv/bin/lambda-watcher` with the
output of `which lambda-watcher` (or the absolute path inside your virtualenv),
and `YOU` with your username.

---

## macOS (launchd)

Save as `~/Library/LaunchAgents/com.lambdawatcher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.lambdawatcher</string>

  <key>ProgramArguments</key>
  <array>
    <string>/path/to/.venv/bin/lambda-watcher</string>
    <string>watch</string>
  </array>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/YOU/.lambda-watcher/logs/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/.lambda-watcher/logs/stderr.log</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/com.lambdawatcher.plist   # start
launchctl unload ~/Library/LaunchAgents/com.lambdawatcher.plist    # stop
```

macOS may ask you to grant the process access to your Downloads folder the
first time it runs — approve it, or the watcher will see an empty directory.

---

## Linux (systemd user service)

Save as `~/.config/systemd/user/lambda-watcher.service`:

```ini
[Unit]
Description=Watch Downloads for AWS Lambda deployment packages
After=default.target

[Service]
Type=simple
ExecStart=/path/to/.venv/bin/lambda-watcher watch
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now lambda-watcher
systemctl --user status lambda-watcher
journalctl --user -u lambda-watcher -f
```

To keep it running when you are not logged in: `sudo loginctl enable-linger $USER`.

---

## Windows (Task Scheduler)

Run in PowerShell, adjusting the paths:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\lambda-watcher.exe" `
                                   -Argument "watch"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries `
                                         -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "lambda-watcher" `
                       -Action $action -Trigger $trigger -Settings $settings `
                       -Description "Archive Lambda deployment zips from Downloads"
```

To run without a console window appearing, point the action at
`pythonw.exe` with `-m lambda_watcher watch` instead.

---

## WSL, network drives and VM shares

Native filesystem events do not cross these boundaries reliably. Turn on
polling:

```yaml
watch:
  force_polling: true
  polling_interval: 2.0
```

If your browser runs on Windows and you want to watch from WSL, point the
watcher at the Windows folder directly:

```yaml
watch:
  dirs: ["/mnt/c/Users/YOU/Downloads"]
  force_polling: true
```

---

## Checking it is working

```bash
lw           # running? how many versions? what arrived last?
lw doctor    # config, watch folders, store, git, disk
lw log       # what it has seen recently, including skipped files
tail -f ~/.lambda-watcher/logs/watcher.log
tail -f ~/.lambda-watcher/logs/service.log   # what the service itself printed
```

If `doctor` reports a watch directory as `MISSING`, fix `watch.dirs` in the
config — that is the single most common reason nothing is being archived.
Run `lw restart` after any config change: the running service is still holding
the old one.
