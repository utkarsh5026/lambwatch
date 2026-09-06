"""Walk an extracted package and record every file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..utils import count_lines, is_probably_text, language_for, matches_any, sha256_file, tree_hash


@dataclass
class FileEntry:
    """One file inside an extracted package, hashed and classified.

    ``path`` is always posix-style and relative to the package root, so the same
    tree hashes identically on Windows and Linux. ``is_vendor`` is the flag most
    of the tool keys off: it separates the handful of files somebody wrote from
    the thousands that came out of ``pip install``.
    """

    path: str          # posix relative path
    size: int
    sha256: str
    mode: int
    is_text: bool
    is_vendor: bool
    lang: str
    lines: int

    def as_dict(self) -> dict:
        """This entry as plain JSON-ready data, for the manifest."""
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mode": self.mode,
            "is_text": self.is_text,
            "is_vendor": self.is_vendor,
            "lang": self.lang,
            "lines": self.lines,
        }


@dataclass
class Inventory:
    """Every file in one extracted package, plus the totals worth caching.

    ``tree_hash`` is the identity of the whole tree and what decides whether an
    ingest has found a new version. The size and line counts are accumulated
    during the walk rather than recomputed, since they are wanted on every
    summary screen.
    """

    files: list[FileEntry] = field(default_factory=list)
    tree_hash: str = ""
    total_size: int = 0
    code_size: int = 0
    code_lines: int = 0

    @property
    def file_count(self) -> int:
        """How many files the package contains, vendored ones included."""
        return len(self.files)

    @property
    def code_files(self) -> list[FileEntry]:
        """First-party files: everything that is not vendored."""
        return [f for f in self.files if not f.is_vendor]

    @property
    def code_file_count(self) -> int:
        """How many first-party files there are — the length of :attr:`code_files`."""
        return len(self.code_files)

    def by_path(self) -> dict[str, FileEntry]:
        """The files as a ``{path: entry}`` lookup.

        Diffing two versions means asking "was this path in the other one too?"
        thousands of times, which wants a dict rather than a scan of the list.
        """
        return {f.path: f for f in self.files}

    def language_breakdown(self) -> dict[str, int]:
        """Count first-party files per language, most common first.

        ``{"python": 12, "json": 3, "markdown": 1}``. Vendored files are excluded
        deliberately — counting them would report the language of the dependencies
        rather than of the function.
        """
        counts: dict[str, int] = {}
        for f in self.code_files:
            counts[f.lang] = counts.get(f.lang, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def build_inventory(
    root: Path,
    vendor_globs: list[str],
    max_scan_file_kb: int = 2048,
) -> Inventory:
    """Hash and classify every file under ``root``."""
    inventory = Inventory()
    hashes: list[tuple[str, str]] = []
    max_scan_bytes = max_scan_file_kb * 1024

    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink() and not path.exists():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue

        rel = path.relative_to(root).as_posix()
        digest = sha256_file(path)
        is_vendor = matches_any(rel, vendor_globs)
        lang = language_for(rel)
        size = stat_result.st_size

        # Only sniff and count lines for files we might actually diff.
        text = False
        lines = 0
        if size <= max_scan_bytes and lang != "binary":
            text = is_probably_text(path)
            if text:
                lines = count_lines(path)

        entry = FileEntry(
            path=rel,
            size=size,
            sha256=digest,
            mode=stat_result.st_mode & 0o777,
            is_text=text,
            is_vendor=is_vendor,
            lang=lang,
            lines=lines,
        )
        inventory.files.append(entry)
        hashes.append((rel, digest))
        inventory.total_size += size
        if not is_vendor:
            inventory.code_size += size
            inventory.code_lines += lines

    inventory.tree_hash = tree_hash(hashes)
    return inventory
