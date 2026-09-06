"""Register the watcher with the platform's service manager.

``lambda-watcher watch`` is a foreground process, and a tool whose whole promise
is *"you keep downloading zips, it does the rest"* cannot also ask you to
remember to start it. This module turns the recipes that used to live only in
`docs/autostart.md` into something ``lw start`` can carry out itself: render the
unit file, hand it to launchd / systemd / Task Scheduler, and answer whether the
thing is actually running.

Everything here installs a *user* service — no sudo, no system-wide daemon. The
watcher reads one person's Downloads folder and writes one person's archive, so
it has no business running as root.

Managers are picked by platform, and the choice can fail over: Linux prefers a
systemd user unit but falls back to a plain detached process with a pidfile,
because WSL and minimal containers frequently have no systemd user session and
that is exactly where a filesystem watcher gets used.
"""

from __future__ import annotations

import getpass
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .utils import LOG

#: Identifiers registered with the OS. Changing either of these orphans the
#: service someone already installed, so they are constants, not config.
LAUNCHD_LABEL = "com.lambdawatcher"
SYSTEMD_UNIT = "lambda-watcher"
SCHEDULED_TASK = "lambda-watcher"

#: How long any service-manager subprocess gets before we give up on it.
_TIMEOUT = 30


class ServiceError(RuntimeError):
    """A service manager refused to do something, with a reason worth showing."""


@dataclass
class ServiceStatus:
    """What the platform says about our service right now."""

    manager: str                    # launchd | systemd | schtasks | pidfile | none
    installed: bool = False
    running: bool = False
    unit_path: Path | None = None
    log_path: Path | None = None
    pid: int | None = None
    detail: str = ""

    @property
    def summary(self) -> str:
        if self.running:
            return "running"
        if self.installed:
            return "installed, not running"
        return "not installed"


# --------------------------------------------------------------- command
def watch_argv(config_path: Path | None = None, windowless: bool = False) -> list[str]:
    """The command a service manager should run, as absolute argv.

    A unit file is read by a daemon with no shell, no virtualenv activation and
    frequently no PATH worth the name, so every element has to be absolute.
    ``python -m lambda_watcher`` is the dependable form — it works from a venv,
    a pipx install, a uv tool install and a plain ``pip install --user`` alike —
    but the console script reads far better in a file a human may open, so it
    wins whenever we can find it next to the running interpreter.
    """
    if windowless:
        # A launcher that runs at logon must not flash a console window, and
        # only the interpreter has a windowless build - the console script does
        # not.
        argv = [_python_for_service(), "-m", "lambda_watcher"]
        if config_path is not None:
            argv += ["--config", str(Path(config_path).expanduser().resolve())]
        return [*argv, "watch"]

    argv: list[str] = []
    script_dir = Path(sys.executable).parent
    suffixes = (".exe", "") if sys.platform.startswith("win") else ("",)
    for name in ("lambda-watcher", "lw"):
        for suffix in suffixes:
            candidate = script_dir / f"{name}{suffix}"
            if candidate.exists():
                argv = [str(candidate)]
                break
        if argv:
            break
    if not argv:
        # No console script beside the interpreter: an editable checkout run
        # through `python -m`, or a layout that puts scripts elsewhere.
        argv = [_python_for_service(), "-m", "lambda_watcher"]

    if config_path is not None:
        argv += ["--config", str(Path(config_path).expanduser().resolve())]
    argv.append("watch")
    return argv


def _python_for_service() -> str:
    """``sys.executable``, preferring the console-free build on Windows.

    A scheduled task pointed at ``python.exe`` flashes a console window at every
    logon; ``pythonw.exe`` is the same interpreter without one.
    """
    executable = Path(sys.executable)
    if sys.platform.startswith("win"):
        windowless = executable.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(executable)


