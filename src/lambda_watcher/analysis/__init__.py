"""Analysis pipeline: turn an extracted package into a structured manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AnalysisConfig
from .deps import Dependency, detect_dependencies
from .envvars import EnvVarRef, detect_env_vars
from .handler import HandlerCandidate, detect_handlers
from .inventory import FileEntry, Inventory, build_inventory
from .runtime import RuntimeGuess, detect_runtime
from .secrets import Finding, scan
from .services import ServiceRef, detect_services

MANIFEST_SCHEMA = 1

__all__ = [
    "Analysis",
    "Dependency",
    "EnvVarRef",
    "FileEntry",
    "Finding",
    "HandlerCandidate",
    "Inventory",
    "RuntimeGuess",
    "ServiceRef",
    "analyse",
    "MANIFEST_SCHEMA",
]


@dataclass
class Analysis:
    """Everything we learned about one extracted deployment package."""

    inventory: Inventory
    runtime: RuntimeGuess
    handlers: list[HandlerCandidate] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    env_vars: list[EnvVarRef] = field(default_factory=list)
    services: list[ServiceRef] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def primary_handler(self) -> str | None:
        """The handler AWS would most likely invoke, or None if none was found.

        :func:`~.handler.detect_handlers` returns its candidates best-first, so this
        is simply the top one — something like ``lambda_function.lambda_handler``.
        """
        return self.handlers[0].handler if self.handlers else None

    @property
    def vendor_file_count(self) -> int:
        """How many files came from ``node_modules``, ``site-packages`` and friends.

        Usually most of the package. Reported separately so a diff can say "1 file
        you wrote changed, 4,812 vendored files came along with it" instead of
        burying the first number in the second.
        """
        return sum(1 for f in self.inventory.files if f.is_vendor)

    @property
    def vendor_size(self) -> int:
        """Total bytes of vendored files, the companion to :attr:`vendor_file_count`."""
        return sum(f.size for f in self.inventory.files if f.is_vendor)

    def unique_env_vars(self, include_reserved: bool = False) -> list[str]:
        """The distinct environment variable names the code reads, sorted.

        Collapses the per-reference list, which can name the same variable from a
        dozen lines. Runtime-provided names (``AWS_REGION``, ``PATH``) are left out
        unless ``include_reserved`` is set, because they are the same in every
        package and only ever add noise to a diff.
        """
        names = {
            ref.name for ref in self.env_vars if include_reserved or not ref.is_reserved
        }
        return sorted(names)

    def unique_services(self) -> list[str]:
        """The distinct AWS service ids the code talks to, sorted.

        ``["dynamodb", "s3", "sqs"]`` — one entry per service no matter how many
        call sites mention it.
        """
        return sorted({ref.service for ref in self.services})

    def totals(self) -> dict[str, int]:
        """The headline counts for this package, as a flat dict.

        The numbers a summary line is built from: how many files, how many bytes,
        and how much of each is first-party code rather than vendored dependency.
        Kept as one dict because it goes straight into the manifest and into the
        index as a row.
        """
        return {
            "file_count": self.inventory.file_count,
            "total_size": self.inventory.total_size,
            "code_file_count": self.inventory.code_file_count,
            "code_size": self.inventory.code_size,
            "code_lines": self.inventory.code_lines,
            "vendor_file_count": self.vendor_file_count,
            "vendor_size": self.vendor_size,
        }

    def to_manifest(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render this analysis as the ``manifest.json`` written beside the version.

        The manifest is the source of truth on disk: the SQLite index is rebuilt
        from these files by :mod:`~lambda_watcher.reindex`, so anything the index
        needs has to be here. ``extra`` carries the fields only the ingest knows —
        the function name, the sequence number, the originating zip — which are
        merged in over the top.

        Bumping the shape of what this returns means bumping ``MANIFEST_SCHEMA``,
        and old manifests still have to reindex.
        """
        manifest: dict[str, Any] = {
            "schema": MANIFEST_SCHEMA,
            "tree_hash": self.inventory.tree_hash,
            "runtime": self.runtime.as_dict(),
            "handlers": [h.as_dict() for h in self.handlers],
            "totals": self.totals(),
            "languages": self.inventory.language_breakdown(),
            "dependencies": [d.as_dict() for d in self.dependencies],
            "env_vars": [e.as_dict() for e in self.env_vars],
            "services": [s.as_dict() for s in self.services],
            "findings": [f.as_dict() for f in self.findings],
            "files": [f.as_dict() for f in self.inventory.files],
        }
        if extra:
            manifest.update(extra)
        return manifest


def analyse(root: Path, cfg: AnalysisConfig) -> Analysis:
    """Run every analyser over the extracted tree at ``root``."""
    inventory = build_inventory(root, cfg.vendor_globs, cfg.max_scan_file_kb)
    runtime = detect_runtime(inventory)
    handlers = detect_handlers(root, inventory)
    dependencies = detect_dependencies(root, inventory)
    env_vars = detect_env_vars(root, inventory) if cfg.scan_env_vars else []
    services = detect_services(root, inventory) if cfg.scan_aws_services else []
    findings = scan(root, inventory, check_secrets=cfg.scan_secrets)
    return Analysis(
        inventory=inventory,
        runtime=runtime,
        handlers=handlers,
        dependencies=dependencies,
        env_vars=env_vars,
        services=services,
        findings=findings,
    )
