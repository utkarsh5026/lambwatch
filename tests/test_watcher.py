import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent

from lambda_watcher.gitmirror import git_available
from lambda_watcher.ingest import Ingestor, wait_until_stable
from lambda_watcher.watcher import Watcher, _Handler
from tests.conftest import PY_V1, PY_V2


def _write_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for member, content in files.items():
            zf.writestr(member, content)
    return path


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_watcher_archives_a_file_dropped_into_the_folder(cfg, db, downloads: Path):
    cfg.watch.force_polling = True       # deterministic on every platform
    cfg.watch.polling_interval = 0.2
    cfg.watch.stable_seconds = 0.2
    cfg.watch.scan_on_start = False

    results = []
    watcher = Watcher(cfg, db, Ingestor(cfg, db), on_result=results.append)
    watcher.start()
    try:
        _write_zip(downloads / "order-processor.zip", {"lambda_function.py": PY_V1})
        assert _wait_for(lambda: results), "the watcher never picked the file up"
    finally:
        watcher.stop()

    assert results[0].status == "new"
    assert results[0].function_name == "order-processor"


def test_partial_downloads_are_ignored_until_renamed(cfg, db, downloads: Path):
    cfg.watch.force_polling = True
    cfg.watch.polling_interval = 0.2
    cfg.watch.stable_seconds = 0.2
    cfg.watch.scan_on_start = False

    results = []
    watcher = Watcher(cfg, db, Ingestor(cfg, db), on_result=results.append)
    watcher.start()
    try:
        partial = downloads / "order-processor.zip.crdownload"
        _write_zip(partial, {"lambda_function.py": PY_V1})
        time.sleep(1.0)
        assert results == [], "a .crdownload file must never be archived"

        partial.rename(downloads / "order-processor.zip")
        assert _wait_for(lambda: results), "the completed download was not archived"
    finally:
        watcher.stop()

    assert results[0].status == "new"


def test_startup_scan_replays_in_chronological_order(cfg, db, downloads: Path):
    cfg.watch.force_polling = True
    cfg.watch.stable_seconds = 0.1

    import os

    older = _write_zip(downloads / "fn-a.zip", {"lambda_function.py": PY_V1})
    newer = _write_zip(downloads / "fn-b.zip", {"lambda_function.py": PY_V2})
    now = time.time()
    os.utime(newer, (now, now))
    os.utime(older, (now - 3600, now - 3600))

    results = []
    watcher = Watcher(cfg, db, Ingestor(cfg, db), on_result=results.append)
    watcher.start()
    try:
        assert _wait_for(lambda: len(results) == 2)
    finally:
        watcher.stop()

    # Oldest file first, so version numbers follow real history.
    assert [r.source.name for r in results] == ["fn-a.zip", "fn-b.zip"]


def test_a_modified_event_for_an_untouched_file_is_ignored(cfg, db, downloads: Path):
    """Windows raises "modified" when nothing was written.

    watchdog subscribes to attribute, security and last-access changes as well
    as writes, so an antivirus sweep, the search indexer or OneDrive
    dehydrating a folder re-announces every zip sitting in it. Those files have
    not changed, and re-reading them is how old downloads got dragged back
    through the pipeline.
    """
    queued: list[tuple[Path, str]] = []
    handler = _Handler(
        lambda path, reason: queued.append((path, reason)),
        Ingestor(cfg, db).is_candidate,
        cfg.watch.arrival_max_age_seconds,
    )

    downloading = _write_zip(downloads / "fresh.zip", {"lambda_function.py": PY_V1})
    settled = _write_zip(downloads / "last-month.zip", {"lambda_function.py": PY_V2})
    long_ago = time.time() - 30 * 86400
    os.utime(settled, (long_ago, long_ago))

    handler.on_modified(FileModifiedEvent(str(downloading)))
    handler.on_modified(FileModifiedEvent(str(settled)))
    assert [path.name for path, _ in queued] == ["fresh.zip"]

    # Age only disqualifies an event that claims a write. A zip *arriving* in
    # the folder keeps whatever mtime it was copied with, and is still ours.
    handler.on_created(FileCreatedEvent(str(settled)))
    assert [path.name for path, _ in queued] == ["fresh.zip", "last-month.zip"]


