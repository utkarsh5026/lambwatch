"""Best-effort desktop notifications, with no extra dependencies."""

from __future__ import annotations

import shutil
import subprocess
import sys

from .utils import LOG


def notify(title: str, message: str, enabled: bool = True) -> bool:
    """Show a desktop notification. Never raises; returns True if it fired."""
    if not enabled:
        return False
    try:
        if sys.platform == "darwin":
            script = (
                f'display notification {_applescript_quote(message)} '
                f'with title {_applescript_quote(title)}'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return True
        if sys.platform.startswith("win"):
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                " ContentType = WindowsRuntime] > $null; "
                "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(2); "
                f"$t.GetElementsByTagName('text')[0]"
                f".AppendChild($t.CreateTextNode({_ps_quote(title)})) > $null; "
                f"$t.GetElementsByTagName('text')[1]"
                f".AppendChild($t.CreateTextNode({_ps_quote(message)})) > $null; "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('lambda-watcher')"
                ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                timeout=15,
            )
            return True
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "-a", "lambda-watcher", title, message],
                capture_output=True,
                timeout=10,
            )
            return True
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("notification failed: %s", exc)
    return False


def _applescript_quote(value: str) -> str:
    """Quote a string for embedding in AppleScript, escaping backslashes then quotes.

    The notification text is a function name that came out of a downloaded
    filename, so it is not trusted input — quoting it is what keeps a stray
    quote character from turning into script.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps_quote(value: str) -> str:
    """Quote a string for a PowerShell single-quoted literal, doubling any quote.

    PowerShell single-quoted strings interpolate nothing, so doubling ``'`` is
    the only escape needed. Same reasoning as :func:`_applescript_quote`.
    """
    return "'" + value.replace("'", "''") + "'"
