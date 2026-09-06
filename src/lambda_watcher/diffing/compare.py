"""Compute a structured diff between two archived versions.

The output is deliberately layered, because "what changed" is rarely a
question about lines of text:

* the headline (runtime, handler, size, file counts)
* dependency changes, which explain most of the file churn in a zip
* environment variables and AWS services the code now needs
* new security findings
* and only then the per-file line diffs, first-party code first
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable
from typing import Any

from ..config import DiffConfig
from ..utils import matches_any, read_text
from .intraline import WordEdit, long_line_edits, mean_line_length


@dataclass
class FileRecord:
    """One file as recorded in the index."""

    path: str
    size: int
    sha256: str
    is_text: bool
    is_vendor: bool
    lang: str
    lines: int
    mode: int = 0o644

    @classmethod
    def from_row(cls, row: Any) -> FileRecord:
        """Build a record from an index row, filling in defaults for null columns."""
        return cls(
            path=row["path"], size=int(row["size"]), sha256=row["sha256"],
            is_text=bool(row["is_text"]), is_vendor=bool(row["is_vendor"]),
            lang=row["lang"] or "text", lines=int(row["lines"]), mode=int(row["mode"] or 0o644),
        )


@dataclass
class FileChange:
    """One file's fate between two versions.

    ``kind`` covers the five things that can happen to a file: ``added``,
    ``removed``, ``modified``, ``renamed`` and ``mode-changed``. For a rename,
    ``path`` is where the file ended up and ``old_path`` where it came from.

    ``diff_lines`` may be empty even for a real modification —
    ``skipped_reason`` then says why (binary, too large, ignored by config,
    whitespace only), so the renderer can explain the absence rather than imply
    nothing changed. Two of those absences are not refusals but better answers:
    a whitespace-only change is fully described by its label, and a file with no
    usable lines is described by ``word_edits`` instead. See :func:`_fill_diff`.
    """

    kind: str  # added | removed | modified | renamed | mode-changed
    path: str
    old_path: str | None = None
    old: FileRecord | None = None
    new: FileRecord | None = None
    diff_lines: list[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    truncated: bool = False
    binary: bool = False
    skipped_reason: str | None = None
    #: True when normalising whitespace makes the two sides identical: a
    #: retab, a reindent, trailing spaces, CRLF, blank lines. The file really did
    #: change and still counts as modified — but the hunk would be the whole file
    #: painted red and green to say nothing, so it is not computed. Line counts
    #: stay at zero for the same reason: ``+3/-3`` is the noise, not the news.
    whitespace_only: bool = False
    #: True when the file was diffed word by word because its lines are too long
    #: to diff by line — a minified bundle. ``word_edits`` then carries the
    #: change and ``diff_lines`` stays empty, whether or not any edit was
    #: quotable; ``skipped_reason`` says why when none was.
    long_lines: bool = False
    #: The changed runs of a ``long_lines`` file, with enough text either side to
    #: place them. Empty for every ordinary file.
    word_edits: list[WordEdit] = field(default_factory=list)

    @property
    def is_vendor(self) -> bool:
        """True when this file is a vendored dependency rather than first-party code.

        Taken from whichever side of the change exists, since a removed file has no
        new record and vice versa.
        """
        record = self.new or self.old
        return bool(record and record.is_vendor)

    @property
    def size_delta(self) -> int:
        """Bytes gained or lost. Negative when the file shrank; a full size for an add."""
        return (self.new.size if self.new else 0) - (self.old.size if self.old else 0)

    @property
    def line_count_note(self) -> str:
        """Why this file's ``+`` and ``−`` are blank, in three words. Empty when they are not.

        ``whitespace only``, ``minified — 1 edit``, ``ignored by config``: a file
        can change and still show no line counts, either because it was measured
        in some other unit or because it was never opened — and an unexplained
        blank reads as "nothing changed", which is the opposite of both. Both
        renderers put this where the missing number would have been.

        Empty under ``--no-patch``, where nothing was computed for any file and
        the header says so once instead.
        """
        if self.whitespace_only:
            return "whitespace only"
        if self.long_lines:
            if self.word_edits:
                return f"minified — {len(self.word_edits)} edit{'s' if len(self.word_edits) != 1 else ''}"
            return f"minified — {self.skipped_reason}" if self.skipped_reason else "minified"
        if self.diff_lines or self.kind == "mode-changed":
            return ""
        return self.skipped_reason or ""

    @property
    def lang(self) -> str:
        """The file's language label, from whichever side of the change exists."""
        record = self.new or self.old
        return record.lang if record else "text"

    def as_dict(self) -> dict:
        """This change as plain JSON-ready data, for the machine-readable diff."""
        return {
            "kind": self.kind,
            "path": self.path,
            "old_path": self.old_path,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "size_delta": self.size_delta,
            "binary": self.binary,
            "truncated": self.truncated,
            "is_vendor": self.is_vendor,
            "whitespace_only": self.whitespace_only,
            "long_lines": self.long_lines,
            "word_edits": [e.as_dict() for e in self.word_edits],
        }


