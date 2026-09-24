"""A model's answer, made safe to draw, and the file it is kept in.

Two halves. :func:`parse_answer` turns whatever the model sent back into an
:class:`Explanation` — tolerating the fences, the preamble, the missing fields
and the invented file paths that models produce often enough to plan for. The
rest reads and writes the :class:`Record` saved beside each version, which is
what lets a report written without an explanation pick one up later, and a
page that is open while one is being written say so.

Where the record lives, and why there:
``functions/<slug>/versions/0007-a1b2c3d4/explanations/from-<12 hex>.json``,
inside the *newer* version's directory and named after the *older* version's
tree hash. Inside the version directory, so ``lw rename`` moving the function,
``lw merge`` renumbering it and ``lw rm`` or pruning deleting it all carry the
explanation along without knowing it exists. Named by tree hash rather than
version number, because a renumbering changes every number and no hash. And
never in ``index.db``, which may only hold what a rebuild from the manifests
can recover — an explanation is neither in a manifest nor reproducible.

An archive written before explanations existed simply has no such folders,
which reads as "not explained yet": nothing to migrate.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..utils import LOG, parse_iso, utc_now_iso

if TYPE_CHECKING:
    from ..store import Store

#: The folder, inside a version directory, that holds its explanations.
EXPLANATIONS_DIRNAME = "explanations"

#: Bumped only if the record's shape ever has to break; see :meth:`Record.from_dict`.
RECORD_SCHEMA = 1

#: How long a "being written" record is believed without its writer being
#: checked on. Longer than the worst case of one request — five attempts at
#: the default timeout, plus backoff — so a slow success is never mistaken
#: for a crash.
PENDING_STALE_SECONDS = 20 * 60

#: The kinds of change and risk levels the report has colours for. Anything
#: else the model says is kept, but drawn as ``other``.
CHANGE_KINDS = ("feature", "fix", "behaviour", "refactor", "dependency", "config", "security",
                "removal", "other")
LEVELS = ("low", "medium", "high")


@dataclass
class Point:
    """One item in a list of changes or risks: a title, a sentence or two, and the files it is about.

    ``kind`` is a change's category (``feature``, ``fix``…) or a risk's level
    (``high``, ``medium``, ``low``), already normalised to one the report can
    colour. ``files`` holds only paths that really are in the diff; see
    :func:`_match_path`.
    """

    kind: str
    title: str
    detail: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class Explanation:
    """Everything one explanation says, plus where it came from.

    The content fields mirror the JSON the prompt asks for (see
    :data:`~.prompt.SYSTEM_PROMPT`). ``structured`` is False when the model
    answered in prose instead of JSON: the prose is kept as the summary rather
    than thrown away, since it is usually still a fair answer.

    The provenance fields are what the report's footer is written from — "by
    claude-sonnet-5, from 14 of 16 changed files, 3 values redacted" — because
    an explanation is only as trustworthy as what it was shown.
    """

    headline: str = ""
    summary: str = ""
    risk: str = ""
    risk_reason: str = ""
    changes: list[Point] = field(default_factory=list)
    risks: list[Point] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    file_notes: dict[str, str] = field(default_factory=dict)
    structured: bool = True
    # -- provenance ------------------------------------------------------
    provider: str = ""
    model: str = ""
    model_name: str = ""
    created_at: str = ""
    prompt_version: int = 0
    files_sent: int = 0
    files_total: int = 0
    omitted: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    redactions: int = 0
    send_code: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None
    seconds: float = 0.0
    attempts: int = 1

    @property
    def is_empty(self) -> bool:
        """True when the answer said nothing a reader could use."""
        return not (self.headline or self.summary or self.changes or self.risks or self.checklist)

    def as_dict(self) -> dict[str, Any]:
        """The explanation as the JSON it is saved as, and ``lw explain --json`` prints."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Explanation:
        """Read a saved explanation back, defaulting anything missing or malformed.

        Saved by this release or a later one, so unknown keys are ignored and
        each list is rebuilt item by item, dropping only the items that are
        broken.
        """
        def points(items: Any) -> list[Point]:
            """The saved list of changes or risks, as :class:`Point` objects."""
            found = []
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get("title"):
                    found.append(Point(kind=str(item.get("kind") or "other"), title=str(item["title"]),
                                       detail=str(item.get("detail") or ""),
                                       files=[str(f) for f in item.get("files") or []]))
            return found

        scalars = {k: data[k] for k in (
            "headline", "summary", "risk", "risk_reason", "structured", "provider", "model",
            "model_name", "created_at", "prompt_version", "files_sent", "files_total", "redactions",
            "send_code", "input_tokens", "output_tokens", "seconds", "attempts",
        ) if k in data and data[k] is not None}
        return cls(
            **scalars,
            changes=points(data.get("changes")),
            risks=points(data.get("risks")),
            checklist=[str(s) for s in data.get("checklist") or [] if s],
            file_notes={str(k): str(v) for k, v in (data.get("file_notes") or {}).items()},
            omitted=[str(p) for p in data.get("omitted") or []],
            withheld=[str(p) for p in data.get("withheld") or []],
        )


