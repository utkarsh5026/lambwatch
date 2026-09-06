"""Collect the environment variables the code reads.

Environment variables are configuration that lives outside the zip, so a diff
that shows a new ``os.environ["TABLE_NAME"]`` is a strong signal that the
deployment also needs a config change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..utils import read_text
from .inventory import Inventory

_PATTERNS = [
    re.compile(r"""os\.environ\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""os\.environ\.get\s*\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.getenv\s*\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""process\.env\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""System\.getenv\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""ENV\s*\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""Environment\.GetEnvironmentVariable\s*\(\s*"([A-Za-z_][A-Za-z0-9_]*)"""),
]

# Runtime-provided variables are noise in a diff.
_AWS_RESERVED = {
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_EXECUTION_ENV", "AWS_LAMBDA_FUNCTION_NAME",
    "AWS_LAMBDA_FUNCTION_VERSION", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "AWS_LAMBDA_LOG_GROUP_NAME",
    "AWS_LAMBDA_LOG_STREAM_NAME", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR", "_HANDLER", "_X_AMZN_TRACE_ID",
    "TZ", "PATH", "HOME", "NODE_PATH", "PYTHONPATH", "LANG",
}

_SCANNABLE = {"python", "javascript", "typescript", "java", "ruby", "csharp", "go", "shell"}


@dataclass
class EnvVarRef:
    """One place in the code where an environment variable is read.

    The same variable read from three files is three refs; collapse them with
    :meth:`~lambda_watcher.analysis.Analysis.unique_env_vars` when you want the
    set of names rather than the call sites.
    """

    name: str
    path: str
    line: int
    is_reserved: bool = False

    def as_dict(self) -> dict:
        """This reference as plain JSON-ready data, for the manifest."""
        return {"name": self.name, "path": self.path, "line": self.line, "is_reserved": self.is_reserved}


def detect_env_vars(
    root: Path, inventory: Inventory, include_vendor: bool = False, max_files: int = 2000
) -> list[EnvVarRef]:
    """Find every environment variable the code reads, across languages.

    Scans each text file line by line for the idioms that read configuration —
    ``os.environ["X"]`` and ``os.getenv("X")`` in Python, ``process.env.X`` in
    JavaScript, ``System.getenv("X")`` in Java, and the Ruby and C# equivalents
    — and records the name, file and line of each hit.

    Vendored dependencies are skipped by default: they read hundreds of
    variables that have nothing to do with this function. ``max_files`` caps how
    many files are opened so a package with an enormous tree cannot make an
    ingest crawl. Results are deduplicated by ``(name, path, line)`` and sorted,
    so re-analysing an unchanged tree produces an identical list.

    This is textual pattern matching, not parsing: a name built at runtime
    (``os.environ[prefix + "_URL"]``) is invisible to it, and one inside a
    comment still counts.
    """
    refs: list[EnvVarRef] = []
    seen: set[tuple[str, str, int]] = set()
    entries = inventory.files if include_vendor else inventory.code_files
    scanned = 0

    for entry in entries:
        if scanned >= max_files:
            break
        if not entry.is_text or entry.lang not in _SCANNABLE:
            continue
        text = read_text(root / entry.path, max_bytes=1024 * 1024)
        if not text:
            continue
        scanned += 1
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in _PATTERNS:
                for match in pattern.finditer(line):
                    name = match.group(1)
                    key = (name, entry.path, line_no)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(
                        EnvVarRef(name, entry.path, line_no, is_reserved=name in _AWS_RESERVED)
                    )
    refs.sort(key=lambda r: (r.name, r.path, r.line))
    return refs
