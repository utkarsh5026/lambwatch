"""Rebuild the SQLite index from the manifests on disk.

The archive directories are the source of truth, so the index can always be
thrown away and reconstructed — after a crash, a manual reorganisation, or a
copy of the store onto another machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .store import Store
from .utils import LOG, utc_now_iso


def _iter_version_dirs(functions_dir: Path):
    """Walk the archive, yielding ``(function_dir, version_dir)`` in stable order.

    Sorted at both levels so a rebuild processes versions in sequence order and
    produces the same index every time.
    """
    for function_dir in sorted(p for p in functions_dir.glob("*") if p.is_dir()):
        versions = function_dir / "versions"
        if not versions.is_dir():
            continue
        for version_dir in sorted(p for p in versions.glob("*") if p.is_dir()):
            yield function_dir, version_dir


def rebuild(cfg: Config) -> dict[str, int]:
    """Drop and repopulate the index. Returns counts for reporting."""
    store = Store(cfg)
    if cfg.db_path.exists():
        backup = cfg.db_path.with_suffix(".db.bak")
        try:
            backup.unlink(missing_ok=True)
            cfg.db_path.replace(backup)
        except OSError as exc:
            LOG.warning("could not back up the old index: %s", exc)
        for suffix in ("-wal", "-shm"):
            Path(str(cfg.db_path) + suffix).unlink(missing_ok=True)

    db = Database(cfg.db_path)
    stats = {"functions": 0, "versions": 0, "skipped": 0}
    now = utc_now_iso()
    seen_functions: dict[str, int] = {}

    for function_dir, version_dir in _iter_version_dirs(cfg.functions_dir):
        manifest = store.read_manifest(version_dir)
        if not manifest:
            LOG.warning("no manifest in %s, skipping", version_dir)
            stats["skipped"] += 1
            continue

        function = manifest.get("function") or {}
        name = function.get("name") or function_dir.name
        slug = function.get("slug") or function_dir.name
        ingested_at = (manifest.get("version") or {}).get("ingested_at") or now

        if name not in seen_functions:
            seen_functions[name] = db.upsert_function(name, slug, ingested_at)
            stats["functions"] += 1
        function_id = seen_functions[name]
        db.conn.execute(
            "UPDATE functions SET last_seen = MAX(last_seen, ?) WHERE id = ?",
            (ingested_at, function_id),
        )

        try:
            _insert(db, store, function_id, manifest, version_dir)
            stats["versions"] += 1
        except Exception as exc:  # noqa: BLE001
            LOG.warning("could not index %s: %s", version_dir, exc)
            stats["skipped"] += 1

    db.log_event("reindex", now, detail=stats)
    db.close()
    return stats


def _insert(db: Database, store: Store, function_id: int, manifest: dict[str, Any],
            version_dir: Path) -> None:
    """Write one version's manifest into the index, exactly as an ingest would.

    This is the rebuild half of the write path, and it has to agree with
    ``Ingestor._index_version`` row for row — if the two drift, ``lw reindex``
    silently produces a different database than the one it replaced. Adding an
    analysis facet means touching both.

    Missing manifest sections default to empty rather than raising, so one
    version written by an older release cannot fail a whole rebuild. A manifest
    with no recorded sequence number falls back to the numeric prefix of its
    directory name, which is where the sequence came from in the first place.
    """
    version_meta = manifest.get("version") or {}
    source = manifest.get("source") or {}
    runtime = manifest.get("runtime") or {}
    totals = manifest.get("totals") or {}
    handlers = manifest.get("handlers") or []

    seq = int(version_meta.get("seq") or 0)
    if not seq:
        # Fall back to the numeric prefix of the directory name.
        seq = int(version_dir.name.split("-")[0])

    with db.transaction():
        version_id = db.insert_version(
            {
                "function_id": function_id,
                "seq": seq,
                "tree_hash": manifest.get("tree_hash") or version_dir.name,
                "zip_sha256": source.get("zip_sha256"),
                "zip_size": source.get("zip_size"),
                "source_name": source.get("filename"),
                "source_path": source.get("path"),
                "source_mtime": source.get("mtime"),
                "ingested_at": version_meta.get("ingested_at") or utc_now_iso(),
                "dir": store.relative(version_dir),
                "runtime": runtime.get("runtime"),
                "runtime_confidence": runtime.get("confidence"),
                "handler": handlers[0]["handler"] if handlers else None,
                "file_count": totals.get("file_count", 0),
                "total_size": totals.get("total_size", 0),
                "code_file_count": totals.get("code_file_count", 0),
                "code_size": totals.get("code_size", 0),
                "code_lines": totals.get("code_lines", 0),
                "label": version_meta.get("label"),
            }
        )
        db.bulk_insert(
            "files",
            ["version_id", "path", "size", "sha256", "mode", "is_text", "is_vendor", "lang", "lines"],
            [
                (version_id, f["path"], f["size"], f["sha256"], f.get("mode"),
                 int(bool(f.get("is_text"))), int(bool(f.get("is_vendor"))),
                 f.get("lang"), f.get("lines", 0))
                for f in manifest.get("files", [])
            ],
        )
        db.bulk_insert(
            "deps", ["version_id", "manager", "name", "version", "source", "is_declared"],
            [
                (version_id, d["manager"], d["name"], d.get("version"), d.get("source"),
                 int(bool(d.get("is_declared"))))
                for d in manifest.get("dependencies", [])
            ],
        )
        db.bulk_insert(
            "env_vars", ["version_id", "name", "path", "line"],
            [
                (version_id, e["name"], e.get("path"), e.get("line"))
                for e in manifest.get("env_vars", []) if not e.get("is_reserved")
            ],
        )
        db.bulk_insert(
            "services", ["version_id", "service", "path", "line"],
            [(version_id, s["service"], s.get("path"), s.get("line"))
             for s in manifest.get("services", [])],
        )
        db.bulk_insert(
            "findings", ["version_id", "kind", "severity", "path", "line", "detail", "is_vendor"],
            [
                (version_id, f["kind"], f["severity"], f.get("path"), f.get("line"),
                 f.get("detail"), int(bool(f.get("is_vendor"))))
                for f in manifest.get("findings", [])
            ],
        )
        if source.get("zip_sha256"):
            db.mark_download_seen(
                source["zip_sha256"], version_meta.get("ingested_at") or utc_now_iso(),
                source.get("filename") or "",
            )
