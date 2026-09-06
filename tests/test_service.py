"""The background-service manager: what it renders, and what it starts."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lambda_watcher import service
from lambda_watcher.config import Config
from lambda_watcher.service import (
    ServiceError,
    ServiceStatus,
    LaunchdManager,
    PidfileManager,
    SchtasksManager,
    StartupFolderManager,
    SystemdManager,
    current_status,
    get_manager,
    install_service,
    manager_chain,
    service_environment,
    watch_argv,
)


def test_the_service_command_is_absolute_and_ends_in_watch():
    argv = watch_argv()
    assert Path(argv[0]).is_absolute(), "a unit file is read with no PATH to speak of"
    assert argv[-1] == "watch"


def test_a_custom_config_is_carried_into_the_service(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("watch: {}\n", encoding="utf-8")
    argv = watch_argv(config)
    assert "--config" in argv
    assert str(config.resolve()) in argv
    # `--config` is a callback option, so it has to precede the subcommand.
    assert argv.index("--config") < argv.index("watch")


def test_the_archive_location_travels_with_the_service(cfg: Config, monkeypatch):
    """A login-started service inherits none of the user's shell environment."""
    monkeypatch.setenv("LAMBDA_WATCHER_HOME", "/somewhere/else")
    assert service_environment(cfg)["LAMBDA_WATCHER_HOME"] == "/somewhere/else"

    monkeypatch.delenv("LAMBDA_WATCHER_HOME")
    assert "LAMBDA_WATCHER_HOME" not in service_environment(cfg)


# ------------------------------------------------------------------ rendering
def test_the_launch_agent_is_a_readable_plist(cfg: Config):
    manager = LaunchdManager(cfg)
    parsed = plistlib.loads(manager._plist(cfg.log_dir / "service.log").encode("utf-8"))
    assert parsed["Label"] == service.LAUNCHD_LABEL
    assert parsed["ProgramArguments"][-1] == "watch"
    assert parsed["RunAtLoad"] is True and parsed["KeepAlive"] is True
    assert parsed["StandardOutPath"].endswith("service.log")


