"""Assemble a :class:`VersionDiff` from the index.

``compare_versions`` deliberately takes plain rows and two directories so it can
be tested with no store behind it. Everything that actually calls it — the CLI's
``diff`` and ``report``, and the report the ingest pipeline renders on its own —
needs the same dozen lookups first, so they live here once instead of three
times.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .compare import VersionDiff, compare_versions

if TYPE_CHECKING:                                  # avoids a Presentation -> Persistence
    from ..config import DiffConfig                # import at runtime; the checker still
    from ..db import Database                      # gets real types
    from ..store import Store


def code_dir(store: Store, version_row: Any) -> Path:
    """Where one version's extracted tree lives."""
    return store.resolve_version_dir(version_row["dir"]) / "code"


def diff_from_index(
    db: Database,
    store: Store,
    diff_cfg: DiffConfig,
    name: str,
    a_row: Any,
    b_row: Any,
    include_vendor: bool | None = None,
    compute_diffs: bool = True,
) -> VersionDiff:
    """Compare two archived versions, pulling every facet out of the index."""
    a_id, b_id = int(a_row["id"]), int(b_row["id"])
    return compare_versions(
        name, int(a_row["seq"]), int(b_row["seq"]),
        db.files_for(a_id), db.files_for(b_id),
        code_dir(store, a_row), code_dir(store, b_row), diff_cfg,
        a_deps=db.deps_for(a_id), b_deps=db.deps_for(b_id),
        a_env=db.env_for(a_id), b_env=db.env_for(b_id),
        a_services=db.services_for(a_id), b_services=db.services_for(b_id),
        a_findings=db.findings_for(a_id), b_findings=db.findings_for(b_id),
        a_meta=dict(a_row), b_meta=dict(b_row),
        include_vendor=include_vendor,
        compute_diffs=compute_diffs,
    )
