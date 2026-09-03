"""Safe extraction of Lambda deployment zips.

Deployment packages are downloaded from a trusted account, but they are still
archives from the internet: this module refuses path traversal, absolute paths,
symlinks pointing outside the tree, and archives that would explode on disk.
"""

from __future__ import annotations

import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .utils import LOG


class ExtractError(RuntimeError):
    """The archive could not be safely extracted."""


@dataclass
class ExtractResult:
    dest: Path
    file_count: int = 0
    dir_count: int = 0
    total_uncompressed: int = 0
    total_compressed: int = 0
    skipped: list[str] = field(default_factory=list)
    top_level: list[str] = field(default_factory=list)
    is_encrypted: bool = False

    @property
    def compression_ratio(self) -> float:
        if not self.total_compressed:
            return 0.0
        return self.total_uncompressed / self.total_compressed


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def safe_member_path(base: Path, name: str) -> Path | None:
    """Resolve an archive member to a path inside ``base``, or None if unsafe."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return None
    # Windows-created archives sometimes use backslashes as separators.
    normalised = name.replace("\\", "/")
    pure = PurePosixPath(normalised)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None
    # Drive letters (C:/...) are traversal on Windows.
    if len(pure.parts) and ":" in pure.parts[0]:
        return None
    target = base / Path(*pure.parts)
    if not _is_within(base, target):
        return None
    return target


def peek_top_level(zip_path: Path) -> list[str]:
    """Top-level entries of a zip without extracting it."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    tops: list[str] = []
    for name in names:
        head = name.replace("\\", "/").split("/")[0]
        if head and head not in tops and not head.startswith("__MACOSX"):
            tops.append(head)
    return tops


def extract_zip(
    zip_path: Path,
    dest: Path,
    max_uncompressed_bytes: int = 2 * 1024**3,
    max_files: int = 200_000,
) -> ExtractResult:
    """Extract ``zip_path`` into ``dest``, enforcing the safety limits."""
    dest.mkdir(parents=True, exist_ok=True)
    result = ExtractResult(dest=dest)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ExtractError(f"not a valid zip archive: {exc}") from exc
    except OSError as exc:
        raise ExtractError(f"could not open archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise ExtractError(f"archive has {len(infos)} entries (limit {max_files})")
        planned = sum(i.file_size for i in infos)
        if planned > max_uncompressed_bytes:
            raise ExtractError(
                f"archive expands to {planned / 1024 ** 2:.0f} MB "
                f"(limit {max_uncompressed_bytes / 1024 ** 2:.0f} MB)"
            )

        written = 0
        for info in infos:
            name = info.filename
            if name.startswith("__MACOSX/") or PurePosixPath(name).name == ".DS_Store":
                result.skipped.append(name)
                continue
            if info.flag_bits & 0x1:
                result.is_encrypted = True
                raise ExtractError("archive is password protected")

            target = safe_member_path(dest, name)
            if target is None:
                LOG.warning("skipping unsafe archive member %r in %s", name, zip_path.name)
                result.skipped.append(name)
                continue

            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                # Symlinks in a deployment package are almost always vendored
                # binaries; store the link text as a regular file so the tree
                # stays self-contained and cannot escape the store.
                target.parent.mkdir(parents=True, exist_ok=True)
                link_target = zf.read(info).decode("utf-8", "replace")
                target.write_text(link_target, encoding="utf-8")
                result.file_count += 1
                continue

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                result.dir_count += 1
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                remaining = max_uncompressed_bytes - written
                chunk_size = 1 << 20
                while True:
                    chunk = src.read(chunk_size)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ExtractError("archive exceeded the uncompressed size limit")
                    out.write(chunk)
                    written += len(chunk)

            result.file_count += 1
            result.total_uncompressed += info.file_size
            result.total_compressed += info.compress_size

            # Preserve the executable bit; Lambda custom runtimes rely on it.
            if mode and (mode & 0o111):
                try:
                    os.chmod(target, (target.stat().st_mode | 0o111) & 0o777)
                except OSError:
                    pass

    result.top_level = sorted(
        {p.name for p in dest.iterdir()} if dest.exists() else set()
    )
    return result