# --------------------------------------------------------------------------- #
# Reading a model's answer
# --------------------------------------------------------------------------- #
def _json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a model's reply, however it was wrapped.

    Handles the three wrappings models actually use: nothing, a ```json fence,
    and a sentence of preamble before the object. Tries the outermost braces
    first and falls back to decoding from each ``{`` in turn, so a stray brace
    in the preamble does not sink the whole answer.
    """
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.S)
    if fence:
        stripped = fence.group(1)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(stripped[start:end + 1])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", stripped):
        try:
            value, _ = decoder.raw_decode(stripped[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _match_path(cited: Any, known: set[str]) -> str | None:
    """The real path a model meant, or ``None`` when it named a file the diff does not have.

    Models cite paths the way diffs print them — ``b/app.py``, ``./app.py``,
    in backticks — or by filename alone. Each of those is resolved to the one
    path in the diff it can mean; a filename shared by two paths, or a path
    that was invented, resolves to nothing, because a link to the wrong file is
    worse than no link.
    """
    text = str(cited or "").strip().strip("`'\"").strip()
    for prefix in ("a/", "b/", "./"):
        if text.startswith(prefix) and text not in known:
            text = text[len(prefix):]
    text = text.split(":")[0].strip()
    if not text:
        return None
    if text in known:
        return text
    tails = [p for p in known if p.endswith("/" + text)]
    return tails[0] if len(tails) == 1 else None