def test_startup_scan_finds_files_it_may_not_delete(cfg, db, downloads: Path):
    cfg.watch.force_polling = True
    cfg.watch.stable_seconds = 0.1
    cfg.store.on_ingest = "move"

    source = _write_zip(downloads / "fn.zip", {"lambda_function.py": PY_V1})
    results = []
    watcher = Watcher(cfg, db, Ingestor(cfg, db), on_result=results.append)
    watcher.start()
    try:
        assert _wait_for(lambda: results)
    finally:
        watcher.stop()

    # Archived, as a scan should: the zip moved into the version directory.
    assert results[0].status == "new"
    assert not source.exists()

    # But content the archive already holds is only ever left where it was: a
    # scan has no way of knowing whether this zip just arrived.
    again = _write_zip(downloads / "fn.zip", {"lambda_function.py": PY_V1})
    watcher = Watcher(cfg, db, Ingestor(cfg, db), on_result=results.append)
    watcher.start()
    try:
        assert _wait_for(lambda: len(results) == 2)
    finally:
        watcher.stop()
    assert results[1].status in {"duplicate-download", "unchanged"}
    assert again.exists()


def test_wait_until_stable_waits_for_a_growing_file(tmp_path: Path):
    import threading

    path = tmp_path / "growing.bin"
    path.write_bytes(b"0" * 1000)

    def grow() -> None:
        for _ in range(3):
            time.sleep(0.2)
            with path.open("ab") as fh:
                fh.write(b"0" * 1000)

    thread = threading.Thread(target=grow)
    thread.start()
    assert wait_until_stable(path, stable_seconds=0.4, max_wait=10, poll=0.1)
    thread.join()
    assert path.stat().st_size == 4000


def test_wait_until_stable_gives_up_on_a_missing_file(tmp_path: Path):
    assert not wait_until_stable(tmp_path / "nope.zip", stable_seconds=0.1, max_wait=1)


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_git_mirror_records_one_commit_per_version(cfg, db, downloads: Path):
    cfg.git_mirror.enabled = True
    ingestor = Ingestor(cfg, db)
    ingestor.ingest(_write_zip(downloads / "fn.zip", {"lambda_function.py": PY_V1}))
    ingestor.ingest(_write_zip(downloads / "fn.zip", {"lambda_function.py": PY_V2}))

    repo = Path(cfg.root) / "repos" / "fn"
    assert (repo / ".git").exists()

    tags = subprocess.run(
        ["git", "-C", str(repo), "tag"], capture_output=True, text=True
    ).stdout.split()
    assert tags == ["v0001", "v0002"]

    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "v0001", "v0002", "--stat"],
        capture_output=True, text=True,
    ).stdout
    assert "lambda_function.py" in diff


def test_a_windows_folder_is_polled_even_when_the_config_never_asked(cfg, db, tmp_path: Path, monkeypatch):
    # The failure this prevents is silent: the native observer attaches to a
    # /mnt folder, reports success, and never fires once - so a config written
    # before this existed would keep a watcher that is running and useless.
    from lambda_watcher import config as config_module
    from watchdog.observers.polling import PollingObserver

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    mount = tmp_path / "mnt"
    monkeypatch.setattr(config_module, "_WSL_MOUNT_ROOT", mount)
    windows_downloads = mount / "c" / "Users" / "Sam" / "Downloads"
    windows_downloads.mkdir(parents=True)

    cfg.watch.force_polling = False
    watcher = Watcher(cfg, db, Ingestor(cfg, db))
    observer_cls, polling, reason = watcher._choose_observer([windows_downloads])

    assert observer_cls is PollingObserver and polling
    assert str(windows_downloads) in reason