@dataclass
class MoveGroup:
    """One directory move, told once instead of once per file.

    ``handlers/`` becoming ``lambda/handlers/`` is a single structural decision,
    but it reaches the diff as one rename per file: twenty rows that differ only
    in the filename at the end, which the reader has to compare against each
    other to discover they say nothing new. This is those rows folded back into
    the decision that produced them.

    The members stay in :attr:`VersionDiff.files` — this is a reading of them,
    not a replacement for them, so the per-file patches and the machine-readable
    diff still see every rename individually. :meth:`VersionDiff.file_rows` is
    what swaps the group in for its members, and only renderers walk that.
    """

    old_dir: str
    new_dir: str
    members: list[FileChange] = field(default_factory=list)
    #: What the source directory held before the move, so a renderer can tell
    #: "all of it" from "20 of 60 files" — different facts, and the second one
    #: read as the first is how a partial move becomes a wrong one.
    total_in_old_dir: int = 0

    @property
    def moved(self) -> int:
        """How many files moved between the two directories."""
        return len(self.members)

    @property
    def paths(self) -> list[str]:
        """Where each moved file ended up."""
        return [c.path for c in self.members]

    @property
    def edited_members(self) -> list[FileChange]:
        """The moved files that were also rewritten, which are the ones worth opening.

        Compares the two content hashes rather than the line counts, because
        ``--no-patch`` computes no line counts at all and this still has to be
        answerable there.

        A renderer that folds the group into one row must still show these
        somewhere: the row stands in for the *move*, and an edit that rode along
        with it is a separate fact that the move does not report.
        """
        return [
            c for c in self.members
            if c.old and c.new and c.old.sha256 != c.new.sha256
        ]

    @property
    def edited(self) -> int:
        """How many of the moved files also changed content on the way."""
        return len(self.edited_members)

    @property
    def added_lines(self) -> int:
        """Lines gained across the whole group, zero when diffs were not computed."""
        return sum(c.added_lines for c in self.members)

    @property
    def removed_lines(self) -> int:
        """Lines lost across the whole group, zero when diffs were not computed."""
        return sum(c.removed_lines for c in self.members)

    @property
    def size_delta(self) -> int:
        """Bytes gained or lost across the whole group."""
        return sum(c.size_delta for c in self.members)

    @property
    def is_vendor(self) -> bool:
        """True when every file in the move is vendored, so the row can be dimmed as one."""
        return all(c.is_vendor for c in self.members)

    @property
    def display_dirs(self) -> tuple[str, str]:
        """The two directory names as a reader should see them, root spelled ``.``.

        A package lifted out of the archive root has ``""`` for its old
        directory, which renders as ``{ → lambda}/`` — a gap where a name should
        be. ``{. → lambda}/`` says the same thing and looks deliberate.
        """
        return self.old_dir or ".", self.new_dir or "."

    @property
    def is_whole_dir(self) -> bool:
        """True when the source directory moved entire, with nothing left behind.

        False makes the move a partial one — ``20 of 60 files`` — which a
        renderer has to say out loud, since a reader told only ``handlers/ →
        lambda/handlers/`` would fairly conclude ``handlers/`` is now gone.
        """
        return self.total_in_old_dir > 0 and len(self.members) >= self.total_in_old_dir

    def as_dict(self) -> dict:
        """This move as plain JSON-ready data, beside the per-file rows it summarises."""
        return {
            "old_dir": self.old_dir, "new_dir": self.new_dir,
            "moved": self.moved, "edited": self.edited,
            "total_in_old_dir": self.total_in_old_dir, "whole_dir": self.is_whole_dir,
            "added_lines": self.added_lines, "removed_lines": self.removed_lines,
            "size_delta": self.size_delta, "paths": self.paths,
        }


@dataclass
class DepChange:
    """One dependency that appeared, disappeared or moved version.

    ``is_declared`` carries through from the dependency itself: a change to a
    declared range (``boto3>=1.34``) is a change of intent, while a change to an
    installed version (``boto3 1.34.0`` -> ``1.35.20``) is what actually
    shipped.
    """

    kind: str  # added | removed | changed
    manager: str
    name: str
    old_version: str | None = None
    new_version: str | None = None
    is_declared: bool = False

    def as_dict(self) -> dict:
        """This change as plain JSON-ready data, for the machine-readable diff."""
        return {
            "kind": self.kind, "manager": self.manager, "name": self.name,
            "old_version": self.old_version, "new_version": self.new_version,
            "is_declared": self.is_declared,
        }


