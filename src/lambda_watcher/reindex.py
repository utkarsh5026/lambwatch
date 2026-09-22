"""Rebuild the SQLite index from the manifests on disk.

The archive directories are the source of truth, so the index can always be
thrown away and reconstructed — after a crash, a manual reorganisation, or a
copy of the store onto another machine.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .store import Store
from .utils import LOG, utc_now_iso


def _iter_function_dirs(functions_dir: Path):
    """Walk the archive, yielding ``(function_dir, [version_dir, ...])`` in stable order.

    Sorted at both levels so a rebuild processes versions in sequence order and
    produces the same index every time.
    """
    for function_dir in sorted(p for p in functions_dir.glob("*") if p.is_dir()):
        versions = function_dir / "versions"
        if not versions.is_dir():
            continue
        yield function_dir, sorted(p for p in versions.glob("*") if p.is_dir())


def _dir_key(path: Path) -> str:
    """One spelling per directory, so a path from the index and a path from a walk compare equal."""
    try:
        path = path.resolve()
    except OSError:
        pass
    return os.path.normcase(str(path))


@dataclass
class _Remembered:
    """What the index being replaced knew that no manifest recorded.

    Only ever filled from an archive an older release wrote: ``rename``, ``label``,
    ``--alias`` and ``merge`` used to change ``index.db`` alone, so on such an
    archive the index is the only record of those edits, and throwing it away —
    which is what a rebuild does — threw them away too.
    """

    #: Function name by slug. The slug is the directory, which ``rename`` did move.
    names: dict[str, str] = field(default_factory=dict)
    #: ``(seq, label)`` by version directory, for directories exactly one row claims.
    versions: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    #: ``(pattern, is_regex)`` pairs by slug.
    aliases: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)


def _remember(cfg: Config, store: Store) -> _Remembered:
    """Read what the current index knows before :func:`rebuild` moves it aside.

    back-compat: this exists for archives written before edits reached the
    manifests. Without it, the first ``lw reindex`` on such an archive reverts
    every rename, drops every label and alias, and renumbers merged versions back
    into collisions — the recovery command doing the damage. It can go only when
    no index written before this release can still be opened, which for an
    archive people leave alone for months is not a date anyone can name.

    Where the index and a manifest disagree about a directory the index points at,
    the index wins: on those archives it is newer, because it is where the edit
    went. A directory two rows claim at once — what an old ``merge`` left behind
    — is ambiguous and is left to the manifest.

    Checkpoints the write-ahead log first, so edits not yet folded into the main
    file are read too, and so the ``.bak`` the rebuild keeps is complete. Any
    failure — no index, a corrupt one, one from another schema — returns nothing
    remembered: the manifests alone are then the answer, as they always were.
    """
    remembered = _Remembered()
    if not cfg.db_path.exists():
        return remembered
    try:
        conn = sqlite3.connect(str(cfg.db_path))
    except sqlite3.Error:
        return remembered
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        for row in conn.execute("SELECT name, slug FROM functions"):
            remembered.names[row["slug"]] = row["name"]
        claims: dict[str, list[tuple[int, str | None]]] = defaultdict(list)
        for row in conn.execute("SELECT seq, label, dir FROM versions"):
            claims[_dir_key(store.resolve_version_dir(row["dir"]))].append((int(row["seq"]), row["label"]))
        remembered.versions = {key: found[0] for key, found in claims.items() if len(found) == 1}
        for row in conn.execute(
            "SELECT a.pattern, a.is_regex, f.slug FROM aliases a JOIN functions f ON f.id = a.function_id"
        ):
            remembered.aliases.setdefault(row["slug"], []).append((row["pattern"], bool(row["is_regex"])))
    except sqlite3.Error as exc:
        LOG.warning("could not read the old index, rebuilding from the manifests alone: %s", exc)
        return _Remembered()
    finally:
        conn.close()
    return remembered


def _function_name(slug: str, manifest: dict[str, Any], remembered: _Remembered) -> str:
    """The name a function directory should be indexed under.

    The directory is the function's identity — it is what ``rename`` moves — so the
    slug always comes from it. The name comes from the old index when it has one
    for that slug, then from the manifest if the manifest agrees about the slug,
    and otherwise from the directory itself.

    back-compat: a manifest whose slug disagrees with its directory is one an
    older release renamed without telling. Taking the manifest's word, as this
    used to, filed the function under a slug whose directory did not exist, and
    every later command looked for it in the wrong place.
    """
    if slug in remembered.names:
        return remembered.names[slug]
    function = manifest.get("function") or {}
    if function.get("slug") == slug and function.get("name"):
        return str(function["name"])
    return slug


def _manifest_seq(manifest: dict[str, Any], version_dir: Path) -> int:
    """A version's sequence number from its manifest, else from its directory name.

    back-compat: a manifest from before the sequence number was recorded in it.
    The number is still in the directory name it was used to build
    (``0007-a1b2c3d4``), so recover it there rather than skipping the version and
    silently rebuilding a shorter history than the archive has. A directory named
    some other way counts as 0 and is renumbered with the rest.
    """
    seq = int((manifest.get("version") or {}).get("seq") or 0)
    if seq:
        return seq
    head = version_dir.name.split("-")[0]
    return int(head) if head.isdigit() else 0


def _heal(
    store: Store, identity: dict[str, str], versions: list[tuple[Path, dict[str, Any]]],
    remembered: _Remembered,
) -> list[tuple[Path, dict[str, Any]]]:
    """Make each manifest say what the rebuilt index will say, then return them.

    Healing on read: whatever an older release left only in the index is written
    into the manifests now, so the archive repairs itself on this rebuild and the
    next one needs nothing remembered. A manifest that already agrees is left
    untouched on disk.

    back-compat: sequence numbers must be unique within a function, and an older
    ``merge`` could leave two manifests claiming the same one. When they collide,
    every version is renumbered by archive time — the rule ``merge`` itself uses —
    rather than letting the index's UNIQUE constraint drop one of them silently.
    """
    planned: list[list[Any]] = []
    for version_dir, manifest in versions:
        meta = manifest.get("version") or {}
        recalled = remembered.versions.get(_dir_key(version_dir))
        seq, label = recalled if recalled else (_manifest_seq(manifest, version_dir), meta.get("label"))
        planned.append([version_dir, manifest, seq, label, meta.get("ingested_at") or ""])

    seqs = [plan[2] for plan in planned]
    if len(set(seqs)) != len(seqs) or 0 in seqs:
        LOG.warning("%s: versions claim the same number; renumbering by archive time", identity["slug"])
        planned.sort(key=lambda plan: (plan[4], plan[2]))
        for new_seq, plan in enumerate(planned, start=1):
            plan[2] = new_seq

    healed: list[tuple[Path, dict[str, Any]]] = []
    for version_dir, manifest, seq, label, _ in planned:
        function = manifest.get("function") or {}
        meta = manifest.get("version") or {}
        if (function.get("name"), function.get("slug")) != (identity["name"], identity["slug"]) \
                or meta.get("seq") != seq or meta.get("label") != label:
            store.patch_manifest(version_dir, function=identity, seq=seq, label=label)
            manifest = store.read_manifest(version_dir) or manifest
        healed.append((version_dir, manifest))
    return healed


def rebuild(cfg: Config) -> dict[str, int]:
    """Drop and repopulate the index from the manifests. Returns counts for reporting.

    Everything a rebuild needs is on disk: each version's manifest, and each
    function's ``aliases.json``. An archive from an older release may still have
    some of that only in the index being replaced, so that is read first — see
    :func:`_remember` — and written back to disk as the rebuild goes.
    """
    store = Store(cfg)
    remembered = _remember(cfg, store)
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
    names_taken: set[str] = set()

    for function_dir, version_dirs in _iter_function_dirs(cfg.functions_dir):
        versions: list[tuple[Path, dict[str, Any]]] = []
        for version_dir in version_dirs:
            manifest = store.read_manifest(version_dir)
            if not manifest:
                LOG.warning("no manifest in %s, skipping", version_dir)
                stats["skipped"] += 1
                continue
            versions.append((version_dir, manifest))
        if not versions:
            continue

        slug = function_dir.name
        name = _function_name(slug, versions[0][1], remembered)
        if name in names_taken:
            # Two directories claiming one name would be folded into one function
            # here, their versions colliding. The directory name is unique.
            LOG.warning("%s claims the name %r, already taken; indexing it as %r", slug, name, slug)
            name = slug
        names_taken.add(name)
        identity = {"name": name, "slug": slug}
        versions = _heal(store, identity, versions, remembered)

        first_seen = min(((m.get("version") or {}).get("ingested_at") or now) for _, m in versions)
        function_id = db.upsert_function(name, slug, first_seen)
        stats["functions"] += 1

        aliases = store.read_aliases(function_dir)
        if aliases is None:
            # back-compat: an archive from before aliases were kept on disk. Take
            # them from the index being replaced and write them down, so this is
            # the last rebuild that has to remember them.
            aliases = remembered.aliases.get(slug, [])
            if aliases:
                store.write_aliases(slug, aliases)
        for pattern, is_regex in aliases:
            db.add_alias(function_id, pattern, is_regex)

        for version_dir, manifest in versions:
            ingested_at = (manifest.get("version") or {}).get("ingested_at") or now
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
    # back-compat: every section is fetched with a default rather than indexed,
    # so a manifest written before that section existed rebuilds as a version
    # missing one facet instead of raising and costing the whole archive its
    # index. Each analyser added since shipped is one more reason this stays.
    version_meta = manifest.get("version") or {}
    source = manifest.get("source") or {}
    runtime = manifest.get("runtime") or {}
    totals = manifest.get("totals") or {}
    handlers = manifest.get("handlers") or []

    seq = _manifest_seq(manifest, version_dir)

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
