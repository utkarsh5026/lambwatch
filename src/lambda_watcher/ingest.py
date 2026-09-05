"""The ingest pipeline: one downloaded archive in, one archived version out.

Steps, in order:

1. hash the archive and check whether this exact download was already handled
2. work out which Lambda function it belongs to
3. extract it into a staging directory (safely)
4. analyse the extracted tree
5. compare the content hash against the latest version -> skip if unchanged
6. move staging into its permanent version directory
7. write ``manifest.json``, index everything in SQLite, mirror into git
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .analysis import analyse
from .config import Config
from .db import Database
from .extract import ExtractError, extract_zip
from .gitmirror import GitUnavailable, commit_version, git_available
from .identify import Identification, identify
from .notify import notify
from .store import Store
from .utils import LOG, human_size, rmtree, sha256_file, short_hash, utc_now_iso


@dataclass
class IngestResult:
    status: str  # new | unchanged | duplicate-download | failed | skipped
    source: Path
    function_name: str | None = None
    seq: int | None = None
    version_dir: Path | None = None
    tree_hash: str | None = None
    identification: Identification | None = None
    message: str = ""
    changed_from: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"new", "unchanged", "duplicate-download"}


def wait_until_stable(
    path: Path, stable_seconds: float = 2.0, max_wait: float = 900.0, poll: float = 0.5
) -> bool:
    """Block until a file stops growing, so we never read a partial download."""
    deadline = time.monotonic() + max_wait
    last_signature: tuple[int, float] | None = None
    stable_since: float | None = None

    while time.monotonic() < deadline:
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            time.sleep(poll)
            continue

        signature = (stat_result.st_size, stat_result.st_mtime)
        now = time.monotonic()
        if signature == last_signature:
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= stable_seconds:
                # A final open() confirms nothing still holds it exclusively
                # (matters on Windows, harmless elsewhere).
                try:
                    with path.open("rb"):
                        pass
                except OSError:
                    time.sleep(poll)
                    continue
                return True
        else:
            last_signature = signature
            stable_since = None
        time.sleep(poll)

    LOG.warning("gave up waiting for %s to finish downloading", path)
    return False


def recently_written(mtime: float, max_age: float) -> bool:
    """Is this file new enough to be a download that just landed?

    A filesystem event does not mean a file was written. Windows raises
    "modified" for attribute, security and last-access changes too, so an
    antivirus scan, the search indexer or OneDrive rehydrating a file
    re-announces zips that have sat in Downloads for weeks. Their mtime has not
    moved, which is what tells the two apart.
    """
    if max_age <= 0:
        return True
    return time.time() - mtime <= max_age


class Ingestor:
    """Runs the pipeline. Safe to call repeatedly from a single thread."""

    def __init__(self, cfg: Config, db: Database, store: Store | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.store = store or Store(cfg)

    # -- public API ------------------------------------------------------
    def is_candidate(self, path: Path) -> bool:
        """Cheap filter applied before any I/O-heavy work."""
        name = path.name
        if name.startswith((".", "~$")):
            return False
        suffix = path.suffix.lower()
        if suffix in {s.lower() for s in self.cfg.watch.partial_suffixes}:
            return False
        return suffix in {e.lower() for e in self.cfg.watch.extensions}

    def ingest(
        self,
        zip_path: Path,
        function_override: str | None = None,
        force: bool = False,
        label: str | None = None,
        *,
        just_downloaded: bool = True,
    ) -> IngestResult:
        """Archive one zip.

        ``just_downloaded`` says this file arrived here rather than having been
        found sitting in place, and only such a file may be cleared out of the
        watched folder afterwards. A startup scan and a backfill pass ``False``:
        neither can tell an overnight download from a zip something merely
        touched.
        """
        zip_path = Path(zip_path)
        now = utc_now_iso()

        if not zip_path.exists():
            return IngestResult("failed", zip_path, message="file disappeared before ingest")

        try:
            zip_sha = sha256_file(zip_path)
            source_stat = zip_path.stat()
            zip_size = source_stat.st_size
            source_mtime = datetime.fromtimestamp(
                source_stat.st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        except OSError as exc:
            return IngestResult("failed", zip_path, message=f"could not read file: {exc}")

        # Deleting the download is the one step that destroys something outside
        # the archive, so it takes more than a filesystem event: the file has to
        # have arrived, and have been written recently enough to be that arrival.
        may_discard = just_downloaded and recently_written(
            source_stat.st_mtime, self.cfg.watch.arrival_max_age_seconds
        )

        # 1. Have we already handled this exact download?
        already = self.db.seen_download(zip_sha)
        self.db.mark_download_seen(zip_sha, now, zip_path.name)
        if already and not force:
            self.db.log_event("duplicate-download", now, source_path=str(zip_path),
                              detail={"zip_sha256": zip_sha})
            LOG.info("skipping %s: identical download already archived", zip_path.name)
            if may_discard:
                self.store.discard_original(zip_path)
            return IngestResult(
                "duplicate-download", zip_path, message="this exact file was already archived"
            )

        # 2. Which function is it?
        ident = identify(zip_path, self.cfg.naming, self.db, function_override)
        LOG.info(
            "%s -> function %r (%s, %s confidence)",
            zip_path.name, ident.name, ident.strategy, ident.confidence,
        )

        # 3. Extract into staging.
        staging = self.store.new_staging_dir(short_hash(zip_sha, 12))
        code_staging = staging / "code"
        try:
            extraction = extract_zip(
                zip_path,
                code_staging,
                max_uncompressed_bytes=self.cfg.store.max_uncompressed_mb * 1024 * 1024,
                max_files=self.cfg.store.max_files,
            )
        except ExtractError as exc:
            rmtree(staging)
            self.store.quarantine(zip_path, str(exc))
            self.db.log_event("failed", now, source_path=str(zip_path), detail=str(exc))
            LOG.error("could not extract %s: %s", zip_path.name, exc)
            return IngestResult("failed", zip_path, ident.name, identification=ident, message=str(exc))

        # 4. Analyse.
        try:
            analysis = analyse(code_staging, self.cfg.analysis)
        except Exception as exc:  # noqa: BLE001 - analysis must never lose an archive
            rmtree(staging)
            self.db.log_event("failed", now, source_path=str(zip_path), detail=f"analysis: {exc}")
            LOG.exception("analysis failed for %s", zip_path.name)
            return IngestResult("failed", zip_path, ident.name, identification=ident,
                                message=f"analysis failed: {exc}")

        tree_hash = analysis.inventory.tree_hash
        function_id = self.db.upsert_function(ident.name, ident.slug, now)
        function_row = self.db.get_function_by_name(ident.name)
        slug = function_row["slug"] if function_row else ident.slug

        # 5. Is this content already stored under this function?
        existing = self.db.find_version_by_tree_hash(function_id, tree_hash)
        if existing is not None and not force:
            rmtree(staging)
            self.db.log_event(
                "unchanged", now, function_id=function_id, version_id=existing["id"],
                source_path=str(zip_path), detail={"seq": existing["seq"]},
            )
            LOG.info(
                "%s is byte-identical to %s v%04d - nothing new to archive",
                zip_path.name, ident.name, existing["seq"],
            )
            if may_discard:
                self.store.discard_original(zip_path)
            return IngestResult(
                "unchanged", zip_path, ident.name, int(existing["seq"]),
                self.store.resolve_version_dir(existing["dir"]), tree_hash, ident,
                message=f"identical to version {existing['seq']}",
            )

        previous = self.db.latest_version(function_id)
        seq = self.db.next_seq(function_id)
        paths = self.store.version_paths(slug, seq, tree_hash)

        # 6. Promote staging to its permanent home.
        paths.root.parent.mkdir(parents=True, exist_ok=True)
        rmtree(paths.root)
        try:
            shutil.move(str(staging), str(paths.root))
        except OSError as exc:
            rmtree(staging)
            self.db.log_event("failed", now, function_id=function_id, source_path=str(zip_path),
                              detail=f"store: {exc}")
            return IngestResult("failed", zip_path, ident.name, identification=ident,
                                message=f"could not store version: {exc}")

        kept_zip = self.store.keep_original(zip_path, paths)

        # 7. Manifest + index.
        manifest = analysis.to_manifest(
            {
                "function": {"name": ident.name, "slug": slug},
                "version": {"seq": seq, "ingested_at": now, "label": label},
                "source": {
                    "filename": zip_path.name,
                    "path": str(zip_path),
                    "mtime": source_mtime,
                    "zip_sha256": zip_sha,
                    "zip_size": zip_size,
                    "identification": ident.as_dict(),
                    "kept_at": self.store.relative(kept_zip) if kept_zip else None,
                },
                "archive": {
                    "file_count": extraction.file_count,
                    "dir_count": extraction.dir_count,
                    "compression_ratio": round(extraction.compression_ratio, 3),
                    "skipped_members": extraction.skipped[:50],
                },
                "previous_seq": int(previous["seq"]) if previous else None,
            }
        )
        self.store.write_manifest(paths, manifest)

        version_id = self._index_version(
            function_id=function_id,
            seq=seq,
            tree_hash=tree_hash,
            zip_sha=zip_sha,
            zip_size=zip_size,
            zip_path=zip_path,
            source_mtime=source_mtime,
            now=now,
            version_dir=paths.root,
            analysis=analysis,
            label=label,
        )

        self.db.log_event(
            "new-version", now, function_id=function_id, version_id=version_id,
            source_path=str(zip_path),
            detail={"seq": seq, "tree_hash": tree_hash, "identification": ident.as_dict()},
        )

        self._mirror_to_git(slug, paths.code, seq, ident.name, now, tree_hash, zip_path.name)
        self._prune(slug, function_id)

        if self.cfg.notify.enabled:
            previous_note = f" (was v{previous['seq']:04d})" if previous else ""
            notify(
                f"Lambda archived: {ident.name}",
                f"v{seq:04d}{previous_note} · {analysis.inventory.file_count} files · "
                f"{human_size(analysis.inventory.total_size)}",
                enabled=True,
            )

        LOG.info(
            "archived %s v%04d (%d files, %s)",
            ident.name, seq, analysis.inventory.file_count,
            human_size(analysis.inventory.total_size),
        )
        return IngestResult(
            "new", zip_path, ident.name, seq, paths.root, tree_hash, ident,
            message="archived a new version",
            changed_from=int(previous["seq"]) if previous else None,
        )

    # -- internals -------------------------------------------------------
    def _index_version(self, **kw) -> int:
        analysis = kw["analysis"]
        with self.db.transaction():
            version_id = self.db.insert_version(
                {
                    "function_id": kw["function_id"],
                    "seq": kw["seq"],
                    "tree_hash": kw["tree_hash"],
                    "zip_sha256": kw["zip_sha"],
                    "zip_size": kw["zip_size"],
                    "source_name": kw["zip_path"].name,
                    "source_path": str(kw["zip_path"]),
                    "source_mtime": kw["source_mtime"],
                    "ingested_at": kw["now"],
                    "dir": self.store.relative(kw["version_dir"]),
                    "runtime": analysis.runtime.runtime,
                    "runtime_confidence": analysis.runtime.confidence,
                    "handler": analysis.primary_handler,
                    "file_count": analysis.inventory.file_count,
                    "total_size": analysis.inventory.total_size,
                    "code_file_count": analysis.inventory.code_file_count,
                    "code_size": analysis.inventory.code_size,
                    "code_lines": analysis.inventory.code_lines,
                    "label": kw["label"],
                }
            )
            self.db.bulk_insert(
                "files",
                ["version_id", "path", "size", "sha256", "mode", "is_text", "is_vendor", "lang", "lines"],
                [
                    (version_id, f.path, f.size, f.sha256, f.mode, int(f.is_text), int(f.is_vendor),
                     f.lang, f.lines)
                    for f in analysis.inventory.files
                ],
            )
            self.db.bulk_insert(
                "deps",
                ["version_id", "manager", "name", "version", "source", "is_declared"],
                [(version_id, d.manager, d.name, d.version, d.source, int(d.is_declared))
                 for d in analysis.dependencies],
            )
            self.db.bulk_insert(
                "env_vars", ["version_id", "name", "path", "line"],
                [(version_id, e.name, e.path, e.line) for e in analysis.env_vars if not e.is_reserved],
            )
            self.db.bulk_insert(
                "services", ["version_id", "service", "path", "line"],
                [(version_id, s.service, s.path, s.line) for s in analysis.services],
            )
            self.db.bulk_insert(
                "findings", ["version_id", "kind", "severity", "path", "line", "detail", "is_vendor"],
                [(version_id, f.kind, f.severity, f.path, f.line, f.detail, int(f.is_vendor))
                 for f in analysis.findings],
            )
        return version_id

    def _mirror_to_git(
        self, slug: str, code_dir: Path, seq: int, name: str, now: str, tree_hash: str, source: str
    ) -> None:
        if not self.cfg.git_mirror.enabled:
            return
        if not git_available():
            LOG.debug("git not on PATH; skipping mirror for %s", name)
            return
        message = (
            f"{name} v{seq:04d}\n\n"
            f"source: {source}\n"
            f"tree-hash: {tree_hash}\n"
            f"ingested: {now}\n"
        )
        try:
            commit_version(
                self.store.repo_dir(slug), code_dir, self.cfg.git_mirror, seq, message, now,
                vendor_globs=self.cfg.analysis.vendor_globs,
            )
        except (GitUnavailable, RuntimeError, OSError) as exc:
            LOG.warning("git mirror failed for %s v%04d: %s", name, seq, exc)

    def _prune(self, slug: str, function_id: int) -> None:
        """Drop the oldest versions once a function exceeds the retention limit."""
        keep = self.cfg.store.max_versions_per_function
        if keep <= 0:
            return
        versions = self.db.list_versions(function_id)  # newest first
        for row in versions[keep:]:
            rmtree(self.store.resolve_version_dir(row["dir"]))
            self.db.delete_version(int(row["id"]))
            LOG.info("pruned %s v%04d (retention limit %d)", slug, row["seq"], keep)