@dataclass
class VersionDiff:
    """The complete comparison of two versions, in layers.

    Assembled roughly in the order a reader wants it: what the package now is
    (runtime, handler), what it depends on, what it needs from its environment,
    what the scanner noticed, and only last the per-file line diffs. The summary
    helpers below collapse this into one or two lines for a notification.
    """

    function_name: str
    a_seq: int
    b_seq: int
    a_meta: dict[str, Any] = field(default_factory=dict)
    b_meta: dict[str, Any] = field(default_factory=dict)

    #: The two version directories the comparison read. Renderers that want to
    #: colour a file need the file, not just the lines the diff quoted from it —
    #: a docstring is only a docstring if you can see where it opened.
    a_root: Path | None = None
    b_root: Path | None = None

    files: list[FileChange] = field(default_factory=list)
    vendor_files_changed: int = 0
    unchanged_files: int = 0
    #: Added files the similarity pass never got to before its pair budget ran
    #: out. Non-zero means the rename map is partial: some of the adds and
    #: removes below may be halves of the same moved file. Renderers say so,
    #: because a silently partial answer reads exactly like a complete one.
    renames_unexamined: int = 0
    #: Directory moves, each standing in for several of the renames in
    #: ``files``. Derived from them and never a substitute: see :class:`MoveGroup`.
    moves: list[MoveGroup] = field(default_factory=list)

    deps: list[DepChange] = field(default_factory=list)
    env_added: list[str] = field(default_factory=list)
    env_removed: list[str] = field(default_factory=list)
    services_added: list[str] = field(default_factory=list)
    services_removed: list[str] = field(default_factory=list)
    findings_new: list[dict] = field(default_factory=list)
    findings_fixed: list[dict] = field(default_factory=list)

    runtime_change: tuple[str, str] | None = None
    handler_change: tuple[str | None, str | None] | None = None
    #: False when the caller asked for a summary only, so line counts are unknown
    #: rather than zero.
    diffs_computed: bool = True

    # -- summary helpers -------------------------------------------------
    def counts(self) -> dict[str, int]:
        """How many files fall into each kind of change.

        Always contains all five keys, zeros included, so a caller can index it
        without guarding.
        """
        counts = {"added": 0, "removed": 0, "modified": 0, "renamed": 0, "mode-changed": 0}
        for change in self.files:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return counts

    @property
    def total_added_lines(self) -> int:
        """Lines added across every file diff. Zero when diffs were not computed."""
        return sum(c.added_lines for c in self.files)

    @property
    def total_removed_lines(self) -> int:
        """Lines removed across every file diff. Zero when diffs were not computed."""
        return sum(c.removed_lines for c in self.files)

    @property
    def lines_uncounted(self) -> int:
        """How many changed files were measured in something other than lines.

        A whitespace-only change and a minified bundle both contribute nothing to
        :attr:`total_added_lines`, so a diff made entirely of those two reports
        ``+0/-0`` — which reads as "nothing changed" and is the one thing that is
        not true. Renderers say this count beside the tally so the zero means
        what it says.
        """
        return sum(1 for c in self.files if c.whitespace_only or c.long_lines)

    @property
    def is_empty(self) -> bool:
        """True when nothing at all changed between the two versions.

        Checks every layer, not just the files: a version whose only change is a
        dependency bump or a new environment variable is not empty. Vendored churn
        counts too, even when it is hidden from the file list.
        """
        return not (
            self.files or self.deps or self.env_added or self.env_removed
            or self.services_added or self.services_removed or self.runtime_change
            or self.handler_change or self.vendor_files_changed
        )

    def headline(self) -> str:
        """How much changed, in one phrase: ``2 modified, 1 added, 52 vendored``.

        Only non-zero kinds appear, so the line stays short. Falls back to
        ``no file changes`` — which is a real outcome, not an error, when a
        dependency or an env var moved but no first-party file did.
        """
        counts = self.counts()
        parts = [f"{counts[k]} {k}" for k in ("added", "removed", "modified", "renamed") if counts[k]]
        if self.vendor_files_changed:
            parts.append(f"{self.vendor_files_changed} vendored")
        if not parts:
            return "no file changes"
        return ", ".join(parts)

    def impact_line(self) -> str:
        """What the file counts cannot say: lines moved, and what a deploy now needs.

        ``headline`` answers "how much changed"; this answers "what does that
        mean for me". Empty when there is nothing of the sort to report, so
        callers can leave the line out rather than print a blank one.
        """
        parts: list[str] = []
        if self.diffs_computed and (self.total_added_lines or self.total_removed_lines):
            parts.append(f"+{self.total_added_lines}/-{self.total_removed_lines} lines")
        arrivals = [
            f"{count} {noun}{'s' if count != 1 else ''}"
            for count, noun in (
                (len(self.env_added), "env var"),
                (len(self.services_added), "AWS service"),
                (len(self.findings_new), "secret"),
            )
            if count
        ]
        if arrivals:
            parts.append("new: " + ", ".join(arrivals))
        return " · ".join(parts)

    def summary_line(self) -> str:
        """Both halves at once, for somewhere with room for one line and no more."""
        impact = self.impact_line()
        return f"{self.headline()} · {impact}" if impact else self.headline()

    def file_rows(self) -> list[FileChange | MoveGroup]:
        """The file list as it should be *shown*: each collapsed move once, in place.

        ``files`` keeps every rename as its own change, because the per-file
        patches and the machine-readable diff both need them. This is the
        reading order instead — the first member of a collapsed move stands in
        for the whole group and the rest drop out, everything else passing
        through untouched. Renderers walk this; nothing else should.
        """
        if not self.moves:
            return list(self.files)
        owner = {path: group for group in self.moves for path in group.paths}
        rows: list[FileChange | MoveGroup] = []
        seen: set[tuple[str, str]] = set()
        for change in self.files:
            group = owner.get(change.path) if change.kind == "renamed" else None
            if group is None:
                rows.append(change)
            elif (key := (group.old_dir, group.new_dir)) not in seen:
                seen.add(key)
                rows.append(group)
        return rows

    def as_dict(self) -> dict:
        """The whole diff as plain JSON-ready data, for ``--json`` output."""
        return {
            "function": self.function_name,
            "from": self.a_seq,
            "to": self.b_seq,
            "counts": self.counts(),
            "lines": {"added": self.total_added_lines, "removed": self.total_removed_lines,
                      "uncounted_files": self.lines_uncounted},
            "vendor_files_changed": self.vendor_files_changed,
            "renames_unexamined": self.renames_unexamined,
            "files": [c.as_dict() for c in self.files],
            "moves": [m.as_dict() for m in self.moves],
            "dependencies": [d.as_dict() for d in self.deps],
            "env_vars": {"added": self.env_added, "removed": self.env_removed},
            "services": {"added": self.services_added, "removed": self.services_removed},
            "runtime_change": self.runtime_change,
            "handler_change": self.handler_change,
            "findings_new": self.findings_new,
            "findings_fixed": self.findings_fixed,
        }


