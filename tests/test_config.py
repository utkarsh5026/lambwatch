from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lambda_watcher import config as config_module
from lambda_watcher.config import (
    default_download_dirs,
    needs_polling,
    on_wsl,
    windows_downloads_on_wsl,
)


@pytest.fixture
def fake_wsl(tmp_path: Path, monkeypatch):
    """A WSL guest whose Windows drives are a directory the test owns."""
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(config_module, "_WSL_MOUNT_ROOT", mount)
    return mount


@pytest.fixture
def not_wsl(tmp_path: Path, monkeypatch):
    """A plain Linux box: no distro name, and a kernel that says nothing."""
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    release = tmp_path / "osrelease"
    release.write_text("6.8.0-generic\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_WSL_OSRELEASE", release)
    return release


def _profile(mount: Path, name: str, drive: str = "c", downloads: bool = True) -> Path:
    """Create ``<mount>/<drive>/Users/<name>``, with a Downloads folder by default."""
    home = mount / drive / "Users" / name
    home.mkdir(parents=True)
    if downloads:
        (home / "Downloads").mkdir()
    return home


def test_a_wsl_guest_is_recognised_from_the_distro_name(fake_wsl):
    assert on_wsl()


def test_a_wsl_guest_is_recognised_from_the_kernel_alone(tmp_path: Path, monkeypatch):
    # A watcher started by a Windows service has no WSL_DISTRO_NAME in its
    # environment; the kernel release is the marker that survives that.
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    release = tmp_path / "osrelease"
    release.write_text("5.15.167.4-microsoft-standard-WSL2\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_WSL_OSRELEASE", release)
    assert on_wsl()


def test_a_plain_linux_box_is_not_mistaken_for_wsl(not_wsl):
    assert not on_wsl()


def test_the_one_windows_profile_is_where_the_downloads_land(fake_wsl):
    _profile(fake_wsl, "Sam")
    assert windows_downloads_on_wsl() == [fake_wsl / "c" / "Users" / "Sam" / "Downloads"]


def test_system_profiles_are_never_offered_as_somebodys_downloads(fake_wsl):
    # Windows ships these on every machine, and Public in particular has a real
    # Downloads folder - so counting them would make every box look ambiguous.
    for name in ("Public", "Default", "Default User", "All Users", "WsiAccount"):
        _profile(fake_wsl, name)
    _profile(fake_wsl, "Sam")
    assert windows_downloads_on_wsl() == [fake_wsl / "c" / "Users" / "Sam" / "Downloads"]


def test_two_real_people_are_not_guessed_between(fake_wsl):
    _profile(fake_wsl, "Sam")
    _profile(fake_wsl, "Sam Jones")
    assert windows_downloads_on_wsl() == []


def test_a_profile_without_a_downloads_folder_is_not_one(fake_wsl):
    _profile(fake_wsl, "Sam", downloads=False)
    assert windows_downloads_on_wsl() == []


def test_a_second_drive_is_looked_at_too(fake_wsl):
    _profile(fake_wsl, "Sam", drive="d")
    assert windows_downloads_on_wsl() == [fake_wsl / "d" / "Users" / "Sam" / "Downloads"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the WSL branch is Linux-only")
def test_the_windows_folder_becomes_the_default_on_wsl(fake_wsl):
    downloads = _profile(fake_wsl, "Sam") / "Downloads"
    assert str(downloads) in default_download_dirs()


def test_the_default_folder_is_untouched_off_wsl(not_wsl):
    assert str(Path("~/Downloads").expanduser()) in default_download_dirs()


def test_a_windows_drive_is_polled_because_no_events_reach_it(fake_wsl):
    assert needs_polling(fake_wsl / "c" / "Users" / "Sam" / "Downloads")


def test_a_linux_folder_keeps_its_native_events(fake_wsl, tmp_path: Path):
    assert not needs_polling(tmp_path / "elsewhere")


def test_nothing_is_polled_for_that_reason_off_wsl(not_wsl):
    assert not needs_polling(Path("/mnt/c/Users/Sam/Downloads"))


def test_a_misspelt_key_is_reported_by_its_dotted_name(tmp_path: Path):
    from lambda_watcher.config import unknown_keys

    config = tmp_path / "config.yaml"
    config.write_text('watch:\n  dir: ["/x"]\n  stable_seconds: 3\nstoree:\n  root: /y\n',
                      encoding="utf-8")
    assert unknown_keys(config) == ["storee", "watch.dir"]


def test_a_misspelt_key_still_lets_the_config_load(tmp_path: Path):
    # Ignoring unknown keys is what lets a config from an older release load, so
    # reporting them must not turn into refusing them.
    from lambda_watcher.config import load_config

    config = tmp_path / "config.yaml"
    config.write_text("watch:\n  dir: [\"/x\"]\n  stable_seconds: 3\n", encoding="utf-8")
    assert load_config(config).watch.stable_seconds == 3


def test_every_real_setting_is_recognised(tmp_path: Path):
    # The generated config must not report itself as full of typos.
    from lambda_watcher.config import unknown_keys
    from lambda_watcher.templates import render_config

    config = tmp_path / "config.yaml"
    config.write_text(render_config(["/x"]), encoding="utf-8")
    assert unknown_keys(config) == []


def test_an_unreadable_config_reports_no_keys_rather_than_raising(tmp_path: Path):
    from lambda_watcher.config import unknown_keys

    config = tmp_path / "config.yaml"
    config.write_text("watch: [unterminated", encoding="utf-8")
    assert unknown_keys(config) == []


def test_the_log_rolls_over_rather_than_growing_forever(tmp_path: Path):
    from logging.handlers import RotatingFileHandler

    from lambda_watcher.utils import setup_logging

    logger = setup_logging("INFO", tmp_path / "watcher.log")
    try:
        assert isinstance(logger.handlers[0], RotatingFileHandler)
        assert logger.handlers[0].maxBytes > 0
    finally:
        setup_logging("INFO", None)
