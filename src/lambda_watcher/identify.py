"""Work out which Lambda function a downloaded archive belongs to.

Downloads arrive with whatever name the browser gave them, so identification
runs through a chain of increasingly fuzzy strategies and records which one
won. When it guesses wrong, ``lambda-watcher rename`` fixes it and can leave an
alias behind so the same filename maps correctly next time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import NamingConfig
from .db import Database
from .extract import peek_top_level
from .utils import LOG, short_hash, slugify

# Stems that carry no information about the function.
GENERIC_STEMS = {
    "code", "function", "lambda", "lambda_function", "deployment", "package",
    "archive", "download", "export", "backup", "source", "src", "app", "index",
    "handler", "main", "bundle", "dist", "build", "output",
}

_HEX_ONLY = re.compile(r"^[0-9a-fA-F]{8,}$")
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]{22,}$")


@dataclass
class Identification:
    """The name chosen for a download, and how much to trust it.

    ``strategy`` records which rule in the chain won — ``alias``, ``sidecar-json``,
    ``filename``, ``fallback`` — so a wrong guess can be traced to the step that
    made it rather than argued with in the abstract. ``raw_stem`` keeps the
    original filename stem for the same reason.
    """

    name: str
    slug: str
    confidence: str  # high | medium | low
    strategy: str
    raw_stem: str

    def as_dict(self) -> dict[str, str]:
        """This identification as plain JSON-ready data, for the manifest."""
        return {
            "name": self.name,
            "slug": self.slug,
            "confidence": self.confidence,
            "strategy": self.strategy,
            "raw_stem": self.raw_stem,
        }


def clean_stem(stem: str, strip_patterns: list[str]) -> str:
    """Strip browser suffixes, timestamps and hashes off a filename stem."""
    cleaned = stem.strip()
    # Apply each pattern repeatedly: "fn-2026-01-01-abc123 (1)" needs several passes.
    for _ in range(4):
        before = cleaned
        for pattern in strip_patterns:
            try:
                cleaned = re.sub(pattern, "", cleaned).strip()
            except re.error:
                LOG.warning("invalid strip pattern %r in config, ignoring", pattern)
        cleaned = cleaned.strip(" -_.")
        if cleaned == before:
            break
    return cleaned


def _looks_meaningless(candidate: str) -> bool:
    """True when a candidate name says nothing about which function this is.

    ``package``, ``download``, ``a1b2c3d4e5f6``, ``42`` — names that would
    produce a directory nobody could recognise later. Deliberately narrow: short
    but real names like ``etl`` or ``fn`` are unusual and still allowed through,
    because refusing a real name is worse than accepting a dull one.
    """
    if len(candidate) < 2:
        return True
    if candidate.lower() in GENERIC_STEMS:
        return True
    if _HEX_ONLY.match(candidate):
        return True
    if candidate.isdigit():
        return True
    # Long strings with no separators and no vowels are usually hashes.
    if len(candidate) > 24 and _BASE64ISH.match(candidate) and not re.search(r"[-_]", candidate):
        return True
    return False


def _sidecar_name(zip_path: Path) -> str | None:
    """Read a FunctionName out of a JSON file downloaded next to the zip.

    ``aws lambda get-function > fn.json`` next to ``fn.zip`` is a common
    workflow, and the JSON names the function exactly.
    """
    for candidate in (
        zip_path.with_suffix(".json"),
        zip_path.parent / f"{zip_path.stem}-configuration.json",
        zip_path.parent / f"{zip_path.stem}.config.json",
    ):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        for path in (("Configuration", "FunctionName"), ("FunctionName",), ("functionName",)):
            node = data
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, str) and node:
                return node
    return None


def _zip_embedded_name(zip_path: Path) -> str | None:
    """A single top-level directory in the archive often carries the name."""
    tops = peek_top_level(zip_path)
    if len(tops) == 1 and not tops[0].endswith((".py", ".js", ".json", ".mjs", ".cjs")):
        candidate = tops[0]
        if not _looks_meaningless(candidate):
            return candidate
    return None


def identify(
    zip_path: Path,
    naming: NamingConfig,
    db: Database | None = None,
    override: str | None = None,
) -> Identification:
    """Resolve the function name for ``zip_path``."""
    filename = zip_path.name
    stem = zip_path.stem

    if override:
        return Identification(override, slugify(override), "high", "explicit", stem)

    # 1. Aliases recorded by a previous `rename --alias`.
    if db is not None:
        for alias in db.list_aliases():
            pattern = alias["pattern"]
            matched = False
            if alias["is_regex"]:
                try:
                    matched = re.search(pattern, filename) is not None
                except re.error:
                    matched = False
            else:
                matched = pattern.lower() in filename.lower()
            if matched:
                name = alias["function_name"]
                return Identification(name, slugify(name), "high", "alias", stem)

    # 2. Explicit rules from the config file.
    for rule in naming.rules:
        pattern = rule.get("pattern")
        target = rule.get("name")
        if not pattern or not target:
            continue
        try:
            match = re.search(pattern, filename)
        except re.error:
            LOG.warning("invalid naming rule pattern %r, ignoring", pattern)
            continue
        if match:
            try:
                name = match.expand(target) if "\\" in target else target
            except re.error:
                name = target
            return Identification(name, slugify(name), "high", "config-rule", stem)

    # 3. A JSON config export downloaded alongside the zip.
    sidecar = _sidecar_name(zip_path)
    if sidecar:
        return Identification(sidecar, slugify(sidecar), "high", "sidecar-json", stem)

    # 4. Cleaned-up filename, preferring an exact match on a known function.
    cleaned = clean_stem(stem, naming.strip_patterns)
    if db is not None and cleaned:
        known = db.get_function_by_name(cleaned, naming.case_insensitive)
        if known:
            return Identification(
                known["name"], known["slug"], "high", "known-function", stem
            )
    if db is not None:
        known = db.get_function_by_name(stem, naming.case_insensitive)
        if known:
            return Identification(known["name"], known["slug"], "high", "known-function", stem)

    if cleaned and not _looks_meaningless(cleaned):
        return Identification(cleaned, slugify(cleaned), "medium", "filename", stem)

    # 5. Look inside the archive.
    if naming.infer_from_zip:
        embedded = _zip_embedded_name(zip_path)
        if embedded:
            return Identification(embedded, slugify(embedded), "low", "zip-top-level", stem)

    # 6. Give up, but stay stable: the same odd filename lands in the same place.
    fallback = cleaned or stem or "unknown"
    if _looks_meaningless(fallback):
        fallback = f"unknown-{short_hash(slugify(stem) or 'x', 8)}"
    return Identification(fallback, slugify(fallback), "low", "fallback", stem)