def _index(records: Iterable[FileRecord]) -> dict[str, FileRecord]:
    """Turn file records into a ``{path: record}`` lookup."""
    return {r.path: r for r in records}

# Pairing every added file against every removed one is quadratic, so the work has
# to be bounded - but the bound belongs on the *product*, because that is what it
# costs. 200 files added against 3 removed is 600 comparisons, not 200 of
# anything, and a per-side cap would refuse it to save seven milliseconds.
# Roughly 30us per fully-scored pair, so the default budget is a few seconds at
# its very worst, and only when no cheap filter rejects anything.
_RENAME_THRESHOLD = 0.55
#: A same-name pair this alike is the same file, and needs no second opinion.
_RENAME_CONFIDENT = 0.8

#: Languages a file can be ported *into* while staying the same file, keyed to
#: the family they share. The rename pass rejects a mismatched pair as a cheap
#: way to skip files that cannot be two halves of one move, and a migration is
#: precisely the pair that fails the letter of that test while satisfying its
#: intent: ``handler.js`` -> ``handler.ts`` is one file gaining type
#: annotations, not one deleted and an unrelated one written.
#:
#: Most near misses never reach here — :data:`~lambda_watcher.utils.LANG_BY_EXT`
#: already folds ``.mjs``, ``.cjs`` and ``.jsx`` into ``javascript``, ``.pyi``
#: into ``python`` and ``.yml`` into ``yaml``, so those arrive as equal labels.
#: Ports that rewrite every line on the way across (Java to Kotlin, JSON to
#: YAML) are left out deliberately: they cannot reach ``_RENAME_THRESHOLD``
#: anyway, so admitting them would only spend budget to fail.
_LANG_FAMILY: dict[str, str] = {
    "javascript": "ecmascript",
    "typescript": "ecmascript",
}


def _lang_compatible(old_lang: str, new_lang: str) -> bool:
    """True when two language labels are near enough to be one file, moved.

    ``python`` and ``python`` trivially; ``javascript`` and ``typescript``
    because that pair is a migration rather than a rewrite. Anything else is a
    mismatch worth rejecting before either file is read.
    """
    if old_lang == new_lang:
        return True
    family = _LANG_FAMILY.get(old_lang)
    return family is not None and family == _LANG_FAMILY.get(new_lang)


def _pair_key(path: str, lang: str) -> tuple[str, str]:
    """The part of a name that survives a move, for anchoring candidate pairs.

    ``handlers/util.py`` -> ``("util.py", "")``: the filename, because a moved
    file usually keeps it. For a language that migrates, the extension is the
    part that changed, so the key drops it and names the family instead —
    ``src/handler.js`` and ``lib/handler.ts`` both key as ``("handler",
    "ecmascript")``. That is what lets a whole ``.js`` -> ``.ts`` migration be
    found by the cheap first pass of :func:`_similarity_renames` rather than by
    its quadratic second one, and it is the same equality that earns the
    corroboration bonus there.
    """
    name = path.rsplit("/", 1)[-1]
    family = _LANG_FAMILY.get(lang)
    if family is None:
        return name, ""
    return name.rpartition(".")[0] or name, family


