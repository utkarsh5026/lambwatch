"""Guess the Lambda runtime from the contents of the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .inventory import Inventory


@dataclass
class RuntimeGuess:
    """Which Lambda runtime this package looks like, and why we think so.

    ``evidence`` names the files and extensions that drove the guess, so a
    surprising answer can be argued with rather than just disbelieved, and
    ``all_scores`` keeps the runners-up for the same reason.
    """

    runtime: str = "unknown"
    confidence: str = "low"
    evidence: list[str] = field(default_factory=list)
    all_scores: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        """This guess as plain JSON-ready data, for the manifest."""
        return {
            "runtime": self.runtime,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "scores": self.all_scores,
        }


# Strong signals: a specific filename at any depth.
_MARKER_FILES: dict[str, tuple[str, int]] = {
    "lambda_function.py": ("python", 40),
    "requirements.txt": ("python", 15),
    "pyproject.toml": ("python", 10),
    "package.json": ("nodejs", 20),
    "package-lock.json": ("nodejs", 10),
    "yarn.lock": ("nodejs", 8),
    "index.js": ("nodejs", 25),
    "index.mjs": ("nodejs", 30),
    "app.js": ("nodejs", 12),
    "pom.xml": ("java", 20),
    "build.gradle": ("java", 15),
    "go.mod": ("go", 30),
    "bootstrap": ("provided", 25),
    "gemfile": ("ruby", 20),
    "gemfile.lock": ("ruby", 15),
}

_EXT_SCORES: dict[str, tuple[str, int]] = {
    ".py": ("python", 3),
    ".js": ("nodejs", 3),
    ".mjs": ("nodejs", 3),
    ".cjs": ("nodejs", 3),
    ".ts": ("nodejs", 2),
    ".java": ("java", 3),
    ".class": ("java", 2),
    ".jar": ("java", 6),
    ".go": ("go", 3),
    ".rb": ("ruby", 3),
    ".cs": ("dotnet", 3),
    ".dll": ("dotnet", 4),
}


def detect_runtime(inventory: Inventory) -> RuntimeGuess:
    """Score the package's files to decide which runtime it targets.

    Two kinds of signal are added up. Marker filenames are strong and specific
    — ``lambda_function.py`` is worth 40 points towards Python, ``go.mod`` 30
    towards Go — while file extensions are weak and cumulative, a few points
    each. The language with the highest total wins.

    Where a marker sits matters as much as which marker it is. One at the
    package root is what the author intended; the same name inside
    ``node_modules`` is somebody else's ``package.json`` and is worth a tenth as
    much, and one buried a few directories down a third. Extensions inside
    vendored trees are ignored outright, since a Python package that vendors a
    JavaScript build tool should still read as Python.

    Confidence is about the margin, not the total: ``high`` needs both a decisive
    score and twice the runner-up, so a package that genuinely looks like two
    runtimes says so instead of picking one and sounding certain. An empty or
    unrecognisable tree returns the default ``unknown``/``low`` guess.
    """
    scores: dict[str, int] = {}
    evidence: list[str] = []

    def bump(lang: str, points: int, why: str) -> None:
        """Add ``points`` to a language's score, recording ``why`` once."""
        scores[lang] = scores.get(lang, 0) + points
        if why not in evidence:
            evidence.append(why)

    for entry in inventory.files:
        name = PurePosixPath(entry.path).name.lower()
        depth = entry.path.count("/")

        marker = _MARKER_FILES.get(name)
        if marker:
            lang, points = marker
            # A marker at the package root is far more meaningful than one
            # buried inside a vendored dependency.
            if entry.is_vendor:
                points = max(1, points // 10)
            elif depth > 1:
                points = max(2, points // 3)
            bump(lang, points, f"{entry.path}")

        if entry.is_vendor:
            continue
        ext = PurePosixPath(entry.path).suffix.lower()
        ext_hit = _EXT_SCORES.get(ext)
        if ext_hit:
            lang, points = ext_hit
            bump(lang, points, f"*{ext}")

    # A .NET deployment is recognisable by its runtime config file.
    for entry in inventory.files:
        if entry.path.endswith(".runtimeconfig.json") or entry.path.endswith(".deps.json"):
            bump("dotnet", 25, entry.path)

    if not scores:
        return RuntimeGuess()

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if top_score >= 30 and top_score >= runner_up * 2:
        confidence = "high"
    elif top_score >= 10:
        confidence = "medium"
    else:
        confidence = "low"

    return RuntimeGuess(
        runtime=top,
        confidence=confidence,
        evidence=evidence[:12],
        all_scores=dict(ranked),
    )
