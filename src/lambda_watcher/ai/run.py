"""Explaining a comparison, start to finish — on demand, or in the background as versions arrive.

:func:`explain_diff` is the whole request: build the prompt, ask the model,
read the answer. :func:`rewrite_pages` puts the result where people look for
it. :class:`Explainer` runs both on a thread of its own for the watcher, so a
download is archived, reported and notified about in the usual second or two,
and the explanation joins the report when it is ready rather than holding up
every download queued behind it.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils import LOG, slugify, utc_now_iso
from .explanation import Explanation, clear_pending, parse_answer, save_done, save_failed
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_prompt
from .providers import AIError, complete
from .report import panel_for
from .settings import AISettings, ModelEntry

if TYPE_CHECKING:
    from ..config import Config
    from ..db import Database
    from ..diffing.compare import VersionDiff
    from ..store import Store

#: The smallest prompt worth sending after a "too large" refusal has halved it
#: a few times. Below this the model would be explaining a list of filenames.
MIN_PROMPT_CHARS = 8_000


def explain_diff(
    diff: VersionDiff,
    entry: ModelEntry,
    settings: AISettings,
    *,
    on_retry: Callable[[int, float, AIError], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Explanation:
    """Ask ``entry`` to explain ``diff``, and return what it said with where it came from.

    A "too large" refusal is handled here rather than surfaced: the prompt is
    rebuilt at half the size and sent again, down to :data:`MIN_PROMPT_CHARS`,
    so a model with a smaller context window than its service's default still
    gets an answer — about fewer of the files, which the explanation then says.

    Raises :class:`AIError` for everything else, including an answer that came
    back empty or cut off before it could be read.
    """
    budget = settings.prompt_budget(entry)
    while True:
        built = build_prompt(diff, send_code=settings.send_code, budget=budget)
        try:
            reply = complete(entry, SYSTEM_PROMPT, built.text, timeout=settings.timeout(entry),
                             retries=settings.max_retries, on_retry=on_retry, sleep=sleep)
            break
        except AIError as error:
            if error.kind != "too-large" or budget // 2 < MIN_PROMPT_CHARS:
                raise
            LOG.info("prompt too large for %s at %d characters; retrying at %d",
                     entry.label, budget, budget // 2)
            budget //= 2

    explanation = parse_answer(reply.text, built.paths)
    if reply.truncated and (not explanation.structured or explanation.is_empty):
        raise AIError("bad-response", f"{entry.label} ran out of room before finishing its answer",
                      hint="try a model that writes longer answers, with --model <name>")
    if explanation.is_empty:
        raise AIError("bad-response", f"{entry.label} sent back an empty answer",
                      hint="try again, or another model with --model <name>")
    explanation.provider = entry.provider
    explanation.model = reply.model or entry.model
    explanation.model_name = entry.name
    explanation.created_at = utc_now_iso()
    explanation.prompt_version = PROMPT_VERSION
    explanation.files_sent = built.files_sent
    explanation.files_total = built.files_total
    explanation.omitted = built.omitted
    explanation.withheld = built.withheld
    explanation.redactions = built.redactions
    explanation.send_code = built.send_code
    explanation.input_tokens = reply.input_tokens
    explanation.output_tokens = reply.output_tokens
    explanation.seconds = round(reply.seconds, 1)
    explanation.attempts = reply.attempts
    return explanation


def rewrite_pages(
    cfg: Config,
    db: Database,
    store: Store,
    function_id: int,
    diff: VersionDiff,
    settings: AISettings,
    *,
    refresh_index: bool = True,
) -> Path:
    """Write this pair's comparison page again with its current AI card, and return where.

    ``reports/<function>/v0006-v0007.html`` always — it is the page the history
    and the archive index link, and the one ``lw explain`` names. ``latest.html``
    too, but only while this pair *is* the latest: if another version arrived
    while the explanation was being written, ``latest.html`` already shows that
    newer pair, and overwriting it with this one would be going backwards.
    The archive's front page is refreshed when it exists, since it quotes the
    newest explanation's headline.
    """
    from ..diffing.build import write_archive_index
    from ..diffing.render_html import write_html

    panel = panel_for(store, diff, settings)
    folder = cfg.reports_dir / slugify(diff.function_name)
    archive_href = "../index.html" if (cfg.reports_dir / "index.html").exists() else None
    history_href = "index.html" if (folder / "index.html").exists() else None
    target = folder / f"v{diff.a_seq:04d}-v{diff.b_seq:04d}.html"
    write_html(diff, target, archive_href=archive_href, history_href=history_href, ai=panel)
    recent = db.list_versions(function_id, limit=2)
    if (len(recent) == 2 and int(recent[0]["seq"]) == diff.b_seq
            and int(recent[1]["seq"]) == diff.a_seq):
        write_html(diff, folder / "latest.html", archive_href=archive_href,
                   history_href=history_href, ai=panel)
    if refresh_index and archive_href:
        write_archive_index(db, cfg.reports_dir, store=store)
    return target


@dataclass
class ExplainJob:
    """One comparison waiting to be explained: which function, and the diff already computed for it."""

    function_id: int
    diff: VersionDiff


class _Stopping(Exception):
    """Raised inside a backoff wait when the explainer is asked to stop."""


class Explainer:
    """Explains newly archived versions on a thread of its own.

    The watcher's ingest thread is the single queue every download waits in,
    and a model can take half a minute to answer — longer when it is being
    rate limited and backed off. So the ingest marks the pair as pending,
    writes the report with a "being written" card, hands the job here and moves
    on. This thread then asks the model, saves the answer, rewrites the pages
    and fires a notification.

    It writes files only — the explanation record and the report pages — and
    only *reads* the index, so the one-writer rule for ``index.db`` holds.

    Settings are re-read for every job, so ``lw ai off`` or ``lw ai use`` in a
    terminal changes what the running watcher does next, with no restart.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        store: Store,
        *,
        on_done: Callable[[ExplainJob, Explanation | None, AIError | None], None] | None = None,
    ) -> None:
        """Bind the explainer to an archive; its thread starts with the first job."""
        self.cfg = cfg
        self.db = db
        self.store = store
        self.on_done = on_done
        self._queue: queue.Queue[ExplainJob | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, job: ExplainJob) -> None:
        """Queue a comparison to be explained, starting the worker thread if it is not running."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="lw-explain", daemon=True)
                self._thread.start()
        self._queue.put(job)

    @property
    def outstanding(self) -> int:
        """How many jobs are queued or running."""
        return self._queue.unfinished_tasks

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until every queued job has finished; returns False if ``timeout`` ran out first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Stop after the current job, marking every job that never started as not pending.

        A request already on the wire cannot be recalled, and it is not worth
        holding a shutdown for; if it is abandoned, its record is left pending
        with this process's id, which the next page render recognises as
        interrupted (see :meth:`~.explanation.Record.pending_is_stale`).
        """
        self._stop.set()
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                self._abandon(job)
            self._queue.task_done()
        # No sentinel: the loop checks the stop flag twice a second, and a
        # sentinel it never reaches would leave `outstanding` stuck at one.
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _wait(self, seconds: float) -> None:
        """Back off between retries, giving up at once if the explainer is stopping."""
        if self._stop.wait(seconds):
            raise _Stopping()

    def _loop(self) -> None:
        """Take jobs until told to stop. One bad job is logged; it never ends the thread."""
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if job is None:
                    break
                self._run(job)
            except Exception:                          # noqa: BLE001 - the thread must survive
                LOG.exception("explaining a version failed")
            finally:
                self._queue.task_done()

    def _abandon(self, job: ExplainJob) -> None:
        """Take a job's pending mark back off and redraw its pages as though it was never queued."""
        diff = job.diff
        try:
            clear_pending(self.store, diff.a_meta, diff.b_meta)
            rewrite_pages(self.cfg, self.db, self.store, job.function_id, diff,
                          AISettings.load(self.cfg.root))
        except Exception as exc:                       # noqa: BLE001 - tidying up only
            LOG.debug("could not tidy up an abandoned explanation: %s", exc)

    def _run(self, job: ExplainJob) -> None:
        """Explain one comparison, save the outcome, redraw its pages and say so.

        Every outcome is saved, failures included, because the report is where
        someone will find out what happened: "rate limited after 5 attempts —
        ``lw explain orders --from 6 --to 7`` tries again" on the page beats a
        line in a log nobody reads.
        """
        diff = job.diff
        settings = AISettings.load(self.cfg.root)
        entry = settings.auto_entry()
        if entry is None:
            # Switched off, or the model removed, after the job was queued.
            self._abandon(job)
            return
        explanation: Explanation | None = None
        failure: AIError | None = None
        try:
            explanation = explain_diff(diff, entry, settings, sleep=self._wait)
            save_done(self.store, diff.a_meta, diff.b_meta, explanation)
            LOG.info("explained %s v%04d → v%04d with %s", diff.function_name, diff.a_seq,
                     diff.b_seq, entry.label)
        except _Stopping:
            self._abandon(job)
            return
        except AIError as error:
            failure = error
            save_failed(self.store, diff.a_meta, diff.b_meta, entry.label, error.as_dict())
            LOG.warning("could not explain %s v%04d: %s", diff.function_name, diff.b_seq, error)
        except Exception as exc:                       # noqa: BLE001 - saved and shown instead
            LOG.exception("explaining %s v%04d failed", diff.function_name, diff.b_seq)
            failure = AIError("bad-response", f"an unexpected error: {exc}",
                              hint="`lw logs` has the details")
            save_failed(self.store, diff.a_meta, diff.b_meta, entry.label, failure.as_dict())
        try:
            rewrite_pages(self.cfg, self.db, self.store, job.function_id, diff, settings)
        except Exception as exc:                       # noqa: BLE001 - never lose the answer over a page
            LOG.warning("could not rewrite the report for %s v%04d: %s",
                        diff.function_name, diff.b_seq, exc)
        if explanation is not None and self.cfg.notify.enabled:
            from ..notify import notify

            notify(f"What changed in {diff.function_name} v{diff.b_seq:04d}",
                   explanation.headline or explanation.summary[:140], enabled=True)
        if self.on_done is not None:
            try:
                self.on_done(job, explanation, failure)
            except Exception:                          # noqa: BLE001 - a printing callback
                LOG.exception("explanation callback failed")