def _greedy_assign(pairs: list[tuple[float, str, str]]) -> dict[str, str]:
    """Take the strongest pairings first, one use per file. Returns new -> old."""
    used_old: set[str] = set()
    used_new: set[str] = set()
    matches: dict[str, str] = {}
    for _ratio, new_path, old_path in sorted(pairs, reverse=True):
        if new_path in used_new or old_path in used_old:
            continue
        matches[new_path] = old_path
        used_new.add(new_path)
        used_old.add(old_path)
    return matches


def _similarity_renames(
    added: list[str],
    removed: list[str],
    old: dict[str, FileRecord],
    new: dict[str, FileRecord],
    old_root: Path,
    new_root: Path,
    cfg: DiffConfig,
) -> tuple[dict[str, str], int]:
    """Pair up added/removed files that look like the same file, moved and edited.

    Returns the matches, and how many added files went unexamined when the pair
    budget ran out. A partial rename map beats none - a restructure reported as
    unrelated adds and deletes is the hardest kind of diff to read by hand - but
    the reader has to be told when the map is partial.
    """
    if not added or not removed:
        return {}, 0

    # First-party files go to the front of both queues, because the budget below
    # is spent in order and vendored churn is what exhausts it: a dependency bump
    # moves thousands of files under site-packages, and pairing those off must
    # not cost the reader the handful of their own files that moved. Sorting
    # rather than dropping keeps a vendored rename reportable when there is room
    # for it - with ignore_vendor on it is often the clearest statement of the
    # bump there is, naming both versions in the path.
    added = sorted(added, key=lambda p: new[p].is_vendor)
    removed = sorted(removed, key=lambda p: old[p].is_vendor)

    # Keyed once here rather than per pair, since the quadratic pass below asks
    # for the same key of the same file thousands of times over.
    new_keys = {path: _pair_key(path, new[path].lang) for path in added}
    old_keys = {path: _pair_key(path, old[path].lang) for path in removed}

    max_bytes = cfg.max_diff_file_kb * 1024
    cache: dict[tuple[str, str], list[str] | None] = {}

    def lines_of(root: Path, record: FileRecord, side: str) -> list[str] | None:
        """The file's lines, or None when it is binary or too large to compare.

        Cached per side, so a file weighed against several candidates is read
        once rather than once per pairing — which is what makes the budget below
        a count of comparisons rather than of disk reads.
        """
        key = (side, record.path)
        if key not in cache:
            if not record.is_text or record.size > max_bytes:
                cache[key] = None
            else:
                text = read_text(root / record.path)
                cache[key] = text.splitlines() if text is not None else None
        return cache[key]

    pairs: list[tuple[float, str, str]] = []
    budget = max(0, cfg.max_rename_pairs)
    unswept: list[str] = []

    def evaluate(new_path: str, old_path: str) -> bool:
        """Score one pair, if there is budget left. False once there is not.

        The cheap rejections - language, size ratio, unreadable file - cost
        nothing worth counting, so only a pair that reaches the matcher spends
        any of the budget.
        """
        nonlocal budget
        if budget <= 0:
            return False
        new_record, old_record = new[new_path], old[old_path]
        if not _lang_compatible(old_record.lang, new_record.lang):
            return True
        # Sizes an order of magnitude apart are not the same file.
        if not (0.2 <= (new_record.size + 1) / (old_record.size + 1) <= 5):
            return True
        new_lines = lines_of(new_root, new_record, "new")
        if not new_lines:
            return True
        old_lines = lines_of(old_root, old_record, "old")
        if not old_lines:
            return True
        budget -= 1
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        if matcher.quick_ratio() < _RENAME_THRESHOLD:
            return True
        ratio = matcher.ratio()
        if ratio < _RENAME_THRESHOLD:
            return True
        # A name that survived the move is strong corroboration for a plain
        # move, or for a migration that changed only the extension.
        if old_keys[old_path] == new_keys[new_path]:
            ratio = min(1.0, ratio + 0.15)
        pairs.append((ratio, new_path, old_path))
        return True

    removed_by_key: dict[tuple[str, str], list[str]] = {}
    for old_path in removed:
        removed_by_key.setdefault(old_keys[old_path], []).append(old_path)

    # Pass 1: candidates that kept their name - the whole filename for a plain
    # move, everything but the extension for a migration, which is the
    # difference _pair_key normalises away. So the restructure that used to be
    # hopeless - one directory renamed under every file in it, or a codebase
    # ported from .js to .ts - now costs one comparison per file instead of one
    # per pair, and lands whole at any size. Running it first also means the
    # budget is spent on the genuinely ambiguous candidates rather than on the
    # ones whose name already answered the question.
    for index, new_path in enumerate(added):
        for old_path in removed_by_key.get(new_keys[new_path], ()):
            if not evaluate(new_path, old_path):
                unswept = added[index:]
                break
        if unswept:
            break

    # A file that kept its name and still reads as the same file is settled -
    # there is nothing a second opinion could add. Dropping those from pass 2 is
    # what turns a moved directory from one comparison per *pair* into one per
    # file, so the restructures that used to be refused now land whole and cheap.
    settled = _greedy_assign([p for p in pairs if p[0] >= _RENAME_CONFIDENT])
    settled_old = set(settled.values())
    rest_added = [p for p in added if p not in settled]
    rest_removed = [p for p in removed if p not in settled_old]

    # Pass 2: the rest of the cross-product, minus the pairs pass 1 already
    # scored. This is the quadratic half, and when the budget runs out it stops
    # here with what it has rather than discarding pass 1 along with it.
    if not unswept:
        for index, new_path in enumerate(rest_added):
            new_key = new_keys[new_path]
            for old_path in rest_removed:
                if old_keys[old_path] == new_key:
                    continue  # scored in pass 1
                if not evaluate(new_path, old_path):
                    unswept = rest_added[index:]
                    break
            if unswept:
                break

    matches = _greedy_assign(pairs)
    # A file the budget cut short may still have been paired by pass 1, so only
    # the ones that ended with no match at all are genuinely unanswered.
    return matches, sum(1 for path in unswept if path not in matches)


