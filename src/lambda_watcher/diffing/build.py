"""Assemble report pages from the index.

``compare_versions`` deliberately takes plain rows and two directories so it can
be tested with no store behind it. Everything that actually calls it — the CLI's
``diff`` and ``report``, and the report the ingest pipeline renders on its own —
needs the same dozen lookups first, so they live here once instead of three
times. The front page of ``reports/`` has the same three kinds of caller and
gets the same treatment: see :func:`write_archive_index`.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .compare import VersionDiff, compare_versions
from .render_html import render_archive_index

if TYPE_CHECKING:                                  # avoids a Presentation -> Persistence
    from ..config import DiffConfig                # import at runtime; the checker still
    from ..db import Database                      # gets real types
    from ..store import Store

#: Severities in the order a reader should meet them — the order the secret
#: scanner sorts its own findings into.
SEVERITY_ORDER = ("high", "medium", "low")


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


def _href(target: Path, page_dir: Path) -> str:
    """How a page written into ``page_dir`` links to ``target``.

    Relative whenever it can be — ``order-processor/v0001-v0002.html`` from
    ``reports/`` itself — so the whole folder still works after it is copied
    somewhere else. Two paths on different Windows drives have no relative form,
    so a page written with ``--output D:\\elsewhere`` falls back to an absolute
    ``file:`` URI for those.
    """
    try:
        return quote(Path(os.path.relpath(target, page_dir)).as_posix())
    except ValueError:
        return target.resolve().as_uri()


def archive_index_entries(
    db: Database, reports_dir: Path, page_dir: Path | None = None, store: Store | None = None
) -> list[dict[str, Any]]:
    """One row per archived function for :func:`render_archive_index`, newest archive first.

    Everything comes from the index and one ``exists()`` per link, so this stays
    cheap enough to run on every ingest however large the archive gets — no diff
    is computed here.

    The comparison link points at the page named after the two newest versions,
    ``v0006-v0007.html``, never at ``latest.html``. The two are written together,
    but ``latest.html`` is only the last comparison that was *rendered*: switch
    ``report.auto_diff`` off and versions keep arriving while it stays behind,
    still showing v5 → v6. A page named after the right pair is either there or
    it is not, and when it is not the row says which command writes it.

    A function with no versions at all — its only ingest failed after it was
    named — has nothing to show and is left out.

    With a ``store``, each row also carries the headline of its latest change's
    AI explanation, when there is one: a single file read per function, cheap
    enough for every ingest, and the difference between a front page that says
    *something changed* and one that says *what*.
    """
    from ..ai.report import headline_for

    page_dir = page_dir or reports_dir
    entries: list[dict[str, Any]] = []
    for function in db.list_functions():
        recent = db.list_versions(int(function["id"]), limit=2)
        if not recent:
            continue
        latest = recent[0]
        previous = recent[1] if len(recent) > 1 else None
        seq = int(latest["seq"])
        own_dir = reports_dir / function["slug"]
        change = own_dir / f"v{int(previous['seq']):04d}-v{seq:04d}.html" if previous else None
        history = own_dir / "index.html"
        counts = Counter(f["severity"] for f in db.findings_for(int(latest["id"])))
        extra = sorted(set(counts) - set(SEVERITY_ORDER))
        entries.append({
            "name": function["name"],
            "versions": int(function["version_count"]),
            "seq": seq,
            "previous_seq": int(previous["seq"]) if previous else None,
            "label": latest["label"],
            "ingested_at": latest["ingested_at"],
            "runtime": latest["runtime"],
            "secrets": {s: counts[s] for s in (*SEVERITY_ORDER, *extra) if counts[s]},
            "change_href": _href(change, page_dir) if change and change.exists() else None,
            "history_href": _href(history, page_dir) if history.exists() else None,
        })
        if store is not None and previous is not None:
            explained = headline_for(store, dict(previous), dict(latest))
            if explained:
                entries[-1]["ai_headline"], entries[-1]["ai_risk"] = explained
    # Stored times are UTC ISO strings to the second, so they sort as text.
    entries.sort(key=lambda entry: entry["ingested_at"] or "", reverse=True)
    return entries


def write_archive_index(
    db: Database, reports_dir: Path, page_dir: Path | None = None, store: Store | None = None
) -> tuple[Path, int]:
    """Write ``index.html``, the page linking every function's reports, and say how many it lists.

    ``page_dir`` defaults to ``reports_dir`` itself, which is where the ingest,
    ``lw report`` and the housekeeping commands all keep it; ``lw report
    --output`` is the only caller that moves it, and :func:`_href` keeps the
    links working from wherever it lands. ``store`` lets each row quote its
    latest AI headline; see :func:`archive_index_entries`.

    Written to a temporary file and swapped into place, because two processes
    can rewrite it at once — the background watcher archiving something while
    ``lw report`` runs in a terminal — and a browser reloading halfway through a
    plain rewrite would get half a page. Two threads of one process can too — the
    watcher's ingest thread and its AI explainer — which is why the scratch name
    carries the thread as well as the process.
    """
    page_dir = page_dir or reports_dir
    entries = archive_index_entries(db, reports_dir, page_dir, store)
    page_dir.mkdir(parents=True, exist_ok=True)
    target = page_dir / "index.html"
    scratch = page_dir / f".index.html.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        scratch.write_text(render_archive_index(entries), encoding="utf-8")
        os.replace(scratch, target)
    finally:
        scratch.unlink(missing_ok=True)
    return target, len(entries)
