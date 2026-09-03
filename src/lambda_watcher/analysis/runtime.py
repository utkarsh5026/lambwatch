"""Guess the Lambda runtime from the contents of the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .inventory import Inventory


@dataclass
class RuntimeGuess:
    runtime: str = "unknown"
    confidence: str = "low"
    evidence: list[str] = field(default_factory=list)
    all_scores: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
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
    scores: dict[str, int] = {}
    evidence: list[str] = []

    def bump(lang: str, points: int, why: str) -> None:
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