def _dirname(path: str) -> str:
    """The directory a path sits in: ``a/b/c.py`` -> ``a/b``, ``c.py`` -> ``""``."""
    return path.rpartition("/")[0]


def _move_groups(
    changes: list[FileChange],
    old: dict[str, FileRecord],
    min_files: int,
) -> list[MoveGroup]:
    """Fold the renames that are really one directory move into a group each.

    A member is a rename that kept its filename and changed only its directory,
    which is what a moved file looks like: ``handlers/mod00.py`` ->
    ``lambda/handlers/mod00.py``. A rename that also changed the filename is a
    decision about that one file and is left to speak for itself.

    Grouping is by the ``(old directory, new directory)`` pair rather than by
    either half, so two packages bumped in the same release stay two groups
    instead of merging into one claim neither of them supports. Groups smaller
    than ``min_files`` are dropped: three near-identical rows are where the eye
    starts having to diff them against each other, and below that folding costs
    the reader more than it saves.

    Detection is not repeated here — this reads the renames
    :func:`_similarity_renames` and the hash pass already found, which is why it
    is cheap enough to run unconditionally. See :meth:`VersionDiff.file_rows`
    for how the result is swapped in for its members at render time.
    """
    if min_files <= 0:
        return []

    groups: dict[tuple[str, str], MoveGroup] = {}
    for change in changes:
        if change.kind != "renamed" or not change.old_path:
            continue
        if change.path.rpartition("/")[2] != change.old_path.rpartition("/")[2]:
            continue
        old_dir, new_dir = _dirname(change.old_path), _dirname(change.path)
        if old_dir == new_dir:
            continue
        groups.setdefault((old_dir, new_dir), MoveGroup(old_dir, new_dir)).members.append(change)

    kept = [g for g in groups.values() if len(g.members) >= min_files]
    # How full each source directory was. Counted only for the groups that
    # survived, and in one pass over the old index rather than one pass each,
    # because a version can hold several thousand paths.
    if kept:
        held = dict.fromkeys((g.old_dir for g in kept), 0)
        for path in old:
            parent = _dirname(path)
            if parent in held:
                held[parent] += 1
        for group in kept:
            group.total_in_old_dir = held[group.old_dir]
    return kept


def _whitespace_key(text: str) -> tuple[str, ...]:
    """What a file says once indentation and spacing stop counting.

    Every run of whitespace inside a line becomes one space, the ends are
    trimmed, and blank lines drop out entirely — so a tab-to-space retab, a
    reindent, a CRLF conversion and a stripped trailing space all leave the key
    untouched, while ``foo bar`` -> ``foobar`` changes it. That last case is the
    reason runs collapse rather than vanish: git's ``-w`` would call it
    whitespace, and it is a rename.

    Two files with equal keys and unequal text differ in whitespace and nothing
    else, which is the whole test :func:`_fill_diff` runs.
    """
    return tuple(" ".join(line.split()) for line in text.splitlines() if line.strip())


