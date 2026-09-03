import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from lambda_watcher.gitmirror import git_available
from lambda_watcher.ingest import Ingestor, wait_until_stable
from lambda_watcher.watcher import Watcher
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

    repo = Path(cfg.functions_dir) / "fn" / "git"
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
