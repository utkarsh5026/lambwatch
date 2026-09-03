"""On-disk layout of the archive.

::

    <root>/
      config.yaml
      index.db
      logs/watcher.log
      reports/
      quarantine/            # archives that failed to extract
      functions/
        <slug>/
          versions/
            0001-a1b2c3d4/
              code/          # the extracted tree
              manifest.json  # full analysis for this version
              package.zip    # the original download (optional)
          git/               # optional mirror, one commit per version

The directories are the source of truth. ``index.db`` is a rebuildable index.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .utils import LOG, rmtree, short_hash


@dataclass
class VersionPaths:
    root: Path

    @property
    def code(self) -> Path:
        return self.root / "code"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def package(self) -> Path:
        return self.root / "package.zip"


class Store:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()

    # -- paths -----------------------------------------------------------
    def function_dir(self, slug: str) -> Path:
        return self.cfg.functions_dir / slug

    def versions_dir(self, slug: str) -> Path:
        return self.function_dir(slug) / "versions"

    def git_dir(self, slug: str) -> Path:
        return self.function_dir(slug) / "git"

    def version_dirname(self, seq: int, tree_hash: str) -> str:
        return f"{seq:04d}-{short_hash(tree_hash)}"

    def version_paths(self, slug: str, seq: int, tree_hash: str) -> VersionPaths:
        return VersionPaths(self.versions_dir(slug) / self.version_dirname(seq, tree_hash))

    def resolve_version_dir(self, stored_dir: str) -> Path:
        """Version dirs are stored relative to the root so the store can move."""
        path = Path(stored_dir)
        return path if path.is_absolute() else self.cfg.root / path

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.cfg.root))
        except ValueError:
            return str(path)

    # -- staging ---------------------------------------------------------
    def new_staging_dir(self, token: str) -> Path:
        """A scratch directory on the same filesystem as the final location."""
        staging = self.cfg.root / ".staging" / token
        rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def clear_staging(self) -> None:
        rmtree(self.cfg.root / ".staging")

    # -- manifests -------------------------------------------------------
    def write_manifest(self, paths: VersionPaths, manifest: dict[str, Any]) -> None:
        paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False),
            encoding="utf-8",
        )

    def read_manifest(self, version_dir: Path) -> dict[str, Any] | None:
        manifest = Path(version_dir) / "manifest.json"
        if not manifest.exists():
            return None
        try:
            return json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("could not read %s: %s", manifest, exc)
            return None

    # -- archive handling ------------------------------------------------
    def keep_original(self, zip_path: Path, paths: VersionPaths) -> Path | None:
        """Copy or move the download next to its extracted version."""
        mode = self.cfg.store.on_ingest
        if not self.cfg.store.keep_zip or mode == "leave":
            return None
        paths.root.mkdir(parents=True, exist_ok=True)
        target = paths.package
        try:
            if mode == "move":
                shutil.move(str(zip_path), str(target))
            else:
                shutil.copy2(str(zip_path), str(target))
        except OSError as exc:
            LOG.warning("could not %s %s into the store: %s", mode, zip_path.name, exc)
            return None
        return target

    def discard_original(self, zip_path: Path) -> None:
        """Used for duplicate downloads when ``on_ingest`` is ``move``."""
        if self.cfg.store.on_ingest != "move":
            return
        try:
            zip_path.unlink()
        except OSError as exc:
            LOG.warning("could not remove duplicate download %s: %s", zip_path, exc)

    def quarantine(self, zip_path: Path, reason: str) -> Path | None:
        """Park an archive we could not process, with a note about why."""
        target_dir = self.cfg.quarantine_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / zip_path.name
        counter = 1
        while target.exists():
            target = target_dir / f"{zip_path.stem}-{counter}{zip_path.suffix}"
            counter += 1
        try:
            shutil.copy2(str(zip_path), str(target))
            target.with_suffix(target.suffix + ".reason.txt").write_text(
                f"{zip_path}\n{reason}\n", encoding="utf-8"
            )
            return target
        except OSError as exc:
            LOG.warning("could not quarantine %s: %s", zip_path, exc)
            return None

    # -- retention -------------------------------------------------------
    def prune(self, slug: str, keep: int) -> list[Path]:
        """Delete the oldest version directories beyond ``keep``."""
        if keep <= 0:
            return []
        versions = sorted(
            (p for p in self.versions_dir(slug).glob("*") if p.is_dir()), key=lambda p: p.name
        )
        removed: list[Path] = []
        for path in versions[:-keep] if len(versions) > keep else []:
            rmtree(path)
            removed.append(path)
        return removed
