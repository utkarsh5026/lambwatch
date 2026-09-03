"""Walk an extracted package and record every file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..utils import count_lines, is_probably_text, language_for, matches_any, sha256_file, tree_hash


@dataclass
class FileEntry:
    path: str          # posix relative path
    size: int
    sha256: str
    mode: int
    is_text: bool
    is_vendor: bool
    lang: str
    lines: int

    def as_dict(self) -> dict:
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
    files: list[FileEntry] = field(default_factory=list)
    tree_hash: str = ""
    total_size: int = 0
    code_size: int = 0
    code_lines: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def code_files(self) -> list[FileEntry]:
        """First-party files: everything that is not vendored."""
        return [f for f in self.files if not f.is_vendor]

    @property
    def code_file_count(self) -> int:
        return len(self.code_files)

    def by_path(self) -> dict[str, FileEntry]:
        return {f.path: f for f in self.files}

    def language_breakdown(self) -> dict[str, int]:
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
