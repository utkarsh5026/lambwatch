# Running lambda-watcher in the background

`lambda-watcher watch` is a long-running foreground process. To have it start
with your machine and stay out of the way, register it with your platform's
service manager.

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
lambda-watcher doctor    # config, watch folders, store, git, disk
lambda-watcher log       # what it has seen recently, including skipped files
tail -f ~/.lambda-watcher/logs/watcher.log
```

If `doctor` reports a watch directory as `MISSING`, fix `watch.dirs` in the
config — that is the single most common reason nothing is being archived.