def test_the_launch_agent_escapes_a_path_that_needs_it(cfg: Config, monkeypatch):
    monkeypatch.setattr(service, "watch_argv", lambda *_a, **_k: ["/Apps/A & B/lw", "watch"])
    parsed = plistlib.loads(LaunchdManager(cfg)._plist(Path("/tmp/x.log")).encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/Apps/A & B/lw"


def test_the_systemd_unit_quotes_an_executable_with_a_space(cfg: Config, monkeypatch):
    """systemd unquotes ExecStart itself, so a space has to be inside quotes."""
    monkeypatch.setattr(service, "watch_argv", lambda *_a, **_k: ["/opt/my tools/lw", "watch"])
    unit = SystemdManager(cfg)._unit()
    assert 'ExecStart="/opt/my tools/lw" watch' in unit
    assert "WantedBy=default.target" in unit


def test_the_systemd_unit_carries_the_archive_location(cfg: Config, monkeypatch):
    monkeypatch.setenv("LAMBDA_WATCHER_HOME", "/var/archive")
    assert 'Environment="LAMBDA_WATCHER_HOME=/var/archive"' in SystemdManager(cfg)._unit()


# ------------------------------------------------------------------ platform
@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", LaunchdManager), ("win32", SchtasksManager)],
)
def test_each_platform_gets_its_own_manager(cfg: Config, monkeypatch, platform, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/usr/bin/launchctl")
    assert isinstance(get_manager(cfg), expected)


def test_linux_without_a_systemd_user_session_still_gets_a_watcher(cfg: Config, monkeypatch):
    """WSL and slim containers have no user bus; they must not be left with nothing."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(SystemdManager, "available", staticmethod(lambda: False))
    assert isinstance(get_manager(cfg), PidfileManager)


def test_windows_gets_a_fallback_when_the_task_scheduler_refuses(cfg: Config, monkeypatch):
    """An ordinary account may not register a scheduled task; it still gets a watcher."""
    monkeypatch.setattr(sys, "platform", "win32")
    chain = [m.name for m in manager_chain(cfg)]
    assert chain == ["schtasks", "startup-folder"]


def test_the_scheduled_task_is_scoped_to_this_user(cfg: Config, monkeypatch):
    """ONLOGON with no /RU fires for *any* user, which needs elevation to register."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service.getpass, "getuser", lambda: "someone")
    calls = _fake_run(monkeypatch)
    SchtasksManager(cfg).install()
    create = next(c for c in calls if "/Create" in c)
    assert create[create.index("/RU") + 1] == "someone"


def test_a_refused_manager_falls_through_to_one_that_works(cfg: Config, monkeypatch):
    monkeypatch.setattr(
        SchtasksManager, "install",
        lambda _self: (_ for _ in ()).throw(ServiceError("ERROR: Access is denied.")),
    )
    monkeypatch.setattr(
        StartupFolderManager, "install",
        lambda self: ServiceStatus(self.name, installed=True, running=True),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    assert install_service(cfg).manager == "startup-folder"


def test_when_everything_refuses_the_reasons_are_all_reported(cfg: Config, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    for manager in (SchtasksManager, StartupFolderManager):
        monkeypatch.setattr(
            manager, "install",
            lambda _self, name=manager.name: (_ for _ in ()).throw(ServiceError(f"{name} said no")),
        )
    with pytest.raises(ServiceError) as caught:
        install_service(cfg)
    assert "schtasks said no" in str(caught.value)
    assert "startup-folder said no" in str(caught.value)


def test_the_startup_launcher_detaches_and_shows_no_console(cfg: Config, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:\\Users\\someone\\AppData\\Roaming")
    manager = StartupFolderManager(cfg)
    assert manager.script_path.name == "lambda-watcher.cmd"
    assert manager.script_path.parent.name == "Startup"

    script = manager._script()
    assert script.startswith("@echo off")
    # `start ""` lets the launching cmd window close instead of waiting forever.
    assert 'start ""' in script
    # -m, not the console script: only the interpreter has a windowless build.
    assert "-m lambda_watcher" in script
    assert script.endswith("\r\n"), "a .cmd file wants CRLF"


def test_the_windowless_command_avoids_the_console_script():
    plain = watch_argv()
    quiet = watch_argv(windowless=True)
    assert plain[-1] == quiet[-1] == "watch"
    assert quiet[1:3] == ["-m", "lambda_watcher"]


# ------------------------------------------------------------------ lifecycle
@pytest.fixture
def idle_watcher(monkeypatch):
    """Stand in for `lw watch` with a process that just sits there."""
    monkeypatch.setattr(
        service, "watch_argv",
        lambda *_a, **_k: [sys.executable, "-c", "import time; time.sleep(30)"],
    )


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix signals")
def test_the_fallback_manager_starts_and_stops_a_real_process(cfg: Config, idle_watcher):
    manager = PidfileManager(cfg)
    assert manager.status().running is False

    started = manager.install()
    assert started.running and started.pid
    assert manager.pid_path.exists()
    # It is honest about the one thing it cannot do.
    assert "reboot" in started.detail

    manager.uninstall()
    assert not manager.pid_path.exists()
    for _ in range(50):                                # SIGTERM is not instant
        if not service._pid_alive(started.pid):
            break
        time.sleep(0.1)
    assert not service._pid_alive(started.pid)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="posix signals")
def test_starting_twice_does_not_start_a_second_watcher(cfg: Config, idle_watcher):
    manager = PidfileManager(cfg)
    first = manager.install()
    try:
        assert manager.install().pid == first.pid
    finally:
        manager.uninstall()


def _fake_run(monkeypatch, stdout: str = "", returncode: int = 0) -> list[list[str]]:
    """Record what would have been run, and answer with canned output."""
    calls: list[list[str]] = []

    def run(argv, check=False):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(service, "_run", run)
    return calls


@pytest.mark.parametrize(
    ("listing", "alive"),
    [('"python.exe","4","Console","1","9,000 K"\n', True), ("INFO: No tasks are running.\n", False)],
)
def test_windows_liveness_asks_tasklist_instead_of_signalling(monkeypatch, listing, alive):
    """`os.kill(pid, 0)` is TerminateProcess on Windows — asking would end it."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service.os, "kill", lambda *_a: pytest.fail("signalled a live process"))
    calls = _fake_run(monkeypatch, stdout=listing)
    assert service._pid_alive(4) is alive
    assert calls[0][0] == "tasklist"


def test_a_stale_pidfile_reads_as_not_running(cfg: Config):
    manager = PidfileManager(cfg)
    cfg.root.mkdir(parents=True, exist_ok=True)
    manager.pid_path.write_text("999999", encoding="utf-8")
    state = manager.status()
    assert state.installed and not state.running
    assert "gone" in state.detail


def test_status_on_a_machine_with_nothing_installed_is_not_an_error(cfg: Config, monkeypatch):
    """The dashboard calls this on every bare `lw`; it may never raise."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(SystemdManager, "available", staticmethod(lambda: False))
    state = current_status(cfg)
    assert not state.installed and not state.running
    assert state.summary == "not installed"
