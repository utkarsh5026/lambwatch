"""The Downloads-folder watcher.

Filesystem events land on the observer thread and are queued; a single worker
thread does the real work, so a slow ingest never makes us miss an event. Every
candidate file is waited on until it stops growing, because browsers rename a
``.crdownload`` into place only at the very end — and some do not.
"""

from __future__ import annotations

import os
import queue
import signal
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

from . import heartbeat
from .config import Config, needs_polling
from .db import Database
from .ingest import IngestResult, Ingestor, recently_written, wait_until_stable
from .utils import LOG, utc_now_iso


#: Queue reasons that mean "this file just landed here". The startup scan is
#: deliberately absent: it sweeps files that were already sitting in the folder,
#: and cannot tell an overnight download from a zip something merely touched, so
#: what it finds is archived but never cleared out of the watched folder.
ARRIVAL_REASONS = frozenset({"created", "moved", "modified", "manual"})


@dataclass
class _Job:
    """One queued file, and the reason it was queued.

    The reason survives into the ingest because it decides whether the download
    may be cleared out of the watched folder — see :data:`ARRIVAL_REASONS`.
    """

    path: Path
    reason: str


#: Suffixes that are archives in all but name. A Java Lambda ships as a ``.jar``,
#: which is a zip file with another extension; one of these arriving and being
#: passed over is the case where someone expected it archived, so it is logged
#: where they will see it rather than at DEBUG.
_ARCHIVE_LIKE = frozenset({".jar", ".war", ".tar", ".tgz", ".gz", ".7z", ".rar"})


