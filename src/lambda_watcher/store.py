"""On-disk layout of the archive.

::

    <root>/
      config.yaml
      index.db
      logs/watcher.log
      reports/
      quarantine/            # archives that failed to extract
      repos/
        <slug>/              # optional git mirror, one commit per version
      functions/
        <slug>/
          versions/
            0001-a1b2c3d4/
              code/          # the extracted tree
              manifest.json  # full analysis for this version
              package.zip    # the original download (optional)

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


#: Where the mirror used to sit, inside the function directory. Both spellings
#: are migrated: ``git/`` shipped, ``repo/`` was a brief step on the way here.
LEGACY_REPO_DIRNAMES = ("git", "repo")


@dataclass
class VersionPaths:
    """The three paths that make up one archived version directory.

    A small holder so callers say ``paths.manifest`` rather than rebuilding
    ``root / "manifest.json"`` in a dozen places and eventually mistyping one.
    """

    root: Path

    @property
    def code(self) -> Path:
        """The extracted tree — the package's files as they shipped."""
        return self.root / "code"

    @property
    def manifest(self) -> Path:
        """``manifest.json``: the full analysis, and the source the index is rebuilt from."""
        return self.root / "manifest.json"

    @property
    def package(self) -> Path:
        """The original download, kept beside the extraction when ``store.keep_zip`` is on."""
        return self.root / "package.zip"


class Store:
    """The archive's directory layout, and the only thing that writes to it.

    Every path under the root is derived here rather than assembled by callers,
    so the layout drawn in the module docstring stays true. Nothing in this
    class touches the SQLite index: disk is the source of truth, and the index
    is rebuilt from what lands here.
    """

    def __init__(self, cfg: Config) -> None:
        """Bind to a config and create the archive directories if they are missing.

        Creating them up front is what lets every command work with no config file
        and no setup step.
        """
        self.cfg = cfg
        cfg.ensure_dirs()

    # -- paths -----------------------------------------------------------
    def function_dir(self, slug: str) -> Path:
        """``functions/<slug>/`` — one function's corner of the archive."""
        return self.cfg.functions_dir / slug

    def versions_dir(self, slug: str) -> Path:
        """``functions/<slug>/versions/`` — where this function's version directories live."""
        return self.function_dir(slug) / "versions"

    def repo_dir(self, slug: str) -> Path:
        """The function's git mirror: a real working tree you can open in an editor.

        It sits at ``repos/<slug>/`` rather than inside the function directory
        for one blunt reason — an editor names the window after the folder you
        opened, and every function opening as "repo" would be useless. Here the
        folder is called ``order-processor``, which is what you want to read in
        the sidebar. Generated per-function output already lives this way:
        ``reports/<slug>/`` is the same shape.

        Older archives kept it under the function directory. Those are moved
        here on first access, so a store from a previous version keeps working
        without a reindex.
        """
        repo = self.cfg.repos_dir / slug
        if repo.exists():
            return repo
        for name in LEGACY_REPO_DIRNAMES:
            legacy = self.function_dir(slug) / name
            if not (legacy / ".git").is_dir():
                continue
            try:
                repo.parent.mkdir(parents=True, exist_ok=True)
                legacy.rename(repo)
                LOG.info("moved the git mirror from %s to %s", legacy, repo)
            except OSError as exc:
                LOG.warning("could not move %s to %s: %s", legacy, repo, exc)
                return legacy
            break
        return repo

    def version_dirname(self, seq: int, tree_hash: str) -> str:
        """The directory name for a version: ``0007-a1b2c3d4``.

        Zero-padded so a plain alphabetical listing is also chronological, and
        suffixed with eight characters of tree hash so the content identity is
        visible without opening the manifest.
        """
        return f"{seq:04d}-{short_hash(tree_hash)}"

    def version_paths(self, slug: str, seq: int, tree_hash: str) -> VersionPaths:
        """The :class:`VersionPaths` for one version of one function."""
        return VersionPaths(self.versions_dir(slug) / self.version_dirname(seq, tree_hash))

    def resolve_version_dir(self, stored_dir: str) -> Path:
        """Version dirs are stored relative to the root so the store can move."""
        path = Path(stored_dir)
        return path if path.is_absolute() else self.cfg.root / path

    def relative(self, path: Path) -> str:
        """A path rewritten relative to the archive root, for storing and display.

        Falls back to the absolute path when it lies outside the root, which is
        better than raising in the middle of writing a manifest.
        """
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
        """Delete the whole staging area.

        Called at startup to clear scratch directories left behind by an ingest that
        was interrupted part-way.
        """
        rmtree(self.cfg.root / ".staging")

    # -- manifests -------------------------------------------------------
    def write_manifest(self, paths: VersionPaths, manifest: dict[str, Any]) -> None:
        """Write ``manifest.json`` for a version, pretty-printed and UTF-8.

        Indented and with key order preserved because this file is meant to be read
        by people and diffed by git, not just parsed.
        """
        paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False),
            encoding="utf-8",
        )

    def read_manifest(self, version_dir: Path) -> dict[str, Any] | None:
        """Read a version's ``manifest.json``, or None if it is missing or unreadable.

        Returns None rather than raising so a single corrupt manifest degrades one
        version instead of failing a whole reindex.
        """
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
        """Delete a download whose content is already archived, in ``move`` mode.

        Whether this particular file may be removed at all is the caller's
        decision, not this one's: see ``Ingestor.ingest``.
        """
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