def _fill_diff(change: FileChange, old_root: Path, new_root: Path, cfg: DiffConfig) -> None:
    """Work out what changed inside one file and write it onto the change.

    Three routes out, because a unified diff is only the right answer for a file
    that has ordinary lines:

    ``whitespace only``
        Normalising whitespace makes the two sides identical. The hunk would be
        every touched line printed twice, red then green, to report a retab —
        so it is not computed and the label carries the change. ``black`` over a
        package is the case that matters: without this, every reformatted file
        reads as a total rewrite. Turn it off with
        ``diff.collapse_whitespace_only`` or ``lw diff --whitespace``.

    ``long_lines``
        Both sides exist and their lines average more than
        ``diff.long_line_mean_chars``, so line granularity has nothing to work
        with — a minified bundle is one 8,000-character line, and its diff is
        the file quoted twice to show a changed digit.
        :func:`~.intraline.long_line_edits` quotes the changed runs instead. A
        bundle that was merely *added* takes the ordinary route: there is no
        second side to find runs against, and one long ``+`` line is at least
        the file.

    the hunk
        Everything else, from :func:`difflib.unified_diff`.

    Before any of that, a file can be refused outright — binary, over
    ``max_diff_file_kb``, or not decodable — and ``skipped_reason`` says which.
    Line counts stay at zero on every route but the last: the counts describe a
    hunk, and where there is no hunk ``+3/-3`` is the noise the route exists to
    remove.
    """
    old, new = change.old, change.new
    max_bytes = cfg.max_diff_file_kb * 1024
    for record in (old, new):
        if record is None:
            continue
        if not record.is_text:
            change.skipped_reason = "binary"
            change.binary = True
            return
        if record.size > max_bytes:
            change.skipped_reason = f"file larger than {cfg.max_diff_file_kb} KB"
            return

    old_text = read_text(old_root / old.path) if old else ""
    new_text = read_text(new_root / new.path) if new else ""
    if old_text is None or new_text is None:
        change.skipped_reason = "not decodable as text"
        return

    if old_text and new_text and old_text != new_text:
        if cfg.collapse_whitespace_only and _whitespace_key(old_text) == _whitespace_key(new_text):
            change.whitespace_only = True
            change.skipped_reason = "whitespace only"
            return

    both_sides = old is not None and new is not None
    if both_sides and max(mean_line_length(old_text),
                          mean_line_length(new_text)) > cfg.long_line_mean_chars:
        change.long_lines = True
        change.word_edits, change.skipped_reason = long_line_edits(old_text, new_text)
        return

    diff = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{old.path}" if old else "/dev/null",
            tofile=f"b/{new.path}" if new else "/dev/null",
            n=cfg.context_lines,
        )
    )
    change.added_lines = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    change.removed_lines = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))

    if len(diff) > cfg.max_diff_lines:
        diff = diff[: cfg.max_diff_lines]
        change.truncated = True
    change.diff_lines = [line.rstrip("\n") for line in diff]


def _diff_deps(old_rows: list[Any], new_rows: list[Any]) -> list[DepChange]:
    """Compare two dependency lists into added, changed and removed entries.

    Dependencies are matched on ``(manager, name, is_declared)``, so a package
    that is both declared in ``requirements.txt`` and installed in
    ``site-packages`` is tracked as two separate facts — which is the point, as
    the declared range and the installed version can disagree.

    They are then collapsed for display: when both rows tell the same story, the
    installed one is kept, because that is the version that actually ran.
    Results are sorted added, then changed, then removed.
    """
    def key(row: Any) -> tuple[str, str, int]:
        """Match dependencies across versions, ignoring the version itself."""
        return (row["manager"], row["name"].lower(), int(row["is_declared"]))

    old_map = {key(r): r for r in old_rows}
    new_map = {key(r): r for r in new_rows}
    changes: list[DepChange] = []

    for k, row in new_map.items():
        previous = old_map.get(k)
        if previous is None:
            changes.append(
                DepChange("added", row["manager"], row["name"], None, row["version"], bool(k[2]))
            )
        elif (previous["version"] or "") != (row["version"] or ""):
            changes.append(
                DepChange("changed", row["manager"], row["name"], previous["version"],
                          row["version"], bool(k[2]))
            )
    for k, row in old_map.items():
        if k not in new_map:
            changes.append(
                DepChange("removed", row["manager"], row["name"], row["version"], None, bool(k[2]))
            )

    # A package usually appears twice - once declared in a manifest, once
    # installed in the zip. When both tell the same story, show one row.
    collapsed: list[DepChange] = []
    by_identity: dict[tuple[str, str, str, str | None, str | None], list[DepChange]] = {}
    for change in changes:
        # Not `key`: `key()` is the sort helper defined above, in this same scope.
        identity = (change.manager, change.name.lower(), change.kind,
                    change.old_version, change.new_version)
        by_identity.setdefault(identity, []).append(change)
    for group in by_identity.values():
        if len(group) > 1:
            # Keep the installed row: it is what actually shipped.
            installed = next((c for c in group if not c.is_declared), group[0])
            collapsed.append(installed)
        else:
            collapsed.append(group[0])

    order = {"added": 0, "changed": 1, "removed": 2}
    collapsed.sort(key=lambda c: (order[c.kind], c.manager, c.name.lower()))
    return collapsed


def _finding_key(row: Any) -> tuple:
    """Identity of a finding across versions: its kind, file and redacted value.

    Deliberately excludes the line number, so inserting a line above a hardcoded
    key does not report the old secret as fixed and an identical new one as
    found.
    """
    return (row["kind"], row["path"], row["detail"])