class _Handler(FileSystemEventHandler):
    """Translates watchdog events into ingest jobs."""

    def __init__(
        self,
        enqueue: Callable[[Path, str], None],
        is_candidate: Callable[[Path], bool],
        arrival_max_age: float = 0.0,
    ) -> None:
        """Wire the watchdog callbacks to an enqueue function and a candidate test.

        ``arrival_max_age`` is the window a modify event's mtime has to fall inside
        to count as a real write; see :meth:`on_modified`.
        """
        self.enqueue = enqueue
        self.is_candidate = is_candidate
        self.arrival_max_age = arrival_max_age

    def _maybe(self, raw_path: str | bytes, reason: str, *, require_recent: bool = False) -> None:
        """Queue a path if it is a candidate, decoding the raw event path first.

        Watchdog hands back ``str`` or ``bytes`` depending on the platform backend.
        ``require_recent`` adds the mtime check that filters out the spurious
        Windows modify events.
        """
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if not self.is_candidate(path):
            if reason in ("created", "moved"):
                self._explain_skip(path, reason)
            return
        if require_recent and not self._recently_written(path):
            LOG.debug("ignoring %s event for %s: nothing was written to it", reason, path.name)
            return
        self.enqueue(path, reason)

    def _explain_skip(self, path: Path, reason: str) -> None:
        """Say why a file that has just arrived is not going to be archived.

        Nothing used to be logged here at any level, which made the commonest
        question — "did it even notice my download?" — unanswerable from the log.
        Only arrivals are explained: a browser's partial file is modified hundreds
        of times on its way to being renamed, and narrating each of those would bury
        everything else.

        An archive with the wrong suffix is logged at INFO with the setting that
        would change it, because that is the skip someone will come looking for.
        Everything else — the PDFs and images that make up most of a Downloads
        folder — goes to DEBUG, so the log stays readable on an ordinary day.
        """
        suffix = path.suffix.lower()
        if suffix in _ARCHIVE_LIKE:
            LOG.info("skipped %s: only watch.extensions are archived — add %s there if it is "
                     "a deployment package", path.name, suffix)
        else:
            LOG.debug("skipped %s (%s): not something this watcher collects", path.name, reason)

    def _recently_written(self, path: Path) -> bool:
        """True when the file's mtime is inside the arrival window.

        A file that cannot be stat'd counts as not recently written — it has usually
        been moved away again by the time we look.
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        return recently_written(mtime, self.arrival_max_age)

    def on_created(self, event: FileSystemEvent) -> None:
        """A new file appeared: queue it if it looks like a deployment package."""
        if isinstance(event, FileCreatedEvent):
            self._maybe(event.src_path, "created")

    def on_moved(self, event: FileSystemEvent) -> None:
        """A file was renamed into place: queue the destination.

        This is how Chrome and Edge finish a download, renaming
        ``foo.zip.crdownload`` to ``foo.zip`` once the bytes are all there.
        """
        if isinstance(event, FileMovedEvent) and not isinstance(event, DirMovedEvent):
            self._maybe(event.dest_path, "moved")

    def on_modified(self, event: FileSystemEvent) -> None:
        """A file was written in place: queue it, but only if it really changed.

        Some browsers write the final file without a rename, so these events
        cannot be ignored. But Windows also raises them when nothing was
        written: watchdog asks ReadDirectoryChangesW for attribute, security
        and last-access changes too, so an antivirus sweep, the search indexer
        or OneDrive dehydrating a folder re-announces every zip in it at once.

        mtime is the filter — see :meth:`_recently_written`.
        """
        if isinstance(event, FileModifiedEvent):
            self._maybe(event.src_path, "modified", require_recent=True)


class Watcher:
    """Watches the download folders and feeds what lands there to the ingestor.

    Two threads, deliberately. Watchdog's observer thread does nothing but
    recognise candidate files and put them on a queue, so a slow ingest can
    never make it miss an event; one worker thread drains that queue and does
    all the extraction and indexing, which is what keeps a single SQLite writer.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        ingestor: Ingestor | None = None,
        on_result: Callable[[IngestResult], None] | None = None,
    ) -> None:
        """Build a watcher over ``cfg``, with an optional ingestor and result callback.

        ``on_result`` is called on the worker thread after each ingest — the CLI
        uses it to print a line and fire a desktop notification. Nothing starts
        until :meth:`start`.
        """
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
        self._beat: heartbeat.Heartbeat | None = None
        self._beater: threading.Thread | None = None
        self._seen = 0

    # -- queueing --------------------------------------------------------
    def enqueue(self, path: Path, reason: str = "manual") -> None:
        """Queue one path for ingest, ignoring it if it is already waiting.

        The deduplication is per-path and lasts until the job finishes. Dropping a
        repeat is safe rather than lossy: the queued job has not started its
        stability wait yet, so any bytes still being written are picked up by the
        job already in flight.
        """
        key = str(path)
        with self._pending_lock:
            if key in self._pending:
                return  # already queued; the stability wait covers late writes
            self._pending.add(key)
        LOG.debug("queued %s (%s)", path.name, reason)
        self._seen += 1
        self._queue.put(_Job(path, reason))

    def _work(self) -> None:
        """The worker loop: pull jobs off the queue until asked to stop.

        Wakes twice a second so a stop is noticed promptly, and takes ``None`` as
        the shutdown signal. Every exception is caught and logged — the whole point
        of the background service is that one bad zip does not take the watcher down
        with it.
        """
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
        """Wait for one file to stop growing, then ingest it.

        The existence check happens twice on purpose: a download can be moved or
        deleted while the stability wait is still running, and by then the queued
        path is stale.

        ``just_downloaded`` is what tells the ingestor it may clear the file out of
        the watched folder, and it is true only for the reasons in
        :data:`ARRIVAL_REASONS` — never for the startup scan, which cannot tell an
        overnight download from a zip that has been sitting there for months.
        """
        if not job.path.exists():
            return
        if not wait_until_stable(
            job.path, self.cfg.watch.stable_seconds, self.cfg.watch.max_wait_seconds
        ):
            return
        if not job.path.exists():  # moved or deleted while we waited
            return
        result = self.ingestor.ingest(job.path, just_downloaded=job.reason in ARRIVAL_REASONS)
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

    # -- saying so --------------------------------------------------------
    def _write_beat(self, stopped: bool = False) -> None:
        """Refresh the heartbeat file with where this watcher has got to.

        Does nothing before :meth:`start` has built one. ``stopped`` records a
        clean shutdown, which is what lets a reader tell "this watcher exited" from
        "this watcher died" without probing a pid that may since have been reused.
        """
        if self._beat is None:
            return
        self._beat.last_beat_at = utc_now_iso()
        self._beat.seen = self._seen
        if stopped:
            self._beat.stopped_at = self._beat.last_beat_at
        heartbeat.write(self.cfg.heartbeat_path, self._beat)

    def _keep_beating(self) -> None:
        """Refresh the heartbeat on a timer for as long as the watcher runs.

        A watcher that is merely idle must keep saying so, or every quiet night
        would read as a crash. Waits on the stop event rather than sleeping, so
        shutdown is not held up by the better part of a minute.
        """
        while not self._stop.wait(heartbeat.BEAT_SECONDS):
            self._write_beat()

    def _record(self, kind: str, detail: object) -> None:
        """Log a watcher lifecycle event to the archive's audit trail.

        Ingest outcomes were the only thing ever recorded, so ``lw log`` on a
        healthy watcher that has simply seen nothing was byte-identical to ``lw
        log`` on one that died weeks ago. Starting and stopping are events too.

        Swallows its own failure: the audit trail is worth having and never worth
        stopping a watcher for.
        """
        try:
            with self.db.transaction():
                self.db.log_event(kind, utc_now_iso(), detail=detail)
        except Exception as exc:  # noqa: BLE001 - never fail a watcher over bookkeeping
            LOG.debug("could not record %s: %s", kind, exc)

    # -- lifecycle -------------------------------------------------------
    def _choose_observer(self, directories: list[Path]) -> tuple[type, bool, str]:
        """Pick the watchdog observer that will actually see these folders change.

        ``watch.force_polling`` is the explicit answer and always wins. The reason
        this is not simply that flag is :func:`config.needs_polling`: a folder on a
        Windows drive under WSL delivers no inotify events whatsoever, so the
        native observer attaches, reports success and never fires. Someone whose
        config was written before that was understood would otherwise keep a
        watcher that is running and useless, and would have to be told to go and
        edit a setting to get one that works.

        The returned reason is prose for the log and for ``lw doctor``: a watcher
        that quietly chose to poll is a watcher nobody can explain later.
        """
        if self.cfg.watch.force_polling:
            return PollingObserver, True, "watch.force_polling is set"
        blind = [d for d in directories if needs_polling(d)]
        if blind:
            return PollingObserver, True, f"no native events reach {blind[0]}"
        return Observer, False, "native events are available here"

    def start(self) -> None:
        """Start watching: bring up the observer, the worker, and the startup scan.

        Raises :class:`FileNotFoundError` if none of the configured directories
        exist, which is the one arrangement where there is nothing useful to do.
        Directories that are merely missing individually are skipped with a warning.

        Polling is used instead of native events when ``watch.force_polling`` is
        set — slower, but it works on network shares and in containers where native
        events never arrive, and the tests use it for determinism. See
        :meth:`_choose_observer` for the folders that get it without being asked.
        """
        directories = [d for d in self.cfg.watch_dirs()]
        existing = [d for d in directories if d.exists()]
        if not existing:
            raise FileNotFoundError(
                "none of the configured watch directories exist: "
                + ", ".join(str(d) for d in directories)
            )

        observer_cls, polling, reason = self._choose_observer(existing)
        kwargs = {"timeout": self.cfg.watch.polling_interval} if polling else {}
        self._observer = observer_cls(**kwargs)  # type: ignore[operator]
        LOG.info("watching by %s (%s)", "polling" if polling else "native events", reason)
        handler = _Handler(
            self.enqueue, self.ingestor.is_candidate, self.cfg.watch.arrival_max_age_seconds
        )
        for directory in existing:
            self._observer.schedule(handler, str(directory), recursive=self.cfg.watch.recursive)
            LOG.info("watching %s", directory)

        now = utc_now_iso()
        self._beat = heartbeat.Heartbeat(
            pid=os.getpid(), started_at=now, last_beat_at=now,
            dirs=[str(d) for d in existing],
            observer="polling" if polling else "native events",
            observer_reason=reason,
        )
        self._write_beat()
        self._record("watcher-started", {
            "dirs": [str(d) for d in existing], "observer": self._beat.observer, "reason": reason,
        })

        self._worker = threading.Thread(target=self._work, name="lw-ingest", daemon=True)
        self._worker.start()
        self._beater = threading.Thread(target=self._keep_beating, name="lw-beat", daemon=True)
        self._beater.start()
        self._observer.start()
        self.initial_scan()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the observer and the worker, waiting up to ``timeout`` for each."""
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        if self._beater is not None:
            self._beater.join(timeout=timeout)
        if self._beat is not None:
            self._write_beat(stopped=True)
            self._record("watcher-stopped", {"seen": self._seen})

    def wait_forever(self) -> None:
        """Block until stopped, translating Ctrl-C into a clean shutdown.

        What ``lw watch`` runs in the foreground; the service manager uses the same
        path with no terminal attached, and stops it with ``SIGTERM`` rather than
        Ctrl-C — see :meth:`stop_on_termination`, which has to be armed before
        :meth:`start` rather than here.
        """
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            LOG.info("stopping on keyboard interrupt")
            return
        LOG.info("stopping on request")

    def stop_on_termination(self) -> None:
        """Treat ``SIGTERM`` as a request for a clean stop rather than a kill.

        systemd, launchd and ``lw stop`` all end the watcher with ``SIGTERM``, and
        without a handler the process simply died — no final heartbeat, no
        ``watcher-stopped`` event, and a status that could not tell a deliberate
        stop from a crash.

        Call it *before* :meth:`start`. Armed any later, a request arriving during
        start-up — ``lw restart`` straight after ``lw start`` does exactly this —
        meets the default action and kills the process mid-scan; that window is
        also why a test of this was intermittent until the order was fixed.

        Signal handlers can only be installed from the main thread, and not every
        platform delivers ``SIGTERM`` the same way, so a refusal is logged and
        ignored: a watcher that cannot hear the request still stops, just less
        tidily. Kept out of :meth:`start` so that tests, which start watchers
        inside the test runner's own process, do not rewire its signals.
        """
        try:
            signal.signal(signal.SIGTERM, lambda _signum, _frame: self._stop.set())
        except (ValueError, OSError, AttributeError) as exc:
            LOG.debug("cannot handle SIGTERM here: %s", exc)

    def drain(self, timeout: float = 60.0) -> None:
        """Block until the queue is empty. Used by tests and `backfill`."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return
            time.sleep(0.1)