def service_environment(cfg: Config) -> dict[str, str]:
    """Environment the service needs that a login session would have supplied.

    Service managers start with an environment stripped almost bare, so anything
    the user set in a shell profile is gone by the time the watcher runs. Only
    the variables that would silently point the service at a *different archive*
    than the one the user is looking at are worth baking in.
    """
    env: dict[str, str] = {}
    for name in ("LAMBDA_WATCHER_HOME", "LAMBDA_WATCHER_CONFIG", "LAMBDA_WATCHER_LOG_LEVEL"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _run(argv: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    LOG.debug("service: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise ServiceError(f"{argv[0]} is not installed") from exc
    except subprocess.SubprocessError as exc:
        raise ServiceError(f"{argv[0]} did not finish: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ServiceError(f"{' '.join(argv[:2])} failed: {detail or proc.returncode}")
    return proc


# ---------------------------------------------------------------- managers
class Manager:
    """One platform's way of running something at login and keeping it up."""

    name = "none"
    #: Launchers that run at logon with no console attached set this.
    windowless = False

    def __init__(self, cfg: Config, config_path: Path | None = None) -> None:
        self.cfg = cfg
        self.config_path = config_path

    # Every manager implements these four.
    def install(self) -> ServiceStatus: raise NotImplementedError
    def uninstall(self) -> None: raise NotImplementedError
    def stop(self) -> None: raise NotImplementedError
    def status(self) -> ServiceStatus: raise NotImplementedError

    # -- shared helpers --------------------------------------------------
    @property
    def argv(self) -> list[str]:
        return watch_argv(self.config_path, windowless=self.windowless)

    @property
    def log_path(self) -> Path:
        return self.cfg.log_dir / "service.log"

    def _prepare_logs(self) -> Path:
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        return self.log_path


class LaunchdManager(Manager):
    """macOS user agent. Loaded per-user, started at login, restarted on crash."""

    name = "launchd"

    @property
    def unit_path(self) -> Path:
        return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCHD_LABEL}.plist"

    def install(self) -> ServiceStatus:
        log = self._prepare_logs()
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text(self._plist(log), encoding="utf-8")
        # An already-loaded agent has to come out before the new definition goes
        # in; launchd will not re-read a plist for a label it already knows.
        _run(["launchctl", "unload", str(self.unit_path)])
        _run(["launchctl", "load", "-w", str(self.unit_path)], check=True)
        return self.status()

    def uninstall(self) -> None:
        if self.unit_path.exists():
            _run(["launchctl", "unload", "-w", str(self.unit_path)])
            self.unit_path.unlink()

    def stop(self) -> None:
        if self.unit_path.exists():
            # `unload` rather than `stop`: KeepAlive would restart it instantly.
            _run(["launchctl", "unload", str(self.unit_path)])

    def status(self) -> ServiceStatus:
        state = ServiceStatus(self.name, unit_path=self.unit_path, log_path=self.log_path)
        state.installed = self.unit_path.exists()
        if not state.installed:
            return state
        proc = _run(["launchctl", "list", LAUNCHD_LABEL])
        if proc.returncode != 0:
            state.detail = "registered but not loaded"
            return state
        for line in proc.stdout.splitlines():
            if '"PID"' in line:
                digits = "".join(c for c in line.split("=")[-1] if c.isdigit())
                if digits:
                    state.pid = int(digits)
        state.running = state.pid is not None
        return state

    def _plist(self, log: Path) -> str:
        args = "\n".join(f"    <string>{_xml(a)}</string>" for a in self.argv)
        env = service_environment(self.cfg)
        env_block = ""
        if env:
            pairs = "\n".join(
                f"    <key>{_xml(k)}</key>\n    <string>{_xml(v)}</string>"
                for k, v in env.items()
            )
            env_block = f"\n  <key>EnvironmentVariables</key>\n  <dict>\n{pairs}\n  </dict>\n"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
{args}
  </array>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
{env_block}
  <key>StandardOutPath</key>
  <string>{_xml(str(log))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(log))}</string>
</dict>
</plist>
"""


class SystemdManager(Manager):
    """Linux systemd *user* unit — no root, enabled for the login session."""

    name = "systemd"

    @property
    def unit_path(self) -> Path:
        return Path("~/.config/systemd/user").expanduser() / f"{SYSTEMD_UNIT}.service"

    @staticmethod
    def available() -> bool:
        """True when there is a systemd user session to talk to.

        `systemctl` being on PATH is not enough: WSL images ship it while
        running no user manager at all, and every call there fails with
        "Failed to connect to bus".
        """
        if not shutil.which("systemctl"):
            return False
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def install(self) -> ServiceStatus:
        self._prepare_logs()
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text(self._unit(), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"], check=True)
        _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], check=True)
        return self.status()

    def uninstall(self) -> None:
        if self.unit_path.exists():
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
            self.unit_path.unlink()
            _run(["systemctl", "--user", "daemon-reload"])

    def stop(self) -> None:
        _run(["systemctl", "--user", "stop", SYSTEMD_UNIT])

    def status(self) -> ServiceStatus:
        state = ServiceStatus(self.name, unit_path=self.unit_path)
        state.installed = self.unit_path.exists()
        if not state.installed:
            return state
        state.running = _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT]).stdout.strip() == "active"
        proc = _run(["systemctl", "--user", "show", SYSTEMD_UNIT, "--property=MainPID", "--value"])
        pid = proc.stdout.strip()
        if pid.isdigit() and int(pid) > 0:
            state.pid = int(pid)
        state.detail = "journalctl --user -u lambda-watcher -f"
        return state

    def _unit(self) -> str:
        exec_start = " ".join(_sh_quote(a) for a in self.argv)
        env_lines = "".join(
            f'Environment="{k}={v}"\n' for k, v in service_environment(self.cfg).items()
        )
        return f"""[Unit]
Description=Watch Downloads for AWS Lambda deployment packages
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
{env_lines}Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


class SchtasksManager(Manager):
    """Windows Task Scheduler entry, triggered at logon."""

    name = "schtasks"

    def install(self) -> ServiceStatus:
        self._prepare_logs()
        command = " ".join(_win_quote(a) for a in self.argv)
        # /RU is not decoration. With ONLOGON and no /RU the task fires for *any*
        # user who logs on, which is a machine-wide change and needs an elevated
        # prompt - "ERROR: Access is denied." on an ordinary account. Naming the
        # current user scopes the trigger to them, which they are allowed to do.
        _run([
            "schtasks", "/Create", "/TN", SCHEDULED_TASK, "/TR", command,
            "/SC", "ONLOGON", "/RU", getpass.getuser(), "/F", "/RL", "LIMITED",
        ], check=True)
        # ONLOGON only fires at the *next* logon, so start it once by hand to
        # make `lw start` mean what it says.
        _run(["schtasks", "/Run", "/TN", SCHEDULED_TASK])
        return self.status()

    def uninstall(self) -> None:
        _run(["schtasks", "/End", "/TN", SCHEDULED_TASK])
        _run(["schtasks", "/Delete", "/TN", SCHEDULED_TASK, "/F"])

    def stop(self) -> None:
        _run(["schtasks", "/End", "/TN", SCHEDULED_TASK])

    def status(self) -> ServiceStatus:
        state = ServiceStatus(self.name, log_path=self.log_path)
        proc = _run(["schtasks", "/Query", "/TN", SCHEDULED_TASK, "/FO", "LIST", "/V"])
        if proc.returncode != 0:
            return state
        state.installed = True
        for line in proc.stdout.splitlines():
            if line.lower().startswith("status:"):
                state.running = line.split(":", 1)[1].strip().lower() == "running"
        return state


class PidfileManager(Manager):
    """Last-resort manager: a detached process tracked by a pidfile.

    This is what WSL and systemd-less Linux get. It genuinely keeps the watcher
    running in the background and survives the terminal closing, but nothing
    restarts it after a reboot — callers are expected to say so.
    """

    name = "pidfile"

    @property
    def pid_path(self) -> Path:
        return self.cfg.root / "watcher.pid"

    def install(self) -> ServiceStatus:
        existing = self.status()
        if existing.running:
            return existing
        log = self._prepare_logs()
        self.cfg.root.mkdir(parents=True, exist_ok=True)
        handle = open(log, "a", encoding="utf-8")            # noqa: SIM115 - owned by the child
        try:
            proc = subprocess.Popen(
                self.argv,
                stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                env={**os.environ, **service_environment(self.cfg)},
                **_detach_kwargs(),
            )
        except OSError as exc:
            handle.close()
            raise ServiceError(f"could not start the watcher: {exc}") from exc
        finally:
            handle.close()
        self.pid_path.write_text(str(proc.pid), encoding="utf-8")
        return self.status()

    def uninstall(self) -> None:
        self.stop()
        self.pid_path.unlink(missing_ok=True)

    def stop(self) -> None:
        pid = self._recorded_pid()
        if pid is None:
            return
        _terminate(pid)
        self.pid_path.unlink(missing_ok=True)

    def status(self) -> ServiceStatus:
        state = ServiceStatus(self.name, log_path=self.log_path)
        pid = self._recorded_pid()
        if pid is None:
            return state
        state.installed = True
        state.pid = pid
        state.running = _pid_alive(pid)
        if not state.running:
            state.detail = "the recorded process is gone"
        else:
            state.detail = "started in the background; will not survive a reboot"
        return state

    def _recorded_pid(self) -> int | None:
        try:
            text = self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None


def _pid_alive(pid: int) -> bool:
    """Is that process still running?

    A process this one started and then signalled stays visible to ``kill(0)``
    as a zombie until somebody collects its exit status, and normally nobody
    does — ``lw stop`` exits immediately afterwards. Reaping first keeps a
    caller that outlives the watcher (the test suite, a `lw status` in the same
    session) from reporting a stopped watcher as running.
    """
    if sys.platform.startswith("win"):
        # `os.kill(pid, 0)` on Windows is TerminateProcess with an exit code of
        # zero, not a probe, so asking whether a process is alive would end it.
        # tasklist only looks.
        proc = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"])
        return f'"{pid}"' in proc.stdout
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass                                          # not ours; nothing to reap
    except (AttributeError, OSError):
        pass                                          # no usable waitpid here
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                   # alive, just not ours to signal
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    """Ask a process to stop, by whatever means the platform offers."""
    if sys.platform.startswith("win"):
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise ServiceError(f"could not stop pid {pid}: {exc}") from exc


def _detach_kwargs() -> dict[str, object]:
    """Popen arguments that outlive this process, per platform."""
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


class StartupFolderManager(PidfileManager):
    """Windows fallback: a launcher in this user's own Startup folder.

    An account that may not register a scheduled task can still drop a file in
    its own Startup folder — the oldest per-user autostart Windows has, and the
    one that needs no privileges whatsoever. It is strictly less capable than
    Task Scheduler (nothing restarts the watcher if it crashes), which is why it
    is the second choice rather than the first.
    """

    name = "startup-folder"
    windowless = True

    @property
    def script_path(self) -> Path:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return (
            base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "lambda-watcher.cmd"
        )

    def install(self) -> ServiceStatus:
        state = super().install()                      # running now, pid recorded
        self.script_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path.write_text(self._script(), encoding="utf-8")
        state.unit_path = self.script_path
        state.detail = "starts at logon from your Startup folder"
        return state

    def uninstall(self) -> None:
        self.script_path.unlink(missing_ok=True)
        super().uninstall()

    def status(self) -> ServiceStatus:
        state = super().status()
        if self.script_path.exists():
            state.installed = True
            state.unit_path = self.script_path
            if state.running:
                state.detail = "starts at logon from your Startup folder"
        return state

    def _script(self) -> str:
        command = " ".join(_cmd_quote(a) for a in self.argv)
        env = "".join(f'set "{k}={v}"\n' for k, v in service_environment(self.cfg).items())
        # `start ""` detaches, so the cmd window this script runs in closes at
        # once instead of waiting on a watcher that never exits.
        return f'@echo off\r\n{env}start "" {command}\r\n'


# ---------------------------------------------------------------- factory
def manager_chain(cfg: Config, config_path: Path | None = None) -> list[Manager]:
    """Every manager this machine might use, best first.

    A chain rather than a single choice because the first one can refuse at the
    moment of use, not at the moment of selection: a Windows account without the
    right to register a scheduled task only finds out when schtasks says "Access
    is denied". Whatever the platform, the last entry always works, so `lw start`
    ends with a watcher running.
    """
    if sys.platform == "darwin" and shutil.which("launchctl"):
        return [LaunchdManager(cfg, config_path)]
    if sys.platform.startswith("win"):
        return [SchtasksManager(cfg, config_path), StartupFolderManager(cfg, config_path)]
    chain: list[Manager] = []
    if sys.platform.startswith(("linux", "freebsd")) and SystemdManager.available():
        chain.append(SystemdManager(cfg, config_path))
    chain.append(PidfileManager(cfg, config_path))
    return chain


def get_manager(cfg: Config, config_path: Path | None = None) -> Manager:
    """The manager this machine would prefer to use."""
    return manager_chain(cfg, config_path)[0]


def install_service(cfg: Config, config_path: Path | None = None) -> ServiceStatus:
    """Install the best background watcher this machine will actually accept.

    Falls through the chain rather than reporting the first refusal, because a
    refusal is usually about privileges rather than about the machine being
    unable: the returned status names which manager took it, so the caller can
    say what the user ended up with.
    """
    refusals: list[str] = []
    for manager in manager_chain(cfg, config_path):
        try:
            return manager.install()
        except ServiceError as exc:
            LOG.debug("%s refused: %s", manager.name, exc)
            refusals.append(f"{manager.name}: {exc}")
    raise ServiceError("; ".join(refusals) or "no service manager available")


def stop_service(cfg: Config, config_path: Path | None = None, remove: bool = False) -> None:
    """Stop whichever manager is actually holding the watcher.

    The one that installed it is not necessarily the one this machine would pick
    today — a scheduled task registered before the account lost that right, say
    — so every candidate is asked.
    """
    for manager in manager_chain(cfg, config_path):
        try:
            if not manager.status().installed:
                continue
            manager.uninstall() if remove else manager.stop()
        except ServiceError as exc:
            LOG.debug("%s could not be stopped: %s", manager.name, exc)


def current_status(cfg: Config, config_path: Path | None = None) -> ServiceStatus:
    """Best-effort status that never raises — safe for a dashboard to call.

    A manager whose own unit is absent may still be shadowed by one installed
    under a different manager (a systemd unit written before the user moved to a
    machine without a user bus, say), so every manager that could plausibly know
    something gets asked before we report "not installed".
    """
    chain = manager_chain(cfg, config_path)
    manager = chain[0]
    candidates = list(chain)
    if sys.platform.startswith("linux") and not any(
        isinstance(c, SystemdManager) for c in candidates
    ):
        # A unit written while a user session existed outlives the session.
        candidates.append(SystemdManager(cfg, config_path))
    for candidate in candidates:
        try:
            state = candidate.status()
        except ServiceError as exc:
            LOG.debug("status via %s failed: %s", candidate.name, exc)
            continue
        if state.installed:
            return state
    return ServiceStatus(manager.name, log_path=manager.log_path)


# ------------------------------------------------------------------ quoting
def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


def _sh_quote(value: str) -> str:
    """Quote one systemd ExecStart argument.

    systemd does its own unquoting rather than handing the line to a shell, so
    this is deliberately not `shlex.quote`: only double quotes and backslashes
    need escaping, and a path with a space has to come out as one quoted token.
    """
    if value and not any(c in value for c in ' \t"\\\''):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _cmd_quote(value: str) -> str:
    """Quote one argument inside a .cmd script — plain quotes, unlike /TR."""
    return f'"{value}"' if " " in value else value


def _win_quote(value: str) -> str:
    """Quote one argument inside a schtasks /TR command string.

    NTFS forbids ``"`` in a path, so wrapping anything containing a space is the
    whole job. The escape is ``\\"`` because /TR's value is itself a quoted
    argument by the time the task scheduler reads it.
    """
    return f'\\"{value}\\"' if " " in value else value