def test_an_ordinary_folder_is_left_on_native_events(cfg, db, downloads: Path, monkeypatch):
    from watchdog.observers import Observer

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    from lambda_watcher import config as config_module
    monkeypatch.setattr(config_module, "_WSL_OSRELEASE", downloads / "nope")

    cfg.watch.force_polling = False
    watcher = Watcher(cfg, db, Ingestor(cfg, db))
    observer_cls, polling, _ = watcher._choose_observer([downloads])

    assert observer_cls is Observer and not polling


def test_a_running_watcher_says_where_it_is_looking(cfg, db, downloads: Path):
    # The service manager can only say a process exists. The heartbeat is what
    # says it attached to the folder, which is what someone actually needs to know.
    from lambda_watcher import heartbeat

    cfg.watch.force_polling = True
    cfg.watch.polling_interval = 0.2
    cfg.watch.scan_on_start = False
    watcher = Watcher(cfg, db, Ingestor(cfg, db))
    watcher.start()
    try:
        beat = heartbeat.read(cfg.heartbeat_path)
        assert beat is not None and beat.is_running()
        assert beat.dirs == [str(downloads)]
        assert beat.observer == "polling"
    finally:
        watcher.stop()

    stopped = heartbeat.read(cfg.heartbeat_path)
    assert stopped is not None and stopped.stopped_at
    assert not stopped.is_running()


def test_starting_and_stopping_are_recorded_so_idle_is_not_mistaken_for_dead(cfg, db, downloads: Path):
    cfg.watch.force_polling = True
    cfg.watch.polling_interval = 0.2
    cfg.watch.scan_on_start = False
    watcher = Watcher(cfg, db, Ingestor(cfg, db))
    watcher.start()
    watcher.stop()
    kinds = [row["kind"] for row in db.recent_events(10)]
    assert "watcher-started" in kinds and "watcher-stopped" in kinds


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM is how POSIX service managers stop a process")
def test_a_termination_request_is_a_clean_stop_not_a_crash(tmp_path: Path):
    # systemd, launchd and `lw stop` all end the watcher with SIGTERM. Before it
    # was handled the process just died, so a deliberate stop looked like a crash.
    import signal
    import sys

    from lambda_watcher import heartbeat

    home = tmp_path / "store"
    downloads = tmp_path / "dl"
    downloads.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f'watch:\n  dirs: ["{downloads.as_posix()}"]\n  scan_on_start: false\n'
        "git_mirror:\n  enabled: false\n",
        encoding="utf-8",
    )
    env = {**os.environ, "LAMBDA_WATCHER_HOME": str(home), "LAMBDA_WATCHER_CONFIG": str(config)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "lambda_watcher", "watch"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for(lambda: (home / "state" / "watcher.json").exists(), timeout=20)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()

    beat = heartbeat.read(home / "state" / "watcher.json")
    assert beat is not None and beat.stopped_at


def test_a_java_package_passed_over_says_why_where_it_will_be_read(tmp_path: Path, caplog):
    # A .jar Lambda is a zip with another name. Skipping it silently left no
    # answer anywhere to "did it even notice my download?".
    import logging

    from lambda_watcher.utils import LOG

    queued = []
    handler = _Handler(lambda p, r: queued.append(p), lambda p: p.suffix == ".zip")
    LOG.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="lambda_watcher"):
            handler.on_created(FileCreatedEvent(str(tmp_path / "order-processor.jar")))
    finally:
        LOG.propagate = False
    assert not queued
    assert "order-processor.jar" in caplog.text and "watch.extensions" in caplog.text


def test_an_ordinary_download_is_skipped_without_filling_the_log(tmp_path: Path, caplog):
    import logging

    from lambda_watcher.utils import LOG

    handler = _Handler(lambda p, r: None, lambda p: p.suffix == ".zip")
    LOG.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="lambda_watcher"):
            handler.on_created(FileCreatedEvent(str(tmp_path / "holiday.jpg")))
    finally:
        LOG.propagate = False
    assert "holiday.jpg" not in caplog.text
