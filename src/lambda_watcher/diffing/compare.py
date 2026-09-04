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
        return cls(
            path=row["path"], size=int(row["size"]), sha256=row["sha256"],
            is_text=bool(row["is_text"]), is_vendor=bool(row["is_vendor"]),
            lang=row["lang"] or "text", lines=int(row["lines"]), mode=int(row["mode"] or 0o644),
        )


@dataclass
class FileChange:
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

    @property
    def is_vendor(self) -> bool:
        record = self.new or self.old
        return bool(record and record.is_vendor)

    @property
    def size_delta(self) -> int:
        return (self.new.size if self.new else 0) - (self.old.size if self.old else 0)

    @property
    def lang(self) -> str:
        record = self.new or self.old
        return record.lang if record else "text"

    def as_dict(self) -> dict:
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
        }


@dataclass
class DepChange:
    kind: str  # added | removed | changed
    manager: str
    name: str
    old_version: str | None = None
    new_version: str | None = None
    is_declared: bool = False

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "manager": self.manager, "name": self.name,
            "old_version": self.old_version, "new_version": self.new_version,
            "is_declared": self.is_declared,
        }


@dataclass
class VersionDiff:
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
        counts = {"added": 0, "removed": 0, "modified": 0, "renamed": 0, "mode-changed": 0}
        for change in self.files:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        return counts

    @property
    def total_added_lines(self) -> int:
        return sum(c.added_lines for c in self.files)

    @property
    def total_removed_lines(self) -> int:
        return sum(c.removed_lines for c in self.files)

    @property
    def is_empty(self) -> bool:
        return not (
            self.files or self.deps or self.env_added or self.env_removed
            or self.services_added or self.services_removed or self.runtime_change
            or self.handler_change or self.vendor_files_changed
        )

    def headline(self) -> str:
        counts = self.counts()
        parts = [f"{counts[k]} {k}" for k in ("added", "removed", "modified", "renamed") if counts[k]]
        if self.vendor_files_changed:
            parts.append(f"{self.vendor_files_changed} vendored")
        if not parts:
            return "no file changes"
        return ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "function": self.function_name,
            "from": self.a_seq,
            "to": self.b_seq,
            "counts": self.counts(),
            "lines": {"added": self.total_added_lines, "removed": self.total_removed_lines},
            "vendor_files_changed": self.vendor_files_changed,
            "files": [c.as_dict() for c in self.files],
            "dependencies": [d.as_dict() for d in self.deps],
            "env_vars": {"added": self.env_added, "removed": self.env_removed},
            "services": {"added": self.services_added, "removed": self.services_removed},
            "runtime_change": self.runtime_change,
            "handler_change": self.handler_change,
            "findings_new": self.findings_new,
            "findings_fixed": self.findings_fixed,
        }


def _index(records: Iterable[FileRecord]) -> dict[str, FileRecord]:
    return {r.path: r for r in records}

# Pairing every candidate against every other is quadratic, so cap the work.
_MAX_RENAME_CANDIDATES = 150
_RENAME_THRESHOLD = 0.55


def _similarity_renames(
    added: list[str],
    removed: list[str],
    old: dict[str, FileRecord],
    new: dict[str, FileRecord],
    old_root: Path,
    new_root: Path,
    cfg: DiffConfig,
) -> dict[str, str]:
    """Pair up added/removed files that look like the same file, moved and edited."""
    if not added or not removed:
        return {}
    if len(added) > _MAX_RENAME_CANDIDATES or len(removed) > _MAX_RENAME_CANDIDATES:
        return {}

    max_bytes = cfg.max_diff_file_kb * 1024
    cache: dict[tuple[str, str], list[str] | None] = {}

    def lines_of(root: Path, record: FileRecord, side: str) -> list[str] | None:
        key = (side, record.path)
        if key not in cache:
            if not record.is_text or record.size > max_bytes:
                cache[key] = None
            else:
                text = read_text(root / record.path)
                cache[key] = text.splitlines() if text is not None else None
        return cache[key]

    pairs: list[tuple[float, str, str]] = []
    for new_path in added:
        new_record = new[new_path]
        new_lines = lines_of(new_root, new_record, "new")
        if not new_lines:
            continue
        new_base = new_path.rsplit("/", 1)[-1]
        for old_path in removed:
            old_record = old[old_path]
            if old_record.lang != new_record.lang:
                continue
            # Sizes an order of magnitude apart are not the same file.
            if not (0.2 <= (new_record.size + 1) / (old_record.size + 1) <= 5):
                continue
            old_lines = lines_of(old_root, old_record, "old")
            if not old_lines:
                continue
            matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
            if matcher.quick_ratio() < _RENAME_THRESHOLD:
                continue
            ratio = matcher.ratio()
            if ratio < _RENAME_THRESHOLD:
                continue
            # A matching filename is strong corroboration for a plain move.
            if old_path.rsplit("/", 1)[-1] == new_base:
                ratio = min(1.0, ratio + 0.15)
            pairs.append((ratio, new_path, old_path))

    # Greedily take the strongest pairings first, one use per file.
    pairs.sort(reverse=True)
    used_old: set[str] = set()
    used_new: set[str] = set()
    matches: dict[str, str] = {}
    for _ratio, new_path, old_path in pairs:
        if new_path in used_new or old_path in used_old:
            continue
        matches[new_path] = old_path
        used_new.add(new_path)
        used_old.add(old_path)
    return matches



def _unified_diff(
    old_root: Path, new_root: Path, old: FileRecord | None, new: FileRecord | None, cfg: DiffConfig
) -> tuple[list[str], int, int, bool, str | None]:
    """Line diff for one file. Returns (lines, added, removed, truncated, skip_reason)."""
    max_bytes = cfg.max_diff_file_kb * 1024
    for record in (old, new):
        if record is None:
            continue
        if not record.is_text:
            return [], 0, 0, False, "binary"
        if record.size > max_bytes:
            return [], 0, 0, False, f"file larger than {cfg.max_diff_file_kb} KB"

    old_text = read_text(old_root / old.path) if old else ""
    new_text = read_text(new_root / new.path) if new else ""
    if old_text is None or new_text is None:
        return [], 0, 0, False, "not decodable as text"

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{old.path}" if old else "/dev/null",
            tofile=f"b/{new.path}" if new else "/dev/null",
            n=cfg.context_lines,
        )
    )
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    truncated = False
    if len(diff) > cfg.max_diff_lines:
        diff = diff[: cfg.max_diff_lines]
        truncated = True
    return [line.rstrip("\n") for line in diff], added, removed, truncated, None


def _diff_deps(old_rows: list[Any], new_rows: list[Any]) -> list[DepChange]:
    def key(row: Any) -> tuple[str, str, int]:
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
        key = (change.manager, change.name.lower(), change.kind, change.old_version, change.new_version)
        by_identity.setdefault(key, []).append(change)
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
    renames.update(
        _similarity_renames(leftover_added, leftover_removed, old, new, a_root, b_root, cfg)
    )
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
            lines, plus, minus, truncated, reason = _unified_diff(
                a_root, b_root, change.old, change.new, cfg
            )
            change.diff_lines = lines
            change.added_lines = plus
            change.removed_lines = minus
            change.truncated = truncated
            change.skipped_reason = reason
            change.binary = reason == "binary"

    # First-party code first, then vendored; alphabetical within each group.
    kind_order = {"modified": 0, "added": 1, "renamed": 2, "removed": 3, "mode-changed": 4}
    changes.sort(key=lambda c: (c.is_vendor, kind_order.get(c.kind, 9), c.path))
    result.files = changes

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