def compare_versions(
    function_name: str,
    a_seq: int,
    b_seq: int,
    a_files: list[Any],
    b_files: list[Any],
    a_root: Path,
    b_root: Path,
    cfg: DiffConfig,
    a_deps: list[Any] | None = None,
    b_deps: list[Any] | None = None,
    a_env: list[Any] | None = None,
    b_env: list[Any] | None = None,
    a_services: list[Any] | None = None,
    b_services: list[Any] | None = None,
    a_findings: list[Any] | None = None,
    b_findings: list[Any] | None = None,
    a_meta: dict | None = None,
    b_meta: dict | None = None,
    include_vendor: bool | None = None,
    compute_diffs: bool = True,
) -> VersionDiff:
    """Compare two indexed versions, reading file contents from disk."""
    show_vendor = (not cfg.ignore_vendor) if include_vendor is None else include_vendor

    old = _index(FileRecord.from_row(r) for r in a_files)
    new = _index(FileRecord.from_row(r) for r in b_files)

    result = VersionDiff(function_name, a_seq, b_seq, a_meta or {}, b_meta or {}, a_root, b_root)
    result.diffs_computed = compute_diffs

    added_paths = [p for p in new if p not in old]
    removed_paths = [p for p in old if p not in new]
    common_paths = [p for p in new if p in old]

    # Rename detection: identical content under a different path.
    removed_by_hash: dict[str, list[str]] = {}
    for path in removed_paths:
        removed_by_hash.setdefault(old[path].sha256, []).append(path)

    renames: dict[str, str] = {}  # new path -> old path
    for path in added_paths:
        bucket = removed_by_hash.get(new[path].sha256)
        if bucket:
            renames[path] = bucket.pop(0)

    # Files that moved *and* changed are the interesting case: a rename plus an
    # edit reads far better than an unrelated add and delete.
    leftover_added = [p for p in added_paths if p not in renames]
    leftover_removed = [p for p in removed_paths if p not in set(renames.values())]
    similar, result.renames_unexamined = _similarity_renames(
        leftover_added, leftover_removed, old, new, a_root, b_root, cfg
    )
    renames.update(similar)
    renamed_old = set(renames.values())

    changes: list[FileChange] = []

    for path in sorted(added_paths):
        record = new[path]
        if path in renames:
            changes.append(
                FileChange("renamed", path, renames[path], old[renames[path]], record)
            )
            continue
        if record.is_vendor and not show_vendor:
            result.vendor_files_changed += 1
            continue
        changes.append(FileChange("added", path, None, None, record))

    for path in sorted(removed_paths):
        if path in renamed_old:
            continue
        record = old[path]
        if record.is_vendor and not show_vendor:
            result.vendor_files_changed += 1
            continue
        changes.append(FileChange("removed", path, None, record, None))

    for path in sorted(common_paths):
        before, after = old[path], new[path]
        if before.sha256 == after.sha256:
            if before.mode != after.mode:
                changes.append(FileChange("mode-changed", path, None, before, after))
            else:
                result.unchanged_files += 1
            continue
        if after.is_vendor and not show_vendor:
            result.vendor_files_changed += 1
            continue
        changes.append(FileChange("modified", path, None, before, after))

    # Fill in the line diffs.
    if compute_diffs:
        for change in changes:
            if change.kind == "mode-changed":
                continue
            if matches_any(change.path, cfg.ignore_globs):
                change.skipped_reason = "ignored by config"
                continue
            _fill_diff(change, a_root, b_root, cfg)

    # First-party code first, then vendored; alphabetical within each group.
    kind_order = {"modified": 0, "added": 1, "renamed": 2, "removed": 3, "mode-changed": 4}
    changes.sort(key=lambda c: (c.is_vendor, kind_order.get(c.kind, 9), c.path))
    result.files = changes
    result.moves = _move_groups(changes, old, cfg.min_moved_files)

    if a_deps is not None and b_deps is not None:
        result.deps = _diff_deps(a_deps, b_deps)

    if a_env is not None and b_env is not None:
        old_env = {r["name"] for r in a_env}
        new_env = {r["name"] for r in b_env}
        result.env_added = sorted(new_env - old_env)
        result.env_removed = sorted(old_env - new_env)

    if a_services is not None and b_services is not None:
        old_services = {r["service"] for r in a_services}
        new_services = {r["service"] for r in b_services}
        result.services_added = sorted(new_services - old_services)
        result.services_removed = sorted(old_services - new_services)

    if a_findings is not None and b_findings is not None:
        old_keys = {_finding_key(r) for r in a_findings}
        new_keys = {_finding_key(r) for r in b_findings}
        result.findings_new = [dict(r) for r in b_findings if _finding_key(r) not in old_keys]
        result.findings_fixed = [dict(r) for r in a_findings if _finding_key(r) not in new_keys]

    if a_meta and b_meta:
        if a_meta.get("runtime") != b_meta.get("runtime"):
            result.runtime_change = (a_meta.get("runtime") or "?", b_meta.get("runtime") or "?")
        if a_meta.get("handler") != b_meta.get("handler"):
            result.handler_change = (a_meta.get("handler"), b_meta.get("handler"))

    return result
