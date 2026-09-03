"""The Downloads-folder watcher.

Filesystem events land on the observer thread and are queued; a single worker
thread does the real work, so a slow ingest never makes us miss an event. Every
candidate file is waited on until it stops growing, because browsers rename a
``.crdownload`` into place only at the very end — and some do not.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from watchdog.events import (
    DirMovedEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .config import Config
from .db import Database
from .ingest import IngestResult, Ingestor, wait_until_stable
from .utils import LOG


@dataclass
class _Job:
    path: Path
    reason: str


class _Handler(FileSystemEventHandler):
    """Translates watchdog events into ingest jobs."""

    def __init__(self, enqueue: Callable[[Path, str], None], is_candidate: Callable[[Path], bool]) -> None:
        self.enqueue = enqueue
        self.is_candidate = is_candidate

    def _maybe(self, raw_path: str | bytes, reason: str) -> None:
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if self.is_candidate(path):
            self.enqueue(path, reason)

    def on_created(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileCreatedEvent):
            self._maybe(event.src_path, "created")

    def on_moved(self, event: FileSystemEvent) -> None:
        # Chrome/Edge finish a download by renaming "foo.zip.crdownload" -> "foo.zip".
        if isinstance(event, FileMovedEvent) and not isinstance(event, DirMovedEvent):
            self._maybe(event.dest_path, "moved")

    def on_modified(self, event: FileSystemEvent) -> None:
        # Some browsers write in place without a rename; the stability wait and
        # the content hash make the extra events harmless.
        if isinstance(event, FileModifiedEvent):
            self._maybe(event.src_path, "modified")


class Watcher:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        ingestor: Ingestor | None = None,
        on_result: Callable[[IngestResult], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.ingestor = ingestor or Ingestor(cfg, db)
        self.on_result = on_result
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._observer = None
        self._worker: threading.Thread | None = None

    # -- queueing --------------------------------------------------------
    def enqueue(self, path: Path, reason: str = "manual") -> None:
        key = str(path)
        with self._pending_lock:
            if key in self._pending:
                return  # already queued; the stability wait covers late writes
            self._pending.add(key)
        LOG.debug("queued %s (%s)", path.name, reason)
        self._queue.put(_Job(path, reason))

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._process(job)
            except Exception:  # noqa: BLE001 - the worker must never die
                LOG.exception("failed to process %s", job.path)
            finally:
                with self._pending_lock:
                    self._pending.discard(str(job.path))
                self._queue.task_done()

    def _process(self, job: _Job) -> None:
        if not job.path.exists():
            return
        if not wait_until_stable(
            job.path, self.cfg.watch.stable_seconds, self.cfg.watch.max_wait_seconds
        ):
            return
        if not job.path.exists():  # moved or deleted while we waited
            return
        result = self.ingestor.ingest(job.path)
        if self.on_result:
            try:
                self.on_result(result)
            except Exception:  # noqa: BLE001
                LOG.exception("result callback failed")

    # -- startup scan ----------------------------------------------------
    def initial_scan(self) -> int:
        """Queue matching files that are already sitting in the watched folders."""
        if not self.cfg.watch.scan_on_start:
            return 0
        max_age = self.cfg.watch.scan_on_start_max_age_hours
        cutoff = None
        if max_age > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - max_age * 3600

        candidates: list[tuple[float, Path]] = []
        for directory in self.cfg.watch_dirs():
            if not directory.exists():
                LOG.warning("watch directory does not exist: %s", directory)
                continue
            entries = directory.rglob("*") if self.cfg.watch.recursive else directory.iterdir()
            for path in entries:
                try:
                    if not path.is_file() or not self.ingestor.is_candidate(path):
                        continue
                    mtime = path.stat().st_mtime
                    if cutoff is not None and mtime < cutoff:
                        continue
                except OSError:
                    continue
                candidates.append((mtime, path))

        # Oldest first, so catching up produces the same version order as
        # watching live would have.
        for _, path in sorted(candidates, key=lambda item: item[0]):
            self.enqueue(path, "startup-scan")
        if candidates:
            LOG.info("startup scan queued %d existing file(s)", len(candidates))
        return len(candidates)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        directories = [d for d in self.cfg.watch_dirs()]
        existing = [d for d in directories if d.exists()]
        if not existing:
            raise FileNotFoundError(
                "none of the configured watch directories exist: "
                + ", ".join(str(d) for d in directories)
            )

        observer_cls = PollingObserver if self.cfg.watch.force_polling else Observer
        kwargs = {"timeout": self.cfg.watch.polling_interval} if self.cfg.watch.force_polling else {}
        self._observer = observer_cls(**kwargs)  # type: ignore[operator]
        handler = _Handler(self.enqueue, self.ingestor.is_candidate)
        for directory in existing:
            self._observer.schedule(handler, str(directory), recursive=self.cfg.watch.recursive)
            LOG.info("watching %s", directory)

        self._worker = threading.Thread(target=self._work, name="lw-ingest", daemon=True)
        self._worker.start()
        self._observer.start()
        self.initial_scan()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=timeout)

    def wait_forever(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            LOG.info("stopping on keyboard interrupt")

    def drain(self, timeout: float = 60.0) -> None:
        """Block until the queue is empty. Used by tests and `backfill`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return
            time.sleep(0.1)
