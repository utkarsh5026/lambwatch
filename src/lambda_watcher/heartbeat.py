"""What a running watcher says about itself, on disk.

The service manager can only answer whether a process exists. That is not the
question anyone actually has: a watcher pointed at a folder that no longer exists,
or one whose events never arrive, is a live process doing nothing, and until this
file existed it reported itself healthy indefinitely. So the watcher writes down
what it is really doing — which folders it attached to, how it is listening, when
it last looked — and ``lw`` and ``lw doctor`` read it back.

Deliberately a small JSON file rather than a row in ``index.db``: it has to be
readable while the database is locked, being rebuilt, or missing altogether,
because that is exactly when someone is asking whether the watcher is alive.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .utils import LOG, parse_iso, utc_now_iso

#: How often a running watcher refreshes the file. Also the unit staleness is
#: judged in: a heartbeat older than a few of these means nobody is writing it.
BEAT_SECONDS = 60.0

#: How far behind the heartbeat may fall before the watcher is treated as stalled
#: rather than merely idle. Generous on purpose — a machine that was asleep, or a
#: worker busy with one enormous zip, must not be reported as broken.
STALE_AFTER_SECONDS = BEAT_SECONDS * 5


@dataclass
class Heartbeat:
    """One watcher's account of itself: where it is looking, and when it last did."""

    pid: int
    started_at: str
    last_beat_at: str
    #: The folders actually attached to, which is not always the configured list —
    #: a directory that did not exist at start-up is skipped, and that difference
    #: is the whole reason someone is reading this file.
    dirs: list[str] = field(default_factory=list)
    #: ``polling`` or ``native events``, with the reason the choice was made.
    observer: str = ""
    observer_reason: str = ""
    #: Files queued for ingest since this watcher started. Zero on a watcher that
    #: has been up for days is the signal that something is pointed wrong.
    seen: int = 0
    stopped_at: str = ""

    def age_seconds(self, now: str | None = None) -> float | None:
        """Seconds since the last beat, or ``None`` if the timestamp is unreadable."""
        beat = parse_iso(self.last_beat_at)
        current = parse_iso(now or utc_now_iso())
        if beat is None or current is None:
            return None
        return (current - beat).total_seconds()

    def is_stale(self, now: str | None = None) -> bool:
        """Whether nothing has refreshed this file for long enough to be worrying.

        An unreadable timestamp counts as stale: a heartbeat we cannot date is not
        evidence of life, and this file exists precisely to stop absence of
        evidence reading as a green light.
        """
        age = self.age_seconds(now)
        return age is None or age > STALE_AFTER_SECONDS

    def is_running(self) -> bool:
        """Whether the process that wrote this is still on this machine.

        A clean shutdown records ``stopped_at`` and needs no probing. Otherwise the
        pid is checked with signal 0, which asks the kernel about the process
        without disturbing it. A pid we are not allowed to signal still exists, so
        ``PermissionError`` is a yes.
        """
        if self.stopped_at:
            return False
        try:
            os.kill(self.pid, 0)
        except PermissionError:
            return True
        except (OSError, ValueError):
            return False
        return True


def write(path: Path, beat: Heartbeat) -> None:
    """Record this watcher's state, replacing whatever was there.

    Written to a neighbouring temporary file and renamed, so a reader never sees
    half a JSON document however the writer is interrupted. Never raises: a
    watcher that cannot describe itself must still archive, so a failure here is
    logged and the run carries on.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(beat), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        LOG.debug("could not write the heartbeat: %s", exc)


def read(path: Path) -> Heartbeat | None:
    """The last heartbeat written, or ``None`` if there is none to be had.

    Returns ``None`` for every way this can go wrong — absent, unreadable, truncated,
    or written by a future release with fields this one does not know. A missing
    heartbeat is an ordinary state (nothing has run yet), not an error, so the
    caller gets one answer to handle rather than a choice of exceptions.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    known = {f for f in Heartbeat.__dataclass_fields__}
    try:
        return Heartbeat(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None