def _clip(value: Any, limit: int) -> str:
    """A model-supplied string, trimmed of whitespace and cut to ``limit`` characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _level(value: Any, default: str = "") -> str:
    """``High`` → ``high``; anything that is not a level → ``default``."""
    text = str(value or "").strip().lower()
    return text if text in LEVELS else default


def parse_answer(text: str, known_paths: set[str]) -> Explanation:
    """Turn a model's reply into an :class:`Explanation`, keeping all it got right.

    Every field is optional and every list item is checked on its own, so one
    malformed risk does not cost the other five. Lengths are capped, the lists
    are cut to the sizes the prompt asked for, and file references are
    resolved against ``known_paths`` — see :func:`_match_path`. A reply with no
    JSON object at all becomes an unstructured explanation whose summary is
    the reply.
    """
    data = _json_object(text)
    if data is None:
        prose = text.strip()
        first = re.split(r"(?<=[.!?])\s", prose, maxsplit=1)[0] if prose else ""
        return Explanation(headline=_clip(first, 160), summary=_clip(prose, 4000), structured=False)

    def files_of(item: dict[str, Any]) -> list[str]:
        """The item's cited files that exist in the diff, each once, in the order given."""
        found: list[str] = []
        for cited in item.get("files") or []:
            path = _match_path(cited, known_paths)
            if path and path not in found:
                found.append(path)
        return found

    changes: list[Point] = []
    for item in data.get("changes") or []:
        if isinstance(item, dict) and item.get("title"):
            kind = str(item.get("kind") or "other").strip().lower()
            changes.append(Point(kind=kind if kind in CHANGE_KINDS else "other",
                                 title=_clip(item["title"], 120), detail=_clip(item.get("detail"), 600),
                                 files=files_of(item)))
    risks: list[Point] = []
    for item in data.get("risks") or []:
        if isinstance(item, dict) and item.get("title"):
            risks.append(Point(kind=_level(item.get("level"), "medium"), title=_clip(item["title"], 120),
                               detail=_clip(item.get("detail"), 600), files=files_of(item)))
    notes: dict[str, str] = {}
    raw_notes = data.get("files")
    if isinstance(raw_notes, dict):
        for cited, note in raw_notes.items():
            path = _match_path(cited, known_paths)
            if path and note:
                notes[path] = _clip(note, 200)
    checklist = [_clip(step, 240) for step in data.get("checklist") or [] if str(step or "").strip()]
    return Explanation(
        headline=_clip(data.get("headline"), 160),
        summary=_clip(data.get("summary"), 1500),
        risk=_level(data.get("risk")),
        risk_reason=_clip(data.get("risk_reason"), 240),
        changes=changes[:8],
        risks=risks[:6],
        checklist=checklist[:8],
        file_notes=notes,
    )


# --------------------------------------------------------------------------- #
# The saved record
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    """What is known about explaining one pair of versions: done, being written, or failed.

    ``status`` is one of three things, and the report draws each differently:

    ``done``
        ``explanation`` holds the answer.
    ``pending``
        A request is in flight, started at ``started_at`` by process ``pid``
        using ``model``. ``explanation`` may still hold an earlier answer that
        this one will replace — a refresh keeps the old answer on screen.
    ``failed``
        The last attempt failed; ``error`` says how (kind, message, hint). An
        earlier answer, if there was one, is kept in ``explanation``.
    """

    status: str
    a_seq: int
    b_seq: int
    a_tree: str
    b_tree: str
    explanation: Explanation | None = None
    started_at: str = ""
    pid: int = 0
    model: str = ""
    error: dict[str, Any] | None = None
    updated_at: str = ""

    def pending_is_stale(self, now: datetime | None = None) -> bool:
        """Whether a ``pending`` record belongs to a request that can no longer finish.

        True when the process that started it has gone — the watcher was
        stopped, the terminal closed — or when it has been pending longer than
        :data:`PENDING_STALE_SECONDS` regardless. Without this a page opened
        after an interrupted run would say "being written" for ever.
        """
        if self.status != "pending":
            return False
        if self.pid and self.pid != os.getpid():
            from ..service import pid_alive

            if not pid_alive(self.pid):
                return True
        started = parse_iso(self.started_at)
        if started is None:
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - started).total_seconds() > PENDING_STALE_SECONDS

    def as_dict(self) -> dict[str, Any]:
        """The record as the JSON file it is saved in."""
        data = asdict(self)
        data["schema"] = RECORD_SCHEMA
        data["explanation"] = self.explanation.as_dict() if self.explanation else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Record | None:
        """Read a record back, or ``None`` when the file is not one."""
        if not isinstance(data, dict) or data.get("status") not in {"done", "pending", "failed"}:
            return None
        explanation = data.get("explanation")
        try:
            return cls(
                status=str(data["status"]),
                a_seq=int(data.get("a_seq") or 0), b_seq=int(data.get("b_seq") or 0),
                a_tree=str(data.get("a_tree") or ""), b_tree=str(data.get("b_tree") or ""),
                explanation=Explanation.from_dict(explanation) if isinstance(explanation, dict) else None,
                started_at=str(data.get("started_at") or ""), pid=int(data.get("pid") or 0),
                model=str(data.get("model") or ""),
                error=data.get("error") if isinstance(data.get("error"), dict) else None,
                updated_at=str(data.get("updated_at") or ""),
            )
        except (TypeError, ValueError):
            return None


