"""The background-service manager: what it renders, and what it starts."""

from __future__ import annotations

import plistlib
import sys
import time
from pathlib import Path

import pytest

from lambda_watcher import service
from lambda_watcher.config import Config
from lambda_watcher.service import (
    LaunchdManager,
    PidfileManager,
    SchtasksManager,
    SystemdManager,
    current_status,
    get_manager,
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
    monkeypatch.setattr(service, "watch_argv", lambda _p=None: ["/Apps/A & B/lw", "watch"])
    parsed = plistlib.loads(LaunchdManager(cfg)._plist(Path("/tmp/x.log")).encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/Apps/A & B/lw"


def test_the_systemd_unit_quotes_an_executable_with_a_space(cfg: Config, monkeypatch):
    """systemd unquotes ExecStart itself, so a space has to be inside quotes."""
    monkeypatch.setattr(service, "watch_argv", lambda _p=None: ["/opt/my tools/lw", "watch"])
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


# ------------------------------------------------------------------ lifecycle
@pytest.fixture
def idle_watcher(monkeypatch):
    """Stand in for `lw watch` with a process that just sits there."""
    monkeypatch.setattr(
        service, "watch_argv",
        lambda _p=None: [sys.executable, "-c", "import time; time.sleep(30)"],
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


def test_liveness_never_signals_a_windows_process(cfg: Config, monkeypatch):
    """`os.kill(pid, 0)` is TerminateProcess on Windows, not a probe."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service.os, "kill", lambda *_a: pytest.fail("signalled a live process"))
    assert service._pid_alive(4) is False


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
