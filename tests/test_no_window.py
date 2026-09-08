"""Nothing the background watcher spawns may flash a console window on Windows.

The watcher runs with no console of its own, so Windows hands a brand new one to
every console program it starts unless ``CREATE_NO_WINDOW`` says otherwise. The
symptom is only visible on Windows and only while a zip is being archived, so
these tests fake the flag rather than the platform: that keeps the wiring pinned
on the Linux leg, where it would otherwise go unchecked until a user complained.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lambda_watcher import gitmirror, notify as notify_mod, service
from lambda_watcher.utils import no_window_kwargs

WINDOWS_FLAG = 0x08000000


@pytest.fixture
def windows_flag(monkeypatch):
    """Give this interpreter the flag that only the Windows build defines."""
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", WINDOWS_FLAG, raising=False)
    return WINDOWS_FLAG


@pytest.fixture
def recorded(monkeypatch):
    """Record the keywords of every ``subprocess.run`` and start nothing.

    Every module here does a plain ``import subprocess``, so patching the one
    module object covers all of them.
    """
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        """Stand in for a command that succeeded silently."""
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_the_flag_is_only_asked_for_where_it_exists(monkeypatch):
    monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
    assert no_window_kwargs() == {}


def test_the_flag_is_passed_through_when_the_platform_has_it(windows_flag):
    assert no_window_kwargs() == {"creationflags": windows_flag}


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only flag")
def test_real_windows_suppresses_the_window():
    assert no_window_kwargs()["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_the_git_mirror_never_opens_a_console(tmp_path, windows_flag, recorded):
    # Archiving one version runs five of these back to back.
    gitmirror._run(tmp_path, "status", "--porcelain", check=False)
    assert recorded[0]["creationflags"] == windows_flag


def test_the_toast_notification_never_opens_a_console(
    monkeypatch, windows_flag, recorded
):
    monkeypatch.setattr(sys, "platform", "win32")
    assert notify_mod.notify("lambda-watcher", "order-processor v3")
    assert recorded[0]["creationflags"] == windows_flag


def test_the_service_manager_never_opens_a_console(windows_flag, recorded):
    service._run(["schtasks", "/Query", "/TN", "lambda-watcher"])
    assert recorded[0]["creationflags"] == windows_flag


def test_the_interactive_git_passthrough_keeps_its_terminal(
    tmp_path, windows_flag, recorded
):
    # `lw git my-fn log` is watched by the person who typed it: suppressing the
    # window would take the pager and the colour with it.
    gitmirror.passthrough(tmp_path, ["log", "--oneline"])
    assert "creationflags" not in recorded[0]