def record_path(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any]) -> Path | None:
    """Where the record for one pair of versions lives, or ``None`` if the rows cannot say.

    ``a_meta`` and ``b_meta`` are the two version rows as dicts, as
    :class:`~lambda_watcher.diffing.compare.VersionDiff` carries them; the
    directory comes from the newer and the name from the older's tree hash.
    """
    stored_dir, tree = b_meta.get("dir"), a_meta.get("tree_hash")
    if not stored_dir or not tree:
        return None
    return store.resolve_version_dir(str(stored_dir)) / EXPLANATIONS_DIRNAME / f"from-{str(tree)[:12]}.json"


def load_record(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any]) -> Record | None:
    """The saved record for a pair of versions, or ``None`` when there is none (or it is unreadable).

    An unreadable file is logged and treated as absent: the worst outcome is
    that the pair gets explained again, which is far better than a report that
    will not render.
    """
    path = record_path(store, a_meta, b_meta)
    if path is None or not path.exists():
        return None
    try:
        return Record.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        LOG.warning("could not read %s: %s", path, exc)
        return None


def _write(path: Path, record: Record) -> None:
    """Save a record in one step, so a page rendering at the same moment never reads half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record.updated_at = utc_now_iso()
    scratch = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        scratch.write_text(json.dumps(record.as_dict(), indent=2) + "\n", encoding="utf-8")
        os.replace(scratch, path)
    finally:
        scratch.unlink(missing_ok=True)


def _fresh(a_meta: dict[str, Any], b_meta: dict[str, Any], status: str) -> Record:
    """A new record for a pair, carrying both versions' numbers and hashes."""
    return Record(status=status, a_seq=int(a_meta.get("seq") or 0), b_seq=int(b_meta.get("seq") or 0),
                  a_tree=str(a_meta.get("tree_hash") or ""), b_tree=str(b_meta.get("tree_hash") or ""))


def save_done(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any],
              explanation: Explanation) -> Record | None:
    """Record a finished explanation, replacing whatever was there."""
    path = record_path(store, a_meta, b_meta)
    if path is None:
        return None
    record = _fresh(a_meta, b_meta, "done")
    record.explanation = explanation
    record.model = explanation.model
    _write(path, record)
    return record


def save_pending(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any],
                 model: str) -> Record | None:
    """Record that an explanation is being written now, keeping any earlier answer on show."""
    path = record_path(store, a_meta, b_meta)
    if path is None:
        return None
    previous = load_record(store, a_meta, b_meta)
    record = _fresh(a_meta, b_meta, "pending")
    record.explanation = previous.explanation if previous else None
    record.started_at, record.pid, record.model = utc_now_iso(), os.getpid(), model
    _write(path, record)
    return record


def save_failed(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any],
                model: str, error: dict[str, Any]) -> Record | None:
    """Record that the last attempt failed and why, keeping any earlier answer on show."""
    path = record_path(store, a_meta, b_meta)
    if path is None:
        return None
    previous = load_record(store, a_meta, b_meta)
    record = _fresh(a_meta, b_meta, "failed")
    record.explanation = previous.explanation if previous else None
    record.model, record.error = model, error
    _write(path, record)
    return record


def clear_pending(store: Store, a_meta: dict[str, Any], b_meta: dict[str, Any]) -> None:
    """Undo a ``pending`` mark for a request that will never be made.

    Put back to ``done`` when an earlier answer exists, and removed otherwise,
    so the report reads as though the request had never been queued — which is
    the truth when AI was switched off, or the watcher stopped, before its turn
    came.
    """
    path = record_path(store, a_meta, b_meta)
    record = load_record(store, a_meta, b_meta)
    if path is None or record is None or record.status != "pending":
        return
    if record.explanation is None:
        path.unlink(missing_ok=True)
        return
    record.status, record.started_at, record.pid = "done", "", 0
    _write(path, record)
